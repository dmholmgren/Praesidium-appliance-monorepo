"""
modules/ediscovery/routes/guided_ingest.py

Guided-ingestion API (observe -> propose -> confirm -> execute -> account).

  POST /api/v1/ediscovery/guided/propose
      { matter_id, paths: [..server paths..], origin? }
      -> creates a pending collection_proposals row, enqueues the propose job,
         returns { proposal_id, status: "pending" }. Poll the GET below.

  GET  /api/v1/ediscovery/guided/proposal/{id}
      -> { id, status, inventory, proposal }  (status: pending|ready|error|
         confirmed|executed)

  POST /api/v1/ediscovery/guided/confirm/{id}
      { proposal: {..edited contract..} }
      -> creates one collection per confirmed unit and enqueues run_collection_full
         per unit (client-files first, eDiscovery second). Returns created ids.

This is the C3 guided session: the same flow the onboarding *_PLAN_NEEDED
alerts' "execute" button launches, and the flow drag-drop / picker enters.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ediscovery/guided", tags=["ediscovery-guided"])


def _uid(user):
    raw = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _log_triage_turn(*, tenant_id, matter_id, proposal_id, plan_snapshot,
                           actions, source, user_message=None, ai_call_id=None,
                           custodian=None, applied=False, user_id=None,
                           assistant_reply=None, assistant_thinking=None):
    """Persist one plan-review decision as a learnable example.

    Each row pairs the plan as the human saw it (plan_snapshot) with what they
    asked for (user_message and/or the structured actions) and what the system
    proposed — the supervised signal for matching / exclusion / duplicate
    triage. Best-effort: logging must never break the review request.
    """
    import json as _json
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("""
                INSERT INTO triage_turns
                  (tenant_id, matter_id, proposal_id, custodian, source,
                   user_message, plan_snapshot, actions, ai_call_id,
                   applied, user_id, assistant_reply, assistant_thinking)
                VALUES
                  (:tid, CAST(:mid AS uuid), CAST(:pid AS uuid), :cust, :src,
                   :msg, CAST(:plan AS jsonb), CAST(:acts AS jsonb), :cid,
                   :applied, :uid, :areply, :athink)
            """), {
                "tid": tenant_id, "mid": matter_id or None,
                "pid": proposal_id or None, "cust": custodian, "src": source,
                "msg": user_message,
                "plan": _json.dumps(plan_snapshot or []),
                "acts": _json.dumps(actions or []),
                "cid": ai_call_id, "applied": applied, "uid": user_id,
                "areply": assistant_reply, "athink": assistant_thinking,
            })
            await s.commit()
    except Exception:
        logger.exception("triage_turn log failed (proposal=%s)", proposal_id)


def _enqueue_propose(proposal_id, tenant_id, matter_id, paths, user_id):
    import os
    import redis as redis_lib
    from rq import Queue
    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    q = Queue("ediscovery", connection=redis_lib.Redis.from_url(redis_url))
    q.enqueue(
        "modules.ediscovery.jobs.guided_propose.run_proposal",
        proposal_id, tenant_id, matter_id, paths, user_id,
        job_timeout="6h", result_ttl=3600,
    )


@router.post("/propose")
async def propose(request: Request, user=Depends(get_current_user)):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = _uid(user)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    matter_id = (body.get("matter_id") or "").strip()
    paths = body.get("paths") or []
    origin = body.get("origin") or "picker"
    if not matter_id or not paths:
        return JSONResponse({"error": "matter_id and paths required"},
                            status_code=400)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            INSERT INTO collection_proposals
                (tenant_id, matter_id, source_paths, status, origin, created_by)
            VALUES
                (:tid, CAST(:mid AS uuid), CAST(:paths AS jsonb), 'pending',
                 :origin, :uid)
            RETURNING id::text
        """), {"tid": tenant_id, "mid": matter_id,
               "paths": __import__("json").dumps(paths), "origin": origin,
               "uid": user_id})).first()
        proposal_id = row[0]
        await s.commit()

    _enqueue_propose(proposal_id, tenant_id, matter_id, paths, user_id)
    return JSONResponse({"proposal_id": proposal_id, "status": "pending"})


@router.get("/proposal/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request,
                       user=Depends(get_current_user)):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT id::text, matter_id::text, status, source_paths,
                   inventory, proposal, origin, created_at
              FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).mappings().first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
    return JSONResponse(d)


@router.get("/pending")
async def pending(request: Request, matter_id: str = "",
                  user=Depends(get_current_user)):
    """Files pending ingestion for a matter — the DMS bulk-move alert source.

    Surfaces accepted folder matches whose disk file count exceeds what has been
    synced into the DMS (disk_file_count > synced_file_count). Each source's
    absolute disk path (disk_root/best_disk_path) is what the guided modal opens
    against. Returns {} when nothing is pending so the alert stays invisible.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    matter_id = (matter_id or "").strip()
    if not matter_id:
        return JSONResponse({"pending_count": 0, "sources": []})
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text("""
            SELECT id::text AS match_id, disk_root, best_disk_path, folder_path,
                   COALESCE(disk_file_count, 0)   AS disk_file_count,
                   COALESCE(synced_file_count, 0)  AS synced_file_count
              FROM dms_folder_matches
             WHERE matter_id = CAST(:mid AS uuid)
               AND TRIM(tenant_id) = :tid
               AND accepted IS TRUE
               AND COALESCE(disk_file_count, 0) > COALESCE(synced_file_count, 0)
             ORDER BY disk_file_count DESC NULLS LAST
        """), {"mid": matter_id, "tid": tenant_id})).mappings().fetchall()

    sources, total_disk, total_synced = [], 0, 0
    for r in rows:
        root = (r["disk_root"] or "").rstrip("/")
        rel = (r["best_disk_path"] or r["folder_path"] or "").lstrip("/")
        disk_path = f"{root}/{rel}" if root and rel else (root or rel)
        if not disk_path:
            continue
        total_disk += r["disk_file_count"]
        total_synced += r["synced_file_count"]
        sources.append({
            "match_id": r["match_id"],
            "disk_path": disk_path,
            "label": rel.rstrip("/").split("/")[-1] if rel else disk_path,
            "disk_file_count": r["disk_file_count"],
            "synced_file_count": r["synced_file_count"],
        })
    return JSONResponse({
        "matter_id": matter_id,
        "pending_count": len(sources),
        "total_disk_files": total_disk,
        "total_synced": total_synced,
        "sources": sources,
    })


@router.post("/chat/{proposal_id}")
async def chat(proposal_id: str, request: Request,
               user=Depends(get_current_user)):
    """Conversational review of the ingestion plan — output-and-confirm.

    The assistant is grounded strictly in the persisted proposal: it answers
    questions and may PROPOSE include/exclude changes, which the modal renders
    for the attorney to confirm — it never applies them itself. Routed through
    the real AI layer (ediscovery/ingest_triage).
    """
    import json as _json
    from modules.intelligence.anthropic_adapter import (
        call as ai_call, AICallContext)
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT matter_id::text, status, inventory, proposal
              FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).mappings().first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    inv = row["inventory"] or {}
    prop = row["proposal"] or {}
    cols = prop.get("collections") or []
    plan_lines = [
        f"[{i}] {c.get('name')} | track={c.get('track')} bucket={c.get('bucket')}"
        f" docs~{c.get('est_doc_count')} dup={bool(c.get('duplicate'))}"
        f" — {c.get('rationale', '')}"
        for i, c in enumerate(cols)
    ]
    plan_ctx = (
        f"Inventory: {inv.get('file_count')} files, {inv.get('total_human')}. "
        f"{len(cols)} logical collections, "
        f"{len(prop.get('flagged') or [])} flagged.\n" + "\n".join(plan_lines)
    )
    sys_prompt = (
        "You are assisting a litigation attorney reviewing an eDiscovery/DMS "
        "ingestion plan before it runs. Ground every answer strictly in the "
        "plan below; never invent collections or files. You may PROPOSE "
        "include/exclude changes for the user to confirm — never apply them "
        "yourself.\n\n"
        'Respond with ONLY a JSON object with three keys: '
        '{"thinking": "<your step-by-step reasoning about the plan — which '
        'collections are implicated, duplicates, productions vs. custodial '
        'data, etc.>", "reply": "<the concise answer to show the attorney>", '
        '"actions": [{"op": "include"|"exclude", "name": "<exact collection '
        'name>", "reason": "<short>"}]}. Keep reasoning in "thinking" and the '
        'final answer in "reply" — do not mix them. Use an empty actions list '
        "when no change is warranted.\n\n=== INGESTION PLAN ===\n" + plan_ctx
    )
    convo = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}"
        for m in history[-8:]
    )
    user_prompt = (convo + "\n" if convo else "") + "user: " + message

    ctx = AICallContext(
        tenant_id=tenant_id, module="ediscovery", purpose="ingest_triage",
        matter_id=row["matter_id"],
    )
    try:
        res = await ai_call(ctx, raw_user_prompt=user_prompt,
                            raw_system_prompt=sys_prompt)
        raw = (res.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw[:4].lower() == "json":
                raw = raw[4:]
            raw = raw.strip()
        # Extract the JSON object even when the model wraps it in prose, so
        # reasoning never leaks into the answer and actions are never lost.
        parsed = None
        try:
            parsed = _json.loads(raw)
        except Exception:
            lo, hi = raw.find("{"), raw.rfind("}")
            if lo != -1 and hi > lo:
                try:
                    parsed = _json.loads(raw[lo:hi + 1])
                except Exception:
                    parsed = None
        if not isinstance(parsed, dict):
            # Last resort: treat the whole thing as the reply (no thinking),
            # rather than dumping a raw JSON blob into the chat.
            parsed = {"thinking": "", "reply": res.text, "actions": []}
        thinking = parsed.get("thinking") or ""
        reply = parsed.get("reply") or ""
        actions = parsed.get("actions") or []
        # Capture the turn as a learnable example (plan -> instruction ->
        # reasoning -> proposed change), linked to the ai_api_calls spend row.
        await _log_triage_turn(
            tenant_id=tenant_id, matter_id=row["matter_id"],
            proposal_id=proposal_id, plan_snapshot=cols, actions=actions,
            source="chat", user_message=message,
            ai_call_id=getattr(res, "call_id", None), user_id=_uid(user),
            applied=False, assistant_reply=reply, assistant_thinking=thinking,
        )
        return JSONResponse({
            "thinking": thinking,
            "reply": reply,
            "actions": actions,
            "model": res.model_used,
            "cost_usd": float(res.cost_usd),
        })
    except Exception as e:
        logger.exception("guided chat %s failed", proposal_id)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/action/{proposal_id}")
async def action(proposal_id: str, request: Request,
                 user=Depends(get_current_user)):
    """Record a direct plan edit as a learnable triage turn — no AI call.

    Fired when the attorney taps Reject/Restore on a collection (tablet-
    friendly, no typing) or confirms a chat-proposed change. Captures the human
    decision verbatim against the plan snapshot so matching / exclusion /
    duplicate calls become learnable. Body: {actions:[{op,name,reason?}],
    source?}. Returns {ok, logged}.
    """
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    actions = body.get("actions") or []
    if not actions:
        return JSONResponse({"error": "actions required"}, status_code=400)
    source = (body.get("source") or "button").strip() or "button"

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT matter_id::text AS matter_id, proposal
              FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).mappings().first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    cols = (row["proposal"] or {}).get("collections") or []
    names = {(a.get("name") or "").lower() for a in actions}
    custodian = None
    for c in cols:
        if (c.get("name") or "").lower() in names and c.get("custodian"):
            custodian = c.get("custodian")
            break

    await _log_triage_turn(
        tenant_id=tenant_id, matter_id=row["matter_id"],
        proposal_id=proposal_id, plan_snapshot=cols, actions=actions,
        source=source, custodian=custodian, user_id=_uid(user), applied=True,
    )
    return JSONResponse({"ok": True, "logged": len(actions)})


@router.get("/automap")
async def automap(path: str, request: Request, user=Depends(get_current_user)):
    """Sniff a load-file unit's columns and suggest a field map for confirmation."""
    from modules.ediscovery.guided_ingest.automap import automap_for_path
    try:
        return JSONResponse(automap_for_path(path))
    except Exception as e:
        logger.warning("automap failed for %s: %s", path, e)
        return JSONResponse({"found": False, "error": str(e)})


@router.post("/confirm/{proposal_id}")
async def confirm(proposal_id: str, request: Request,
                  user=Depends(get_current_user)):
    from modules.ediscovery.guided_ingest.pipeline import confirm_proposal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = _uid(user)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("""
            SELECT matter_id::text, status FROM collection_proposals
             WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"pid": proposal_id, "tid": tenant_id})).first()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    if row[1] == "executed":
        return JSONResponse({"error": "already executed"}, status_code=409)

    matter_id = row[0]
    edited = body.get("proposal") or {}
    edited["matter_id"] = matter_id   # so pipeline.matter_id_of resolves
    try:
        created = await confirm_proposal(tenant_id, proposal_id, edited, user_id)
    except Exception as e:
        logger.exception("guided confirm %s failed", proposal_id)
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"proposal_id": proposal_id, "status": "executed",
                         "created": created})
