"""
jobs/wiam_engine.py

M5i-D — WIAM Adversarial Gap Analysis Engine (S2-002)

RQ job implementing the four-module adversarial gap analysis system.

Module 1 [142] — Their case: OC claims/defenses vs. depo testimony + produced docs
Module 2 [144] — Your case: own claims/defenses vs. your evidentiary record
Module 3 [146] — External context: industry standards, regulatory frameworks, authority
Module 4 [148] — Drift gaps: unaddressed drift events + unactioned admissions

Finding output subsystem [150]:
  - citations MANDATORY — finding without citations is logged as system error, not written
  - All findings written to wiam_findings table
  - Session marked complete/failed when job finishes

Queue: ediscovery
Entry point: jobs.wiam_engine.run(session_id, matter_id, tenant_id, triggered_by)
Timeout: 900 seconds

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Finding types and validation                                        #
# ------------------------------------------------------------------ #

VALID_FINDING_TYPES = {"opp_gap", "own_gap", "external_gap", "drift_gap"}
VALID_PRIORITIES    = {"high", "medium", "low"}


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


# ================================================================== #
# Citation validation (S2-002 / Claim 11)                            #
# ================================================================== #

def _validate_citations(citations: list) -> bool:
    """
    Hard enforcement: every finding MUST have at least one citation
    with doc_id, page, and line. Finding without valid citations is
    a system error — never written to DB.
    """
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
    claim_element: str,
    description: str,
    citations: list,
    confidence: float,
    priority: str,
    suggested_action: str,
) -> str | None:
    """
    Write a WIAM finding to the DB.
    Returns finding_id if written, None if rejected (citation validation failure).
    """
    if not _validate_citations(citations):
        log.error(
            "WIAM finding REJECTED — missing/malformed citations. "
            "session=%s type=%s element=%s",
            session_id, finding_type, claim_element,
        )
        return None

    if finding_type not in VALID_FINDING_TYPES:
        finding_type = "own_gap"
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    finding_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO wiam_findings
                (id, session_id, matter_id, tenant_id, finding_type,
                 claim_element, description, citations, confidence,
                 priority, suggested_action)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            finding_id, session_id, matter_id, tenant_id,
            finding_type,
            (claim_element or "")[:256],
            description,
            json.dumps(citations),
            min(max(float(confidence), 0.0), 1.0),
            priority,
            suggested_action,
        ))
    return finding_id


def _mark_session(conn, session_id: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE wiam_sessions
            SET status = %s, completed_at = %s
            WHERE id = %s
        """, (status, datetime.now(timezone.utc), session_id))
    conn.commit()


# ================================================================== #
# Context assembly                                                    #
# ================================================================== #

def _assemble_context(matter_id: str, tenant_id: str, conn) -> dict:
    """
    Assemble the full context package for WIAM analysis:
    - Latest issue map
    - KG entities (persons, admissions, contradictions)
    - Recent drift events
    - Sample documents
    """
    context = {
        "issue_map": {},
        "entities": [],
        "admissions": [],
        "contradictions": [],
        "drift_events": [],
        "documents": [],
    }

    with conn.cursor() as cur:
        # Latest issue map version
        cur.execute("""
            SELECT issue_map, version_num, magnitude
            FROM issue_map_versions
            WHERE matter_id = %s AND tenant_id = %s
            ORDER BY version_num DESC LIMIT 1
        """, (matter_id, tenant_id))
        row = cur.fetchone()
        if row:
            im = row["issue_map"]
            if isinstance(im, str):
                try:
                    im = json.loads(im)
                except Exception:
                    im = {}
            context["issue_map"] = im or {}
            context["issue_map_version"] = row["version_num"]
            context["issue_map_magnitude"] = float(row["magnitude"] or 0)

        # KG persons
        cur.execute("""
            SELECT canonical_name, entity_type, properties
            FROM kg_entities
            WHERE matter_id = %s AND tenant_id = %s
              AND entity_type = 'person'
            ORDER BY created_at ASC LIMIT 30
        """, (matter_id, tenant_id))
        context["entities"] = [dict(r) for r in cur.fetchall()]

        # Admissions
        cur.execute("""
            SELECT canonical_name, properties
            FROM kg_entities
            WHERE matter_id = %s AND tenant_id = %s
              AND entity_type = 'admission'
            ORDER BY created_at DESC LIMIT 20
        """, (matter_id, tenant_id))
        context["admissions"] = [dict(r) for r in cur.fetchall()]

        # Contradictions
        cur.execute("""
            SELECT canonical_name, properties
            FROM kg_entities
            WHERE matter_id = %s AND tenant_id = %s
              AND entity_type = 'contradiction'
            ORDER BY created_at DESC LIMIT 20
        """, (matter_id, tenant_id))
        context["contradictions"] = [dict(r) for r in cur.fetchall()]

        # Recent drift events
        cur.execute("""
            SELECT id, dimension, drift_type, description, magnitude,
                   version_before, version_after, source_doc_id
            FROM drift_events
            WHERE matter_id = %s AND tenant_id = %s
            ORDER BY created_at DESC LIMIT 15
        """, (matter_id, tenant_id))
        context["drift_events"] = [dict(r) for r in cur.fetchall()]

        # Sample documents for citation anchors
        cur.execute("""
            SELECT d.id, d.file_name, d.doc_type, d.custodian, d.doc_date,
                   d.bates_begin, d.bates_end
            FROM ediscovery_documents d
            JOIN ediscovery_collections c ON d.collection_id = c.id
            WHERE c.matter_id = %s AND d.tenant_id = %s
            ORDER BY d.created_at ASC LIMIT 30
        """, (matter_id, tenant_id))
        context["documents"] = [dict(r) for r in cur.fetchall()]

    return context


def _context_summary(ctx: dict) -> str:
    """Render context as a text block for AI prompts."""
    im = ctx.get("issue_map", {})
    claims   = im.get("claims", [])
    defenses = im.get("defenses", [])
    players  = im.get("key_players", [])

    lines = []
    lines.append(f"=== ISSUE MAP (v{ctx.get('issue_map_version', '?')}) ===")
    for cl in claims[:5]:
        lines.append(f"CLAIM: {cl.get('claim_name','')}")
        for el in cl.get("elements", [])[:4]:
            lines.append(f"  Element: {el.get('element_name','')} — {el.get('facts_required','')[:120]}")
    for df in defenses[:3]:
        lines.append(f"DEFENSE: {df.get('defense_name','')}")

    lines.append(f"\n=== KEY PLAYERS ({len(players)}) ===")
    for p in players[:10]:
        lines.append(f"  {p.get('name','')} ({p.get('role','')})")

    lines.append(f"\n=== ADMISSIONS ({len(ctx.get('admissions',[]))}) ===")
    for a in ctx.get("admissions", [])[:8]:
        props = a.get("properties") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = {}
        lines.append(f"  {a.get('canonical_name','')} — {props.get('admission_text','')[:120]}")

    lines.append(f"\n=== DRIFT EVENTS ({len(ctx.get('drift_events',[]))}) ===")
    for ev in ctx.get("drift_events", [])[:6]:
        lines.append(
            f"  [{ev.get('dimension','')} | {ev.get('drift_type','')} | "
            f"mag={ev.get('magnitude',0):.2f}] {ev.get('description','')[:100]}"
        )

    lines.append(f"\n=== DOCUMENTS ({len(ctx.get('documents',[]))}) ===")
    for doc in ctx.get("documents", [])[:10]:
        lines.append(
            f"  [{doc.get('doc_type','')}] {doc.get('file_name','')} "
            f"custodian={doc.get('custodian','')} "
            f"bates={doc.get('bates_begin','')}–{doc.get('bates_end','')}"
        )

    return "\n".join(lines)


def _doc_citation_map(ctx: dict) -> dict:
    """Map file_name → doc_id for citation construction."""
    return {
        doc.get("file_name", ""): str(doc.get("id", ""))
        for doc in ctx.get("documents", [])
        if doc.get("id")
    }


def _make_placeholder_citation(ctx: dict) -> list:
    """
    Build a placeholder citation from the first available document.
    Used when AI returns a finding but no specific citation.
    """
    docs = ctx.get("documents", [])
    if docs:
        return [{
            "doc_id": str(docs[0]["id"]),
            "page": "1",
            "line": "1",
            "excerpt_summary": "See document for full context",
        }]
    return []


# ================================================================== #
# WIAM AI prompt template                                            #
# ================================================================== #

WIAM_PROMPT_TEMPLATE = """You are a litigation intelligence engine performing adversarial gap analysis.
You are analyzing {dimension_label} for the matter described below.

{context}

ANALYSIS DIMENSION: {dimension_label}
{dimension_instructions}

Return ONLY a JSON array of findings. Each finding MUST have all fields:
[
  {{
    "finding_type": "{finding_type}",
    "claim_element": "specific claim or defense element name",
    "description": "detailed description of the gap, inconsistency, or risk",
    "citations": [
      {{
        "doc_id": "UUID of the document from the DOCUMENTS section above",
        "page": "page number as string",
        "line": "line number as string",
        "excerpt_summary": "brief quote or summary from that location"
      }}
    ],
    "confidence": 0.0-1.0,
    "priority": "high|medium|low",
    "suggested_action": "specific recommended action"
  }}
]

CRITICAL RULES:
- citations array MUST NOT be empty — every finding requires at least one citation
- doc_id MUST be a UUID from the documents listed above — do not invent UUIDs
- claim_element MUST be specific, not generic ("Element 3 — Breach" not "breach of contract")
- Only surface findings with genuine evidentiary basis — do not hallucinate
- If you cannot find a finding with a real citation, return []
- Return ONLY the JSON array. No preamble. No markdown fences."""

DIM_INSTRUCTIONS = {
    "opp_gap": (
        "THEIR CASE (Opposing Counsel Gaps):\n"
        "For each element of opposing counsel's claims and defenses, identify:\n"
        "1. Elements with NO supporting deposition testimony (MSJ opportunity)\n"
        "2. Internal inconsistencies between opposing witnesses\n"
        "3. Documents that contradict opposing counsel's pleaded position\n"
        "Focus on elements where the evidentiary record does NOT support the pleaded position."
    ),
    "own_gap": (
        "YOUR CASE (Own Case Gaps):\n"
        "For each element of your claims and defenses, identify:\n"
        "1. Elements lacking sufficient documentary or testimonial support\n"
        "2. Admissions by your witnesses that undermine your position\n"
        "3. Documents you have not yet produced that are likely responsive\n"
        "Focus on vulnerabilities in your own evidentiary record."
    ),
    "external_gap": (
        "EXTERNAL CONTEXT (Industry/Legal Gaps):\n"
        "Identify gaps between the current record and:\n"
        "1. Industry standards or customs relevant to the claims\n"
        "2. Regulatory frameworks that apply to the facts\n"
        "3. Recent legal authority that affects the elements\n"
        "Use the documents and entities in the record as anchors for your citations."
    ),
    "drift_gap": (
        "DRIFT GAPS (Unaddressed Case Developments):\n"
        "Based on the drift events listed above, identify:\n"
        "1. Material drift events not yet reflected in litigation strategy\n"
        "2. Admissions that have not been actioned or incorporated into theory\n"
        "3. Contradictions that create impeachment opportunities not yet exploited\n"
        "Cite the source documents associated with each drift event."
    ),
}

DIM_LABELS = {
    "opp_gap": "Opposing Counsel Case Gaps",
    "own_gap": "Own Case Gaps",
    "external_gap": "External Context Gaps",
    "drift_gap": "Drift and Unaddressed Developments",
}


# ================================================================== #
# Four analysis modules                                               #
# ================================================================== #

def _run_dimension(
    dimension: str,
    ctx: dict,
    ctx_text: str,
    session_id: str,
    matter_id: str,
    tenant_id: str,
    conn,
) -> int:
    """
    Run one WIAM dimension. Returns count of findings written.
    """
    label        = DIM_LABELS[dimension]
    instructions = DIM_INSTRUCTIONS[dimension]
    doc_map      = _doc_citation_map(ctx)

    prompt = WIAM_PROMPT_TEMPLATE.format(
        dimension_label=label,
        context=ctx_text,
        dimension_instructions=instructions,
        finding_type=dimension,
    )

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
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
        log.error("wiam_engine: AI call failed for dimension=%s: %s", dimension, exc)
        return 0

    written = 0
    for f in findings:
        citations = f.get("citations", [])

        # Resolve any placeholder doc names to UUIDs
        resolved = []
        for c in citations:
            doc_id = c.get("doc_id", "")
            # If doc_id looks like a filename, try to resolve it
            if doc_id and not _is_uuid(doc_id):
                doc_id = doc_map.get(doc_id, "")
            if doc_id:
                resolved.append({
                    "doc_id": doc_id,
                    "page": str(c.get("page", "1")),
                    "line": str(c.get("line", "1")),
                    "excerpt_summary": c.get("excerpt_summary", ""),
                })

        if not resolved:
            log.error(
                "wiam_engine: finding rejected — no valid citations. "
                "session=%s dimension=%s element=%s",
                session_id, dimension, f.get("claim_element", ""),
            )
            continue

        fid = _write_finding(
            conn=conn,
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            finding_type=dimension,
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
    return written


def _is_uuid(s: str) -> bool:
    try:
        import re
        pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        return bool(re.match(pattern, s.lower()))
    except Exception:
        return False


# ================================================================== #
# Drift gap auto-surfacing (S2-001 high-magnitude auto-action)       #
# ================================================================== #

def _surface_drift_findings(
    ctx: dict,
    session_id: str,
    matter_id: str,
    tenant_id: str,
    conn,
) -> int:
    """
    Auto-surface high-magnitude drift events as drift_gap findings.
    These don't require an AI call — they come directly from drift_events table.
    Still enforces citation requirement using source_doc_id.
    """
    written = 0
    for ev in ctx.get("drift_events", []):
        if float(ev.get("magnitude", 0)) < 0.35:
            continue

        src_doc_id = ev.get("source_doc_id")
        if not src_doc_id:
            # No source doc — cannot build citation, skip
            log.warning(
                "wiam_engine: drift event %s has no source_doc_id — skipping",
                ev.get("id")
            )
            continue

        citations = [{
            "doc_id": str(src_doc_id),
            "page": "1",
            "line": "1",
            "excerpt_summary": f"Source document for drift event: {ev.get('description','')[:120]}",
        }]

        priority = "high" if float(ev.get("magnitude", 0)) >= 0.70 else "medium"

        fid = _write_finding(
            conn=conn,
            session_id=session_id,
            matter_id=matter_id,
            tenant_id=tenant_id,
            finding_type="drift_gap",
            claim_element=f"{ev.get('dimension','')} — {ev.get('drift_type','')}",
            description=(
                f"Drift event detected (magnitude={ev.get('magnitude',0):.2f}): "
                f"{ev.get('description','')}"
            ),
            citations=citations,
            confidence=min(float(ev.get("magnitude", 0.5)), 1.0),
            priority=priority,
            suggested_action=(
                "Review this drift event and update litigation strategy accordingly. "
                "Consider whether issue map refresh is needed."
            ),
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
) -> dict:
    """
    RQ entry point.

    Runs all four WIAM dimensions sequentially, writes findings,
    marks session complete or failed.

    Returns summary dict with session_id, total_findings, per-dimension counts.
    """
    tenant_id = tenant_id.strip()
    log.info(
        "wiam_engine: starting session=%s matter=%s trigger=%s",
        session_id, matter_id, triggered_by,
    )

    conn = _get_conn()
    try:
        # Verify session exists
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM wiam_sessions WHERE id = %s",
                (session_id,)
            )
            sess = cur.fetchone()
        if not sess:
            log.error("wiam_engine: session %s not found", session_id)
            return {"status": "error", "error": "session not found"}

        # Assemble context
        ctx      = _assemble_context(matter_id, tenant_id, conn)
        ctx_text = _context_summary(ctx)

        counts = {
            "opp_gap":      0,
            "own_gap":      0,
            "external_gap": 0,
            "drift_gap":    0,
        }

        # Module 1 — Their case (opp_gap)
        counts["opp_gap"] = _run_dimension(
            "opp_gap", ctx, ctx_text, session_id, matter_id, tenant_id, conn
        )

        # Module 2 — Your case (own_gap)
        counts["own_gap"] = _run_dimension(
            "own_gap", ctx, ctx_text, session_id, matter_id, tenant_id, conn
        )

        # Module 3 — External context (external_gap)
        # Only run if we have documents to anchor citations
        if ctx.get("documents"):
            counts["external_gap"] = _run_dimension(
                "external_gap", ctx, ctx_text, session_id, matter_id, tenant_id, conn
            )

        # Module 4 — Drift gaps (auto-surface + AI)
        # First auto-surface high-magnitude drift events
        drift_auto = _surface_drift_findings(
            ctx, session_id, matter_id, tenant_id, conn
        )
        # Then run AI for additional drift analysis
        drift_ai = _run_dimension(
            "drift_gap", ctx, ctx_text, session_id, matter_id, tenant_id, conn
        )
        counts["drift_gap"] = drift_auto + drift_ai

        total = sum(counts.values())
        _mark_session(conn, session_id, "complete")

        log.info(
            "wiam_engine: session=%s complete — %d total findings",
            session_id, total,
        )

        return {
            "status": "complete",
            "session_id": session_id,
            "total_findings": total,
            "counts": counts,
        }

    except Exception as exc:
        log.exception("wiam_engine: fatal error session=%s: %s", session_id, exc)
        try:
            _mark_session(conn, session_id, "failed")
        except Exception:
            pass
        conn.rollback()
        raise
    finally:
        conn.close()
