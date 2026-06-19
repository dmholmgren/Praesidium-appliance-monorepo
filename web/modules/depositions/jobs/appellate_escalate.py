"""appellate_escalate.py -- Record Fact-Element Classifier / U9.3: T2 frontier lane.

The narrow, bounded escalation lane. T1 (U9.2) flags low-margin / low-score links
needs_escalation; only those reach a model. For each, the frontier (via the
AnthropicAdapter single choke point, routing intelligence/extraction_escalation ->
local Ollama by default, so spend stays bounded and serialized) is shown the record
fact + the candidate elements of its cause and decides element + relation
(supports/undermines/neutral) + confidence + rationale. The verdict refines the link
(tier=2); a 'neutral' verdict rejects it. The closed record caps the denominator, so
this never roams.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.appellate_escalate --appeal UUID [--tenant T] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_SYS = ("You are a legal record classifier for an appeal. Given one record fact and the "
        "numbered elements of a single cause of action, decide which element (if any) the "
        "fact tends to prove (supports) or disprove (undermines). Be strict: choose neutral "
        "if the fact does not bear on any listed element. Respond with ONLY a JSON object: "
        '{"element_n": <int or null>, "relation": "supports"|"undermines"|"neutral", '
        '"confidence": <0..1>, "rationale": "<one sentence>"}.')


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _fact_text(cur, kind, fref):
    if kind == "cr_section":
        cur.execute("SELECT content FROM document_sections WHERE id=CAST(%s AS uuid)", (fref,))
    else:
        cur.execute("SELECT coalesce(question_text,'')||' '||coalesce(answer_text,'') "
                    "FROM transcript_qa_units WHERE id=CAST(%s AS uuid)", (fref,))
    r = cur.fetchone()
    return re.sub(r"\s+", " ", (r[0] if r else "") or "").strip()


def _prompt(fact, elements):
    lines = ["Record fact:", fact[:1200], "", "Candidate elements of the cause of action:"]
    for n, name in elements:
        lines.append("  %s. %s" % (n, name))
    lines.append("")
    lines.append("Which element does the fact support or undermine? JSON only.")
    return "\n".join(lines)


async def _decide(call, AICallContext, tenant, matter_id, kind, fref, fact, elements):
    from modules.intelligence import strip_markdown_fences
    ctx = AICallContext(tenant_id=tenant, module="intelligence",
                        purpose="extraction_escalation", matter_id=matter_id,
                        document_id=fref, document_source=(
                            "document_sections" if kind == "cr_section" else "transcript_qa_units"))
    res = await call(ctx, raw_user_prompt=_prompt(fact, elements), raw_system_prompt=_SYS)
    raw = strip_markdown_fences(res.text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def escalate(tenant_id, appellate_case_id, limit=0) -> dict:
    try:
        from modules.intelligence import call, AICallContext, AILayerError
    except Exception as e:
        return {"error": "AI layer unavailable: %s" % e}
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id::text FROM appellate_cases WHERE id=CAST(%s AS uuid) "
                    "AND TRIM(tenant_id)=%s", (str(appellate_case_id), tenant))
        r = cur.fetchone()
        if not r:
            return {"error": "appellate case not found"}
        matter_id = r[0]

        cur.execute(
            "SELECT l.id::text, l.fact_kind, l.fact_ref_id::text, l.cause_of_action_id::text, "
            "       l.coa_element_id::text FROM record_fact_element_links l "
            "WHERE l.appellate_case_id=CAST(%s AS uuid) AND l.needs_escalation=true "
            "  AND l.tier=1 ORDER BY l.confidence ASC" + (" LIMIT %d" % limit if limit else ""),
            (str(appellate_case_id),))
        targets = cur.fetchall()
        if not targets:
            return {"escalated": 0, "note": "no needs_escalation links"}

        # cache elements per cause: [(n, name, element_id)]
        el_cache = {}

        def elements_for(caid):
            if caid not in el_cache:
                cur.execute("SELECT attributes->>'n', element_name, id::text FROM coa_elements "
                            "WHERE cause_of_action_id=CAST(%s AS uuid) "
                            "ORDER BY (attributes->>'n')::int", (caid,))
                el_cache[caid] = cur.fetchall()
            return el_cache[caid]

        from collections import Counter
        outcome = Counter()
        done = 0
        for (lid, kind, fref, caid, eid) in targets:
            fact = _fact_text(cur, kind, fref)
            els = elements_for(caid)
            if not fact or not els:
                continue
            try:
                verdict = asyncio.run(_decide(call, AICallContext, tenant, matter_id, kind,
                                              fref, fact, [(n, nm) for n, nm, _ in els]))
            except AILayerError as e:
                return {"escalated": done, "error": "AI layer error: %s" % e,
                        "by_outcome": dict(outcome)}
            except Exception as e:
                logger.warning("escalation failed for %s: %s", lid, e)
                outcome["call_failed"] += 1
                continue
            if not verdict:
                outcome["unparsed"] += 1
                continue
            rel = str(verdict.get("relation", "neutral")).lower()
            if rel not in ("supports", "undermines", "neutral"):
                rel = "neutral"
            conf = verdict.get("confidence")
            try:
                conf = float(conf)
            except Exception:
                conf = None
            rationale = str(verdict.get("rationale", ""))[:500]
            en = verdict.get("element_n")
            # map element_n -> element_id (else keep the T1 element)
            new_eid = eid
            if en is not None:
                for n, _nm, ei in els:
                    if str(n) == str(en):
                        new_eid = ei
                        break
            status = "rejected" if rel == "neutral" else "proposed"
            cur.execute(
                "UPDATE record_fact_element_links SET tier=2, relation=%s, confidence=%s, "
                "  rationale=%s, coa_element_id=CAST(%s AS uuid), needs_escalation=false, "
                "  status=%s, updated_at=now() WHERE id=CAST(%s AS uuid)",
                (rel, conf, rationale, new_eid, status, lid))
            outcome[rel] += 1
            done += 1
            if done % 10 == 0:
                conn.commit()
                logger.info("  escalated %d/%d", done, len(targets))
        conn.commit()
        return {"appellate_case_id": str(appellate_case_id), "escalated": done,
                "targets": len(targets), "by_outcome": dict(outcome)}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="T2 frontier escalation (U9.3)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--appeal", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    out = escalate(args.tenant, args.appeal, limit=args.limit)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
