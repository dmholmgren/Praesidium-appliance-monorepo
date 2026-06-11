"""
jobs/wiam_engine.py

M5i-D — WIAM Adversarial Gap Analysis Engine (S2-002)

Upgraded: pulls from full matter data set — DMS documents with extracted text,
emails, contacts, time entries, matter_contacts, eDiscovery docs, KG entities,
issue map, drift events. Four-dimension analysis per patent spec FIG. 3.

Module 1 [142] — Their case: OC claims/defenses vs. evidence
Module 2 [144] — Your case: own claims/defenses vs. your record
Module 3 [146] — External context: industry, regulatory, authority gaps
Module 4 [148] — Drift gaps: unaddressed drift + unactioned admissions

Finding output subsystem [150]:
  - citations MANDATORY — finding without citations = system error
  - dimension column written: their_case | your_case | context | drift
  - Session marked complete/failed on finish

Queue: ediscovery
Entry: jobs.wiam_engine.run(session_id, matter_id, tenant_id, triggered_by)
Timeout: 900 seconds

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Constants                                                           #
# ------------------------------------------------------------------ #

VALID_FINDING_TYPES = {"opp_gap", "own_gap", "external_gap", "drift_gap"}
VALID_DIMENSIONS    = {"their_case", "your_case", "context", "drift"}
VALID_PRIORITIES    = {"critical", "high", "medium", "low"}

# Maps finding_type -> dimension for the S2-002 FIG.3 column
DIMENSION_MAP = {
    "opp_gap":      "their_case",
    "own_gap":      "your_case",
    "external_gap": "context",
    "drift_gap":    "drift",
}


# ================================================================== #
# DB helpers                                                          #
# ================================================================== #

def _get_conn():
    raw = os.environ.get("DATABASE_URL", "")
    raw = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = raw.rfind("@")
    creds = raw[len("postgresql://"):at]
    rest = raw[at + 1:]
    user, password = creds.split(":", 1)
    host_port, dbname = rest.rsplit("/", 1)
    host = host_port.split(":")[0]
    return psycopg2.connect(
        host=host, port=5432,
        dbname=dbname, user=user, password=password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _get_ai_client():
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def _is_uuid(s: str) -> bool:
    try:
        return bool(re.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            s.lower()
        ))
    except Exception:
        return False


# ================================================================== #
# Citation validation (S2-002 / Claim 11)                            #
# ================================================================== #

def _validate_citations(citations: list) -> bool:
    if not citations or not isinstance(citations, list):
        return False
    for c in citations:
        if not isinstance(c, dict):
            return False
        if not all(k in c for k in ("doc_id", "page", "line")):
            return False
        if not c.get("doc_id"):
            return False
    return True


def _write_finding(
    conn,
    session_id: str,
    matter_id: str,
    tenant_id: str,
    finding_type: str,
    dimension: str,
    claim_element: str,
    description: str,
    citations: list,
    confidence: float,
    priority: str,
    suggested_action: str,
) -> str | None:
    if not _validate_citations(citations):
        log.error(
            "WIAM finding REJECTED — missing/malformed citations. "
            "session=%s type=%s dimension=%s element=%s",
            session_id, finding_type, dimension, claim_element,
        )
        return None

    if finding_type not in VALID_FINDING_TYPES:
        finding_type = "own_gap"
    if dimension not in VALID_DIMENSIONS:
        dimension = DIMENSION_MAP.get(finding_type, "your_case")
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    finding_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO wiam_findings
                (id, session_id, matter_id, tenant_id, finding_type,
                 dimension, claim_element, description, citations,
                 confidence, priority, suggested_action)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            finding_id, session_id, matter_id, tenant_id,
            finding_type,
            dimension,
            (claim_element or "")[:256],
            description,
            json.dumps(citations),
            min(max(float(confidence), 0.0), 1.0),
            priority,
            suggested_action,
        ))
    return finding_id


def _mark_session(conn, session_id: str, status: str,
                  total_findings: int = 0, error_log: str = None,
                  context_summary: str = None) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE wiam_sessions
            SET status = %s, completed_at = %s,
                total_findings = %s, error_log = %s,
                context_summary = %s
            WHERE id = %s
        """, (status, datetime.now(timezone.utc), total_findings,
              error_log, context_summary, session_id))
    conn.commit()


# ================================================================== #
# Context assembly — FULL MATTER DATA SET                            #
# ================================================================== #

def _assemble_context(matter_id: str, tenant_id: str, conn) -> dict:
    """
    Assemble the complete matter data package for WIAM analysis.
    Pulls from ALL available data sources — not just eDiscovery.
    """
    ctx = {
        "matter": {},
        "issue_map": {},
        "entities": [],
        "admissions": [],
        "contradictions": [],
        "drift_events": [],
        "ediscovery_docs": [],
        "dms_docs": [],
        "emails": [],
        "contacts": [],
        "time_summary": {},
        "pleadings": [],
    }

    with conn.cursor() as cur:
        # --- Matter info ---
        cur.execute("""
            SELECT id, matter_name, matter_number, matter_type,
                   case_type, description, status, opposing_counsel,
                   court, judge
            FROM matters
            WHERE id = %s AND TRIM(tenant_id) = %s
        """, (matter_id, tenant_id))
        row = cur.fetchone()
        if row:
            ctx["matter"] = dict(row)

        # --- Latest issue map ---
        cur.execute("""
            SELECT issue_map, version_num, magnitude
            FROM issue_map_versions
            WHERE matter_id = %s AND TRIM(tenant_id) = %s
            ORDER BY version_num DESC LIMIT 1
        """, (matter_id, tenant_id))
        row = cur.fetchone()
        if row:
            im = row["issue_map"]
            if isinstance(im, str):
                try: im = json.loads(im)
                except: im = {}
            ctx["issue_map"] = im or {}
            ctx["issue_map_version"] = row["version_num"]

        # --- KG entities ---
        cur.execute("""
            SELECT canonical_name, entity_type, properties
            FROM kg_entities
            WHERE matter_id = %s AND TRIM(tenant_id) = %s
            ORDER BY entity_type, created_at ASC LIMIT 50
        """, (matter_id, tenant_id))
        ctx["entities"] = [dict(r) for r in cur.fetchall()]

        # --- Admissions ---
        cur.execute("""
            SELECT canonical_name, properties
            FROM kg_entities
            WHERE matter_id = %s AND TRIM(tenant_id) = %s
              AND entity_type = 'admission'
            ORDER BY created_at DESC LIMIT 20
        """, (matter_id, tenant_id))
        ctx["admissions"] = [dict(r) for r in cur.fetchall()]

        # --- Contradictions ---
        cur.execute("""
            SELECT canonical_name, properties
            FROM kg_entities
            WHERE matter_id = %s AND TRIM(tenant_id) = %s
              AND entity_type = 'contradiction'
            ORDER BY created_at DESC LIMIT 20
        """, (matter_id, tenant_id))
        ctx["contradictions"] = [dict(r) for r in cur.fetchall()]

        # --- Drift events ---
        cur.execute("""
            SELECT id, dimension, drift_type, description, magnitude,
                   version_before, version_after, source_doc_id
            FROM drift_events
            WHERE matter_id = %s AND TRIM(tenant_id) = %s
            ORDER BY created_at DESC LIMIT 15
        """, (matter_id, tenant_id))
        ctx["drift_events"] = [dict(r) for r in cur.fetchall()]

        # --- eDiscovery documents ---
        cur.execute("""
            SELECT d.id, d.file_name, d.doc_type, d.custodian,
                   d.doc_date, d.bates_begin, d.bates_end
            FROM ediscovery_documents d
            JOIN ediscovery_collections c ON d.collection_id = c.id
            WHERE c.matter_id = %s AND d.tenant_id = %s
            ORDER BY d.created_at ASC LIMIT 40
        """, (matter_id, tenant_id))
        ctx["ediscovery_docs"] = [dict(r) for r in cur.fetchall()]

        # --- DMS documents with extracted text (the big corpus) ---
        cur.execute("""
            SELECT d.id, d.file_name, d.folder_path,
                   LEFT(d.extracted_text, 500) AS text_preview,
                   d.doc_date, d.content_type
            FROM documents d
            WHERE d.matter_id = %s AND TRIM(d.tenant_id) = %s
              AND d.extracted_text IS NOT NULL
              AND d.extracted_text != ''
              AND d.status != 'deleted'
            ORDER BY d.doc_date DESC NULLS LAST, d.created_at DESC
            LIMIT 50
        """, (matter_id, tenant_id))
        ctx["dms_docs"] = [dict(r) for r in cur.fetchall()]

        # --- Pleadings specifically (from DMS, by folder name) ---
        cur.execute("""
            SELECT d.id, d.file_name, d.folder_path,
                   LEFT(d.extracted_text, 800) AS text_preview
            FROM documents d
            WHERE d.matter_id = %s AND TRIM(d.tenant_id) = %s
              AND d.extracted_text IS NOT NULL
              AND (d.folder_path ILIKE '%%plead%%'
                   OR d.folder_path ILIKE '%%motion%%'
                   OR d.folder_path ILIKE '%%brief%%'
                   OR d.folder_path ILIKE '%%order%%')
              AND d.status != 'deleted'
            ORDER BY d.doc_date DESC NULLS LAST
            LIMIT 30
        """, (matter_id, tenant_id))
        ctx["pleadings"] = [dict(r) for r in cur.fetchall()]

        # --- Emails filed to matter ---
        cur.execute("""
            SELECT e.id, e.subject, e.from_email, e.to_email,
                   e.received_at, LEFT(e.body_text, 300) AS body_preview
            FROM email_routing_queue e
            WHERE e.routed_matter_id = %s AND TRIM(e.tenant_id) = %s
            ORDER BY e.received_at DESC LIMIT 30
        """, (matter_id, tenant_id))
        ctx["emails"] = [dict(r) for r in cur.fetchall()]

        # --- Contacts on matter ---
        cur.execute("""
            SELECT c.id, c.full_name, c.email, c.phone, c.company,
                   mc.role, mc.role_detail
            FROM matter_contacts mc
            JOIN contacts c ON mc.contact_id = c.id
            WHERE mc.matter_id = %s AND TRIM(mc.tenant_id) = %s
              AND mc.status = 'confirmed'
            ORDER BY mc.role, c.full_name
            LIMIT 30
        """, (matter_id, tenant_id))
        ctx["contacts"] = [dict(r) for r in cur.fetchall()]

        # --- Time entry summary ---
        cur.execute("""
            SELECT COUNT(*) as entry_count,
                   COALESCE(SUM(hours), 0) as total_hours,
                   MIN(work_date) as earliest_entry,
                   MAX(work_date) as latest_entry
            FROM ts_slips
            WHERE source_client_id IN (
                SELECT legacy_id FROM matters
                WHERE id = %s AND TRIM(tenant_id) = %s
            )
            AND TRIM(tenant_id) = %s
        """, (matter_id, tenant_id, tenant_id))
        row = cur.fetchone()
        if row:
            ctx["time_summary"] = dict(row)

    return ctx


def _context_summary(ctx: dict) -> str:
    """Render assembled context as text block for AI prompts."""
    lines = []
    m = ctx.get("matter", {})
    lines.append(f"=== MATTER: {m.get('matter_name', 'Unknown')} ===")
    lines.append(f"Number: {m.get('matter_number', 'N/A')}")
    lines.append(f"Type: {m.get('matter_type', 'N/A')} / {m.get('case_type', 'N/A')}")
    lines.append(f"Court: {m.get('court', 'N/A')} | Judge: {m.get('judge', 'N/A')}")
    lines.append(f"OC: {m.get('opposing_counsel', 'N/A')}")
    if m.get("description"):
        lines.append(f"Description: {m['description'][:300]}")

    # Issue map
    im = ctx.get("issue_map", {})
    claims = im.get("claims", [])
    defenses = im.get("defenses", [])
    if claims or defenses:
        lines.append(f"\n=== ISSUE MAP (v{ctx.get('issue_map_version', '?')}) ===")
        for cl in claims[:5]:
            lines.append(f"CLAIM: {cl.get('claim_name', '')}")
            for el in cl.get("elements", [])[:4]:
                lines.append(f"  Element: {el.get('element_name', '')} — "
                             f"{el.get('facts_required', '')[:120]}")
        for df in defenses[:3]:
            lines.append(f"DEFENSE: {df.get('defense_name', '')}")

    # Entities
    entities = ctx.get("entities", [])
    if entities:
        lines.append(f"\n=== ENTITIES ({len(entities)}) ===")
        for e in entities[:15]:
            lines.append(f"  [{e.get('entity_type', '')}] {e.get('canonical_name', '')}")

    # Admissions
    admissions = ctx.get("admissions", [])
    if admissions:
        lines.append(f"\n=== ADMISSIONS ({len(admissions)}) ===")
        for a in admissions[:8]:
            props = a.get("properties") or {}
            if isinstance(props, str):
                try: props = json.loads(props)
                except: props = {}
            lines.append(f"  {a.get('canonical_name', '')} — "
                         f"{props.get('admission_text', '')[:120]}")

    # Drift events
    drift = ctx.get("drift_events", [])
    if drift:
        lines.append(f"\n=== DRIFT EVENTS ({len(drift)}) ===")
        for ev in drift[:6]:
            lines.append(
                f"  [{ev.get('dimension', '')} | {ev.get('drift_type', '')} | "
                f"mag={ev.get('magnitude', 0):.2f}] {ev.get('description', '')[:100]}"
            )

    # Pleadings (from DMS)
    pleadings = ctx.get("pleadings", [])
    if pleadings:
        lines.append(f"\n=== PLEADINGS / MOTIONS ({len(pleadings)}) ===")
        for p in pleadings[:10]:
            lines.append(f"  [DOC:{p.get('id', '')}] {p.get('file_name', '')}")
            if p.get("text_preview"):
                lines.append(f"    Preview: {p['text_preview'][:200]}")

    # eDiscovery docs
    edocs = ctx.get("ediscovery_docs", [])
    if edocs:
        lines.append(f"\n=== eDISCOVERY DOCUMENTS ({len(edocs)}) ===")
        for d in edocs[:10]:
            lines.append(
                f"  [DOC:{d.get('id', '')}] {d.get('file_name', '')} "
                f"custodian={d.get('custodian', '')} "
                f"bates={d.get('bates_begin', '')}–{d.get('bates_end', '')}"
            )

    # DMS documents (broader corpus)
    dms = ctx.get("dms_docs", [])
    if dms:
        lines.append(f"\n=== DMS DOCUMENTS ({len(dms)}) ===")
        for d in dms[:15]:
            lines.append(f"  [DOC:{d.get('id', '')}] {d.get('file_name', '')} "
                         f"folder={d.get('folder_path', '')}")
            if d.get("text_preview"):
                lines.append(f"    Preview: {d['text_preview'][:150]}")

    # Emails
    emails = ctx.get("emails", [])
    if emails:
        lines.append(f"\n=== EMAILS ({len(emails)}) ===")
        for e in emails[:10]:
            lines.append(f"  [EMAIL:{e.get('id', '')}] From: {e.get('from_email', '')} "
                         f"Subject: {e.get('subject', '')}")
            if e.get("body_preview"):
                lines.append(f"    Preview: {e['body_preview'][:120]}")

    # Contacts
    contacts = ctx.get("contacts", [])
    if contacts:
        lines.append(f"\n=== MATTER CONTACTS ({len(contacts)}) ===")
        for c in contacts[:15]:
            lines.append(f"  [{c.get('role', '')}] {c.get('full_name', '')} "
                         f"— {c.get('company', '')} {c.get('email', '')}")

    # Time summary
    ts = ctx.get("time_summary", {})
    if ts.get("entry_count"):
        lines.append(f"\n=== TIME SUMMARY ===")
        lines.append(f"  Entries: {ts.get('entry_count', 0)} | "
                     f"Hours: {ts.get('total_hours', 0):.1f} | "
                     f"Range: {ts.get('earliest_entry', 'N/A')} to "
                     f"{ts.get('latest_entry', 'N/A')}")

    return "\n".join(lines)


def _build_doc_map(ctx: dict) -> dict:
    """Map file_name -> doc_id across all document sources."""
    m = {}
    for d in ctx.get("ediscovery_docs", []):
        if d.get("id") and d.get("file_name"):
            m[d["file_name"]] = str(d["id"])
    for d in ctx.get("dms_docs", []):
        if d.get("id") and d.get("file_name"):
            m[d["file_name"]] = str(d["id"])
    for d in ctx.get("pleadings", []):
        if d.get("id") and d.get("file_name"):
            m[d["file_name"]] = str(d["id"])
    return m


# ================================================================== #
# WIAM AI prompt template                                            #
# ================================================================== #

WIAM_PROMPT = """You are a litigation intelligence engine performing adversarial gap analysis for a law firm.
You are analyzing {dimension_label} for the matter described below.

{context}

ANALYSIS DIMENSION: {dimension_label}
{dimension_instructions}

Return ONLY a JSON array of findings. Each finding MUST have ALL fields:
[
  {{
    "finding_type": "{finding_type}",
    "claim_element": "specific claim/defense element or topic",
    "description": "detailed description of the gap, risk, or opportunity",
    "citations": [
      {{
        "doc_id": "UUID from DOC: prefixes above — MUST be a real UUID",
        "page": "page number or '1' if unknown",
        "line": "line number or '1' if unknown",
        "excerpt_summary": "brief description of what this document shows"
      }}
    ],
    "confidence": 0.0-1.0,
    "priority": "critical|high|medium|low",
    "suggested_action": "specific recommended next step"
  }}
]

CRITICAL RULES:
- citations MUST NOT be empty — every finding requires at least one citation
- doc_id MUST be a UUID from the documents listed above (look for DOC: prefixes)
- If you cannot anchor a finding to a specific document, DO NOT include it
- claim_element must be specific, not generic
- Only surface genuine findings with evidentiary basis
- If no findings can be supported by citations, return []
- Return ONLY the JSON array. No preamble. No markdown fences."""

DIM_INSTRUCTIONS = {
    "opp_gap": (
        "THEIR CASE (Opposing Counsel Gaps):\n"
        "For each element of opposing counsel's claims and defenses, identify:\n"
        "1. Elements with NO supporting testimony or documentary evidence (MSJ opportunity)\n"
        "2. Internal inconsistencies between opposing witnesses or documents\n"
        "3. Documents that contradict opposing counsel's pleaded position\n"
        "4. Missing evidence that OC would need to prove their case\n"
        "Focus on elements where the evidentiary record does NOT support OC's position."
    ),
    "own_gap": (
        "YOUR CASE (Own Case Gaps):\n"
        "For each element of your claims and defenses, identify:\n"
        "1. Elements lacking sufficient documentary or testimonial support\n"
        "2. Admissions by your client or witnesses that undermine your position\n"
        "3. Documents in the record that hurt your case theory\n"
        "4. Missing evidence you need but don't appear to have\n"
        "Focus on vulnerabilities in your own evidentiary record."
    ),
    "external_gap": (
        "EXTERNAL CONTEXT (Industry/Legal/Regulatory Gaps):\n"
        "Based on the matter type and facts, identify:\n"
        "1. Industry standards or customs relevant to the claims\n"
        "2. Regulatory frameworks that apply to the facts\n"
        "3. Legal authority considerations based on the matter type\n"
        "4. Potential expert witness needs not yet addressed\n"
        "Anchor citations to documents that establish the factual basis."
    ),
    "drift_gap": (
        "DRIFT GAPS (Unaddressed Case Developments):\n"
        "Based on the drift events and recent documents, identify:\n"
        "1. Material developments not reflected in current litigation strategy\n"
        "2. Admissions that have not been actioned\n"
        "3. Contradictions creating impeachment opportunities not yet exploited\n"
        "4. Timeline gaps where the record goes silent on important issues\n"
        "Cite the source documents associated with each development."
    ),
}

DIM_LABELS = {
    "opp_gap": "Opposing Counsel Case Gaps",
    "own_gap": "Own Case Gaps",
    "external_gap": "External Context Gaps",
    "drift_gap": "Drift and Unaddressed Developments",
}


# ================================================================== #
# Dimension runner                                                    #
# ================================================================== #

def _run_dimension(
    dimension: str,
    ctx: dict,
    ctx_text: str,
    session_id: str,
    matter_id: str,
    tenant_id: str,
    conn,
) -> tuple[int, list[str]]:
    """Run one WIAM dimension. Returns (findings_written, error_messages)."""
    label = DIM_LABELS[dimension]
    instructions = DIM_INSTRUCTIONS[dimension]
    doc_map = _build_doc_map(ctx)
    mapped_dimension = DIMENSION_MAP[dimension]
    errors = []

    prompt = WIAM_PROMPT.format(
        dimension_label=label,
        context=ctx_text,
        dimension_instructions=instructions,
        finding_type=dimension,
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        findings = json.loads(raw)
        if not isinstance(findings, list):
            findings = []
    except Exception as exc:
        msg = f"AI call failed for dimension={dimension}: {exc}"
        log.error("wiam_engine: %s", msg)
        errors.append(msg)
        return 0, errors

    written = 0
    for f in findings:
        citations = f.get("citations", [])
        resolved = []
        for c in citations:
            doc_id = c.get("doc_id", "")
            if doc_id and not _is_uuid(doc_id):
                doc_id = doc_map.get(doc_id, "")
            if doc_id and _is_uuid(doc_id):
                resolved.append({
                    "doc_id": doc_id,
                    "page": str(c.get("page", "1")),
                    "line": str(c.get("line", "1")),
                    "excerpt_summary": c.get("excerpt_summary", ""),
                })

        if not resolved:
            msg = (f"Finding rejected — no valid citations. "
                   f"dimension={dimension} element={f.get('claim_element', '')}")
            log.warning("wiam_engine: %s", msg)
            errors.append(msg)
            continue

        fid = _write_finding(
            conn=conn,
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            finding_type=dimension,
            dimension=mapped_dimension,
            claim_element=f.get("claim_element", ""),
            description=f.get("description", ""),
            citations=resolved,
            confidence=float(f.get("confidence", 0.75)),
            priority=f.get("priority", "medium"),
            suggested_action=f.get("suggested_action", ""),
        )
        if fid:
            written += 1

    conn.commit()
    log.info("wiam_engine: dimension=%s wrote %d findings", dimension, written)
    return written, errors


# ================================================================== #
# Drift auto-surface (high-magnitude)                                #
# ================================================================== #

def _surface_drift_findings(ctx, session_id, matter_id, tenant_id, conn):
    written = 0
    for ev in ctx.get("drift_events", []):
        if float(ev.get("magnitude", 0)) < 0.35:
            continue
        src_doc_id = ev.get("source_doc_id")
        if not src_doc_id:
            continue
        citations = [{
            "doc_id": str(src_doc_id),
            "page": "1", "line": "1",
            "excerpt_summary": f"Drift event source: {ev.get('description', '')[:120]}",
        }]
        priority = "critical" if float(ev.get("magnitude", 0)) >= 0.80 else \
                   "high" if float(ev.get("magnitude", 0)) >= 0.60 else "medium"
        fid = _write_finding(
            conn=conn, session_id=session_id, matter_id=matter_id,
            tenant_id=tenant_id, finding_type="drift_gap",
            dimension="drift",
            claim_element=f"{ev.get('dimension', '')} — {ev.get('drift_type', '')}",
            description=f"Drift event (mag={ev.get('magnitude', 0):.2f}): "
                        f"{ev.get('description', '')}",
            citations=citations,
            confidence=min(float(ev.get("magnitude", 0.5)), 1.0),
            priority=priority,
            suggested_action="Review drift event and update litigation strategy.",
        )
        if fid:
            written += 1
    conn.commit()
    return written


# ================================================================== #
# Main entry point                                                    #
# ================================================================== #

def run(
    session_id: str,
    matter_id: str,
    tenant_id: str,
    triggered_by: str = "manual",
    dimensions: list[str] | None = None,
) -> dict:
    """
    RQ entry point. Runs WIAM analysis across requested dimensions.
    
    Args:
        dimensions: list of dimension keys to run. Default = all four.
                    Valid: opp_gap, own_gap, external_gap, drift_gap
    """
    tenant_id = tenant_id.strip()
    if not dimensions:
        dimensions = ["opp_gap", "own_gap", "external_gap", "drift_gap"]

    log.info(
        "wiam_engine: starting session=%s matter=%s trigger=%s dims=%s",
        session_id, matter_id, triggered_by, dimensions,
    )

    conn = _get_conn()
    all_errors = []
    try:
        # Verify session
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM wiam_sessions WHERE id = %s",
                        (session_id,))
            sess = cur.fetchone()
        if not sess:
            log.error("wiam_engine: session %s not found", session_id)
            return {"status": "error", "error": "session not found"}

        # Store requested dimensions
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE wiam_sessions
                SET dimensions_requested = %s
                WHERE id = %s
            """, (json.dumps(dimensions), session_id))
        conn.commit()

        # Assemble full context
        ctx = _assemble_context(matter_id, tenant_id, conn)
        ctx_text = _context_summary(ctx)

        counts = {}
        for dim in dimensions:
            if dim not in DIM_LABELS:
                continue
            if dim == "drift_gap":
                auto = _surface_drift_findings(
                    ctx, session_id, matter_id, tenant_id, conn)
                ai_count, errs = _run_dimension(
                    dim, ctx, ctx_text, session_id, matter_id, tenant_id, conn)
                counts[dim] = auto + ai_count
                all_errors.extend(errs)
            else:
                count, errs = _run_dimension(
                    dim, ctx, ctx_text, session_id, matter_id, tenant_id, conn)
                counts[dim] = count
                all_errors.extend(errs)

        total = sum(counts.values())
        error_log = "\n".join(all_errors) if all_errors else None

        _mark_session(
            conn, session_id,
            status="complete",
            total_findings=total,
            error_log=error_log,
            context_summary=ctx_text[:5000] if ctx_text else None,
        )

        log.info("wiam_engine: session=%s complete — %d findings", session_id, total)
        return {
            "status": "complete",
            "session_id": session_id,
            "total_findings": total,
            "counts": counts,
            "errors": all_errors,
        }

    except Exception as exc:
        log.exception("wiam_engine: fatal error session=%s: %s", session_id, exc)
        try:
            _mark_session(conn, session_id, "failed",
                          error_log=str(exc))
        except Exception:
            pass
        conn.rollback()
        raise
    finally:
        conn.close()
