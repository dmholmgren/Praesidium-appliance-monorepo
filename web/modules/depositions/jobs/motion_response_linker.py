"""
modules/depositions/jobs/motion_response_linker.py

Link each response/reply to the motion it answers, so the Trial Center Motions
tab can NEST responses + replies under their motion. Titles in the wild are
mostly boilerplate ("Re: Case: 017-..."), so association is by SUBJECT MATTER:
a batched LLM pass is given the matter's motions and its responses/replies (each
with a short text snippet) and returns, per response/reply, the motion id it
responds to (or null). The link is stored on documents.legal_meta.responds_to
(merged, so reclassification preserves it). Unmatched docs stay top-level.

NOTE: this is the nesting linkage only. Response/reply DEADLINES → calendar/tasks
are a separate (deferred) primitive.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.motion_response_linker --tenant T --matter M [--force]
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

_MOTION_CATS = ("motion", "motion_to_dismiss", "plea_to_jurisdiction", "special_appearance")
_CHILD_CATS = ("response", "reply")
_SNIP = 400

_SYS = ("You link litigation RESPONSES/REPLIES to the MOTION each one answers, by "
        "subject matter (not by the boilerplate case caption). You are given MOTIONS "
        "and CHILDREN (responses/replies), each with an id and a snippet. For each "
        "child, return the motion id it responds to, or null if none clearly matches. "
        "Reply ONLY with a JSON object mapping child_id -> motion_id_or_null. No prose.")


def _rows_block(label, rows):
    out = [label + ":"]
    for r in rows:
        snip = (r["snip"] or "").replace("\n", " ")[:_SNIP]
        out.append('  id=%s | %s | %s' % (r["id"], (r["title"] or "")[:90], snip))
    return "\n".join(out)


async def link_responses(tenant_id, matter_id, force=False) -> dict:
    tid = (tenant_id or "").strip()
    out = {"motions": 0, "children": 0, "linked": 0, "unmatched": 0}
    async with AsyncSessionLocal() as s:
        async def _load(cats):
            r = await s.execute(sa_text(
                "SELECT id::text id, COALESCE(NULLIF(title,''), original_filename, filename,'') title, "
                "       LEFT(COALESCE(extracted_text, ocr_text, ''), :n) snip "
                "FROM documents WHERE matter_id = CAST(:m AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
                "  AND legal_category = ANY(:cats) ORDER BY created_at"),
                {"m": matter_id, "t": tid, "cats": list(cats), "n": _SNIP})
            return [dict(x) for x in r.mappings().fetchall()]

        motions = await _load(_MOTION_CATS)
        children = await _load(_CHILD_CATS)
        out["motions"], out["children"] = len(motions), len(children)
        if not motions or not children:
            return out
        if not force:
            children = [c for c in children if not await _already_linked(s, c["id"])]
            if not children:
                return out

        from modules.intelligence.anthropic_adapter import call, AICallContext
        ctx = AICallContext(tenant_id=tid, module="intelligence", purpose="chat",
                            matter_id=matter_id)
        prompt = (_rows_block("MOTIONS", motions) + "\n\n" +
                  _rows_block("CHILDREN (responses/replies)", children) +
                  "\n\nReturn the JSON map now.")
        res = await call(ctx, raw_system_prompt=_SYS, raw_user_prompt=prompt,
                         max_tokens_override=2000)
        raw = (getattr(res, "text", "") or "").strip()
        i, j = raw.find("{"), raw.rfind("}")
        try:
            mapping = json.loads(raw[i:j + 1]) if i >= 0 and j > i else {}
        except Exception:
            logger.warning("linker: non-JSON model output for matter %s", matter_id)
            mapping = {}

        motion_ids = {m["id"] for m in motions}
        for cid, mid in mapping.items():
            if mid and mid in motion_ids and cid != mid:
                await s.execute(sa_text(
                    "UPDATE documents SET legal_meta = "
                    " COALESCE(legal_meta, '{}'::jsonb) || jsonb_build_object('responds_to', :mid), "
                    " updated_at = now() "
                    "WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
                    {"mid": mid, "cid": cid, "t": tid})
                out["linked"] += 1
            else:
                out["unmatched"] += 1
        await s.commit()
    logger.info("motion_response_linker matter=%s motions=%d children=%d linked=%d",
                matter_id, out["motions"], out["children"], out["linked"])
    return out


async def _already_linked(s, doc_id) -> bool:
    r = await s.execute(sa_text(
        "SELECT legal_meta->>'responds_to' FROM documents WHERE id = CAST(:d AS uuid)"),
        {"d": doc_id})
    row = r.fetchone()
    return bool(row and row[0])


if __name__ == "__main__":
    import argparse, asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--matter", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(link_responses(a.tenant, a.matter, force=a.force)), indent=2))
