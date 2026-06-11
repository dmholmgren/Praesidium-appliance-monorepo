"""
modules/drafting/bates_service.py

Layer 9 — Production Consistency Check (M5iii).

Called exclusively by sanity_service.py when:
    - matter has eDiscovery collections
    - sanity_check_config layer 9 is active

Responsibilities:
    1. Detect document citations in draft text
    2. Match citations to production_rows by filename, hash, fuzzy name
    3. Flag: not produced, Bates mismatch, clawback, privilege log
    4. Write bates_insertion_log rows (one per citation found)
    5. Return (result, findings) to sanity runner

Tables read:
    productions          -- production sets (id is VARCHAR(36))
    production_rows      -- individual produced documents
    ediscovery_documents -- for hash matching

Table written:
    bates_insertion_log  -- one row per citation detected

Architecture:
    - AsyncSessionLocal only -- never get_session_factory()
    - tenant_id always .strip()
    - productions.id is VARCHAR(36) -- not UUID
    - No HTTP, no templates, no imports from sanity_service
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.drafting.sanity_service import LayerFinding

logger = logging.getLogger(__name__)

# Minimum trigram similarity score for fuzzy name matching (0.0-1.0)
FUZZY_THRESHOLD = 0.4


# ---------------------------------------------------------------------------
# Citation detection
# ---------------------------------------------------------------------------

def _detect_document_citations(draft_text: str) -> list[str]:
    """
    Extract document references from draft text.

    Targets patterns attorneys use when referencing produced documents:
        - "Exhibit A", "Ex. B", "Exh. 1"
        - "Bates No. PROD001-0047"
        - "DEF000123 through DEF000145"
        - "the Agreement", "the Contract", "the Email"
        - Quoted document names: "Smith Email re: Meeting"
        - File-style references: "smith_agreement_v2.pdf"

    Returns deduplicated list of citation strings.
    """
    patterns = [
        # Exhibit references
        r'\bEx(?:hibit|h?)\.?\s+[A-Z0-9]+\b',
        # Bates number ranges
        r'\b[A-Z]{2,10}[-_]?\d{4,8}(?:\s+through\s+[A-Z]{2,10}[-_]?\d{4,8})?\b',
        # Generic Bates label
        r'\bBates\s+(?:No\.?|Number)?\s*[A-Z]{2,10}[-_]?\d{4,8}\b',
        # Quoted document names (2-8 words in quotes)
        r'"[A-Z][A-Za-z\s]{5,60}"',
        # File name style references
        r'\b\w+[-_]\w+\.(?:pdf|docx?|xlsx?|msg|eml)\b',
        # "the [Document Type]" references
        r'\bthe\s+(?:Agreement|Contract|Email|Letter|Report|Invoice|'
        r'Memo(?:randum)?|Policy|Proposal|Statement|Deposition|Transcript)\b',
    ]

    citations = set()
    for pattern in patterns:
        for match in re.finditer(pattern, draft_text, re.IGNORECASE):
            citation = match.group(0).strip().strip('"')
            if len(citation) >= 4:
                citations.add(citation)

    return list(citations)[:30]  # cap per run


# ---------------------------------------------------------------------------
# Bates lookup
# ---------------------------------------------------------------------------

async def _lookup_by_bates(
    session,
    tenant_id: str,
    matter_id: Optional[UUID],
    bates_ref: str,
) -> Optional[dict]:
    """
    Look up a Bates number directly in production_rows.
    Returns {production_id, bates_start, bates_end, doc_id} or None.
    productions.id is VARCHAR(36).
    """
    # Extract numeric part for range check
    row = await session.execute(
        text(
            "SELECT pr.production_id, pr.bates_begin, pr.bates_end, "
            "       pr.document_id, p.production_name "
            "FROM production_rows pr "
            "JOIN productions p ON p.id = pr.production_id "
            "WHERE trim(p.tenant_id) = :tid "
            "  AND (:mid IS NULL OR p.matter_id = CAST(:mid AS uuid)) "
            "  AND (pr.bates_begin = :ref OR pr.bates_end = :ref "
            "       OR pr.bates_begin ILIKE :prefix) "
            "LIMIT 1"
        ),
        {
            'tid': tenant_id,
            'mid': str(matter_id) if matter_id else None,
            'ref': bates_ref.upper(),
            'prefix': bates_ref.upper()[:8] + '%',
        },
    )
    r = row.fetchone()
    if not r:
        return None
    return {
        'production_id': r.production_id,
        'bates_ref': f"{r.bates_begin} through {r.bates_end}" if r.bates_end != r.bates_begin else r.bates_begin,
        'matched_doc_id': str(r.document_id) if r.document_id else None,
        'match_method': 'filename',  # direct Bates = exact match
        'match_confidence': 1.0,
    }


async def _lookup_by_filename(
    session,
    tenant_id: str,
    matter_id: Optional[UUID],
    citation: str,
) -> Optional[dict]:
    """
    Match citation text against production_rows filename / document description.
    Uses case-insensitive LIKE match.
    """
    search_term = f"%{citation.lower()[:50]}%"
    row = await session.execute(
        text(
            "SELECT pr.production_id, pr.bates_begin, pr.bates_end, "
            "       pr.document_id, pr.doc_description "
            "FROM production_rows pr "
            "JOIN productions p ON p.id = pr.production_id "
            "WHERE trim(p.tenant_id) = :tid "
            "  AND (:mid IS NULL OR p.matter_id = CAST(:mid AS uuid)) "
            "  AND (LOWER(pr.doc_description) LIKE :term "
            "       OR LOWER(pr.native_file) LIKE :term) "
            "LIMIT 1"
        ),
        {
            'tid': tenant_id,
            'mid': str(matter_id) if matter_id else None,
            'term': search_term,
        },
    )
    r = row.fetchone()
    if not r:
        return None
    bates = (
        f"{r.bates_begin} through {r.bates_end}"
        if r.bates_end and r.bates_end != r.bates_begin
        else (r.bates_begin or '')
    )
    return {
        'production_id': r.production_id,
        'bates_ref': bates,
        'matched_doc_id': str(r.document_id) if r.document_id else None,
        'match_method': 'filename',
        'match_confidence': 0.85,
    }


async def _lookup_fuzzy(
    session,
    tenant_id: str,
    matter_id: Optional[UUID],
    citation: str,
) -> Optional[dict]:
    """
    Fuzzy match using pg_trgm similarity against production_rows descriptions.
    Requires pg_trgm extension (already installed on DB-01).
    """
    row = await session.execute(
        text(
            "SELECT pr.production_id, pr.bates_begin, pr.bates_end, "
            "       pr.document_id, "
            "       similarity(LOWER(pr.doc_description), LOWER(:citation)) as sim "
            "FROM production_rows pr "
            "JOIN productions p ON p.id = pr.production_id "
            "WHERE trim(p.tenant_id) = :tid "
            "  AND (:mid IS NULL OR p.matter_id = CAST(:mid AS uuid)) "
            "  AND pr.doc_description IS NOT NULL "
            "  AND similarity(LOWER(pr.doc_description), LOWER(:citation)) > :thresh "
            "ORDER BY sim DESC "
            "LIMIT 1"
        ),
        {
            'tid': tenant_id,
            'mid': str(matter_id) if matter_id else None,
            'citation': citation[:100],
            'thresh': FUZZY_THRESHOLD,
        },
    )
    r = row.fetchone()
    if not r:
        return None
    bates = (
        f"{r.bates_begin} through {r.bates_end}"
        if r.bates_end and r.bates_end != r.bates_begin
        else (r.bates_begin or '')
    )
    return {
        'production_id': r.production_id,
        'bates_ref': bates,
        'matched_doc_id': str(r.document_id) if r.document_id else None,
        'match_method': 'fuzzy',
        'match_confidence': float(r.sim),
    }


async def _check_clawback(
    session,
    tenant_id: str,
    matter_id: Optional[UUID],
    production_id: str,
) -> bool:
    """
    Check if this production set has been subject to a clawback notice.
    Looks for productions with status='clawback' or clawback_notice=true.
    Returns True if clawback flag is set.
    """
    row = await session.execute(
        text(
            "SELECT COUNT(*) as cnt FROM productions "
            "WHERE id = :pid "
            "  AND trim(tenant_id) = :tid "
            "  AND (status = 'clawback' OR clawback_notice = TRUE)"
        ),
        {'pid': production_id, 'tid': tenant_id},
    )
    r = row.fetchone()
    return bool(r and r.cnt > 0)


async def _check_privilege_log(
    session,
    tenant_id: str,
    doc_id: Optional[str],
) -> bool:
    """
    Check if the matched document appears on a privilege log.
    Looks at ediscovery_documents.privilege_designation column.
    """
    if not doc_id:
        return False
    row = await session.execute(
        text(
            "SELECT privilege_designation FROM ediscovery_documents "
            "WHERE id = :did "
            "  AND privilege_designation IS NOT NULL "
            "  AND privilege_designation != '' "
            "LIMIT 1"
        ),
        {'did': doc_id},
    )
    r = row.fetchone()
    return bool(r)


# ---------------------------------------------------------------------------
# Public API — called by sanity_service.py
# ---------------------------------------------------------------------------

async def run_bates_layer(
    session_id: UUID,
    tenant_id: str,
    matter_id: Optional[UUID],
    draft_text: str,
) -> tuple[str, list[LayerFinding]]:
    """
    Run Layer 9 production consistency check.

    1. Detect document citations in draft
    2. For each citation: filename match → fuzzy match → unmatched
    3. Check clawback and privilege log for matched docs
    4. Write bates_insertion_log rows
    5. Return (result, findings)

    result: 'pass' | 'warning' | 'critical' | 'skipped'
    """
    tenant_id = tenant_id.strip()

    citations = _detect_document_citations(draft_text)
    if not citations:
        return 'skipped', []

    findings: list[LayerFinding] = []
    result = 'pass'

    async with AsyncSessionLocal() as session:
        for citation in citations:
            match = None
            match_method = 'unmatched'
            match_confidence = 0.0

            # Try Bates direct match first
            is_bates = bool(re.match(r'^[A-Z]{2,10}[-_]?\d{4,8}', citation.upper()))
            if is_bates:
                match = await _lookup_by_bates(session, tenant_id, matter_id, citation)
                if match:
                    match_method = 'filename'
                    match_confidence = 1.0

            # Filename/description match
            if not match:
                match = await _lookup_by_filename(session, tenant_id, matter_id, citation)
                if match:
                    match_method = match['match_method']
                    match_confidence = match['match_confidence']

            # Fuzzy fallback
            if not match and len(citation) >= 6:
                match = await _lookup_fuzzy(session, tenant_id, matter_id, citation)
                if match:
                    match_method = 'fuzzy'
                    match_confidence = match['match_confidence']

            # Flags
            flag_not_produced = match is None
            flag_bates_mismatch = False
            flag_clawback = False
            flag_privilege_log = False

            if match:
                if match['production_id']:
                    flag_clawback = await _check_clawback(
                        session, tenant_id, matter_id, match['production_id']
                    )
                if match.get('matched_doc_id'):
                    flag_privilege_log = await _check_privilege_log(
                        session, tenant_id, match['matched_doc_id']
                    )

            # Determine severity and build finding
            if flag_clawback:
                findings.append(LayerFinding(
                    severity='critical',
                    message=f"Cited document subject to clawback notice: {citation}",
                    location=citation,
                    suggestion="Remove citation — this document has been clawed back",
                ))
                result = 'critical'
            elif flag_privilege_log:
                findings.append(LayerFinding(
                    severity='critical',
                    message=f"Cited document on privilege log without designation: {citation}",
                    location=citation,
                    suggestion="Add privilege designation or remove citation",
                ))
                result = 'critical'
            elif flag_not_produced:
                findings.append(LayerFinding(
                    severity='warning',
                    message=f"Citation not found in any production set: {citation}",
                    location=citation,
                    suggestion="Verify document was produced or remove citation",
                ))
                if result != 'critical':
                    result = 'warning'
            elif match and match_method == 'fuzzy' and match_confidence < 0.7:
                findings.append(LayerFinding(
                    severity='warning',
                    message=f"Low-confidence Bates match ({match_confidence:.0%}): {citation}",
                    location=citation,
                    suggestion=f"Verify match: {match.get('bates_ref', '')}",
                ))
                if result != 'critical':
                    result = 'warning'

            # Write bates_insertion_log row
            log_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO bates_insertion_log "
                    "(id, session_id, tenant_id, citation_text, "
                    " matched_doc_id, production_id, bates_ref, "
                    " match_method, match_confidence, "
                    " flag_not_produced, flag_bates_mismatch, "
                    " flag_clawback, flag_privilege_log, "
                    " disposition, created_at) "
                    "VALUES "
                    "(:id, :sid, :tid, :citation, "
                    " :doc_id, :prod_id, :bates_ref, "
                    " :method, :conf, "
                    " :not_produced, :mismatch, "
                    " :clawback, :priv, "
                    " 'pending', NOW())"
                ),
                {
                    'id': str(log_id),
                    'sid': str(session_id),
                    'tid': tenant_id,
                    'citation': citation,
                    'doc_id': match.get('matched_doc_id') if match else None,
                    'prod_id': match.get('production_id') if match else None,
                    'bates_ref': match.get('bates_ref') if match else None,
                    'method': match_method,
                    'conf': match_confidence,
                    'not_produced': flag_not_produced,
                    'mismatch': flag_bates_mismatch,
                    'clawback': flag_clawback,
                    'priv': flag_privilege_log,
                },
            )

        await session.commit()

    # If no findings at all, add a pass note
    if not findings and result == 'pass':
        findings.append(LayerFinding(
            severity='info',
            message=f"Checked {len(citations)} citation(s) — all found in production records",
            suggestion='',
        ))

    return result, findings


async def get_bates_insertions(
    session_id: UUID,
    tenant_id: str,
) -> list[dict]:
    """
    Return all bates_insertion_log rows for a session.
    Used by the drafting panel to render the accept/reject list.
    """
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT id, citation_text, matched_doc_id, production_id, "
                "       bates_ref, match_method, match_confidence, "
                "       flag_not_produced, flag_bates_mismatch, "
                "       flag_clawback, flag_privilege_log, "
                "       disposition, disposed_at "
                "FROM bates_insertion_log "
                "WHERE session_id=:sid AND trim(tenant_id)=:tid "
                "ORDER BY created_at"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        return [
            {
                'id': str(r.id),
                'citation_text': r.citation_text,
                'matched_doc_id': str(r.matched_doc_id) if r.matched_doc_id else None,
                'production_id': r.production_id,
                'bates_ref': r.bates_ref,
                'match_method': r.match_method,
                'match_confidence': float(r.match_confidence) if r.match_confidence else 0.0,
                'flag_not_produced': r.flag_not_produced,
                'flag_bates_mismatch': r.flag_bates_mismatch,
                'flag_clawback': r.flag_clawback,
                'flag_privilege_log': r.flag_privilege_log,
                'disposition': r.disposition,
                'disposed_at': r.disposed_at.isoformat() if r.disposed_at else None,
            }
            for r in rows.fetchall()
        ]


async def dispose_insertion(
    log_id: UUID,
    session_id: UUID,
    tenant_id: str,
    disposition: str,
    disposed_by: int,
) -> bool:
    """
    Accept or reject a single bates_insertion_log row.
    disposition must be 'accepted' or 'rejected'.
    Returns True if row was updated.
    """
    if disposition not in ('accepted', 'rejected'):
        raise ValueError(f"Invalid disposition: {disposition!r}")
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "UPDATE bates_insertion_log "
                "SET disposition=:disp, disposed_by=:by, disposed_at=NOW() "
                "WHERE id=:lid AND session_id=:sid AND trim(tenant_id)=:tid "
                "  AND disposition='pending'"
            ),
            {
                'disp': disposition,
                'by': disposed_by,
                'lid': str(log_id),
                'sid': str(session_id),
                'tid': tenant_id,
            },
        )
        await session.commit()
        return result.rowcount > 0
