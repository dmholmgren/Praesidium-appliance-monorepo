"""
modules/reconciliation/recon_task_api.py
JSON API for the Task Reconciliation page.

Endpoints:
  GET  /api/v1/reconciliation/tasks/summary     -> counts by status
  GET  /api/v1/reconciliation/tasks/candidates   -> flagged/extracted emails with signals
  POST /api/v1/reconciliation/tasks/extract       -> run Tier 2 AI on a flagged email
  POST /api/v1/reconciliation/tasks/accept        -> create task from extraction
  POST /api/v1/reconciliation/tasks/dismiss       -> dismiss a candidate
  POST /api/v1/reconciliation/tasks/bulk-dismiss  -> dismiss multiple candidates
  POST /api/v1/reconciliation/tasks/scan          -> trigger Tier 1 scan now
  GET  /api/v1/reconciliation/tasks/created       -> list AI-created tasks for review
  POST /api/v1/reconciliation/tasks/delete-task   -> delete an AI-created task
"""
from __future__ import annotations
import os
import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/reconciliation/tasks", tags=["recon-tasks-api"])


def _tid(r: Request) -> str:
    return (getattr(r.state, "tenant_id", "") or "").strip()


def _uid(r: Request) -> int:
    user = getattr(r.state, "current_user", None)
    return getattr(user, "id", 0) if user else 0


# ── Summary ──────────────────────────────────────────────────────────────

@router.get("/summary")
async def task_recon_summary(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = await db.execute(sa_text("""
            SELECT
                COUNT(*) FILTER (WHERE task_extraction_status = 'flagged')   AS flagged,
                COUNT(*) FILTER (WHERE task_extraction_status = 'extracted') AS extracted,
                COUNT(*) FILTER (WHERE task_extraction_status = 'created')   AS created,
                COUNT(*) FILTER (WHERE task_extraction_status = 'dismissed') AS dismissed,
                COUNT(*) FILTER (WHERE task_extraction_status = 'scanned')   AS scanned,
                COUNT(*) FILTER (WHERE task_extraction_status IS NULL
                                   AND (body_text IS NOT NULL OR body_preview IS NOT NULL)) AS unscanned
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
              AND received_at >= NOW() - INTERVAL '90 days'
        """), {"tid": tid})
        r = row.mappings().fetchone()
        task_row = await db.execute(sa_text("""
            SELECT COUNT(*) AS pending_tasks
            FROM tasks
            WHERE TRIM(tenant_id) = :tid
              AND source = 'ai_email'
              AND status IN ('pending_review', 'open')
        """), {"tid": tid})
        tr = task_row.mappings().fetchone()
    return JSONResponse({
        "flagged": r["flagged"] or 0, "extracted": r["extracted"] or 0,
        "created": r["created"] or 0, "dismissed": r["dismissed"] or 0,
        "scanned": r["scanned"] or 0, "unscanned": r["unscanned"] or 0,
        "pending_review": (r["flagged"] or 0) + (r["extracted"] or 0),
        "pending_tasks": tr["pending_tasks"] or 0,
    })


# ── Candidates list ──────────────────────────────────────────────────────

@router.get("/candidates")
async def task_recon_candidates(request: Request, status: str = "flagged,extracted", limit: int = 50, offset: int = 0):
    tid = _tid(request)
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
    params = {"tid": tid, "lim": limit, "off": offset}
    for i, s in enumerate(statuses):
        params[f"s{i}"] = s
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text(f"""
            SELECT id::text, subject, from_email, from_display,
                   body_preview, received_at,
                   matched_matter_id::text, routing_status,
                   task_extraction_status, task_extraction_result,
                   task_extraction_at
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
              AND task_extraction_status IN ({placeholders})
              AND received_at >= NOW() - INTERVAL '90 days'
            ORDER BY
                CASE task_extraction_status
                    WHEN 'extracted' THEN 1 WHEN 'flagged' THEN 2 ELSE 3
                END, received_at DESC
            LIMIT :lim OFFSET :off
        """), params)
        results = []
        for r in rows.mappings():
            row = dict(r)
            row["received_at"] = row["received_at"].isoformat() if row["received_at"] else None
            row["task_extraction_at"] = row["task_extraction_at"].isoformat() if row["task_extraction_at"] else None
            if isinstance(row.get("task_extraction_result"), str):
                try: row["task_extraction_result"] = json.loads(row["task_extraction_result"])
                except (json.JSONDecodeError, TypeError): pass
            results.append(row)
        count_row = await db.execute(sa_text(f"""
            SELECT COUNT(*) AS cnt FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid AND task_extraction_status IN ({placeholders})
              AND received_at >= NOW() - INTERVAL '90 days'
        """), params)
        total = count_row.scalar() or 0
    return JSONResponse({"candidates": results, "total": total})


# ── Tier 2 AI Extraction ─────────────────────────────────────────────────

@router.post("/extract")
async def task_recon_extract(request: Request):
    tid = _tid(request)
    body = await request.json()
    email_id = body.get("email_id")
    if not email_id:
        return JSONResponse({"error": "email_id required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        row = await db.execute(sa_text("""
            SELECT id::text, subject, body_text, body_preview,
                   from_email, from_display, received_at,
                   matched_matter_id::text, task_extraction_result
            FROM email_routing_queue
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})
        email = row.mappings().fetchone()
    if not email:
        return JSONResponse({"error": "Email not found"}, status_code=404)

    email_body = email["body_text"] or email["body_preview"] or ""
    subject = email["subject"] or ""
    from_display = email["from_display"] or email["from_email"] or ""
    received = email["received_at"]
    existing_result = email["task_extraction_result"]
    if isinstance(existing_result, str):
        try: existing_result = json.loads(existing_result)
        except (json.JSONDecodeError, TypeError): existing_result = {}
    if not existing_result: existing_result = {}
    signals = existing_result.get("tier1_signals", [])
    signal_phrases = "; ".join(s.get("context", s.get("phrase", "")) for s in signals[:5])
    today_str = date.today().isoformat()
    received_str = received.strftime("%Y-%m-%d") if received else today_str
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    next_week_str = (date.today() + timedelta(days=7)).isoformat()

    prompt = f"""You are a legal practice management assistant. Extract actionable tasks from this email.

EMAIL METADATA:
- From: {from_display}
- Subject: {subject}
- Date: {received_str}
- Today's date: {today_str}

SIGNAL PHRASES DETECTED:
{signal_phrases}

EMAIL BODY:
{email_body[:3000]}

INSTRUCTIONS:
1. Extract any commitments, action items, deadlines, or requests from this email.
2. For each task, provide:
   - title: concise task description (imperative form)
   - due_date: ISO date (YYYY-MM-DD). Resolve relative references ("tomorrow" = {tomorrow_str}, "next week" = {next_week_str}, "end of week" = next Friday, "ASAP" = tomorrow). If no date mentioned, use null.
   - priority: "high" / "medium" / "low" based on urgency signals
   - commitment_phrase: the exact phrase from the email that indicates this task
   - assignee_hint: "self" if the email author committed to doing it, "other" if requesting someone else, "unclear" if ambiguous
   - confidence: 0.0-1.0 how confident you are this is a real actionable task
3. Only extract genuine action items. Ignore signatures, disclaimers, pleasantries.
4. If no real tasks exist, return an empty tasks array.

Respond ONLY with valid JSON, no markdown fences:
{{"tasks": [...]}}"""

    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            ai_data = resp.json()
    except Exception as e:
        logger.exception("AI extraction failed for email %s: %s", email_id, e)
        return JSONResponse({"error": f"AI extraction failed: {str(e)}"}, status_code=500)

    ai_text = ""
    for block in ai_data.get("content", []):
        if block.get("type") == "text": ai_text += block.get("text", "")
    try:
        clean = ai_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"): clean = clean[:-3]
        extracted = json.loads(clean.strip())
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Failed to parse AI response for email %s: %s\nRaw: %s", email_id, e, ai_text[:500])
        return JSONResponse({"error": "Failed to parse AI response"}, status_code=500)

    tasks = extracted.get("tasks", [])
    result_obj = {**existing_result, "tier2_extraction": {
        "tasks": tasks, "model": "claude-sonnet-4-6",
        "extracted_at": datetime.utcnow().isoformat() + "Z",
    }}
    new_status = "extracted" if tasks else "scanned"
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE email_routing_queue
            SET task_extraction_status = :status,
                task_extraction_result = CAST(:result AS jsonb),
                task_extraction_at = NOW()
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid, "status": new_status, "result": json.dumps(result_obj)})
        await db.commit()
    return JSONResponse({"ok": True, "email_id": email_id, "status": new_status,
                         "tasks_found": len(tasks), "tasks": tasks})


# ── Accept (create task) ─────────────────────────────────────────────────

@router.post("/accept")
async def task_recon_accept(request: Request):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    email_id = body.get("email_id")
    task_index = body.get("task_index", 0)
    overrides = body.get("overrides", {})
    if not email_id:
        return JSONResponse({"error": "email_id required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        row = await db.execute(sa_text("""
            SELECT id::text, subject, from_email, from_display,
                   matched_matter_id::text,
                   task_extraction_result, task_extraction_status
            FROM email_routing_queue
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})
        email = row.mappings().fetchone()
    if not email:
        return JSONResponse({"error": "Email not found"}, status_code=404)
    result = email["task_extraction_result"]
    if isinstance(result, str):
        try: result = json.loads(result)
        except (json.JSONDecodeError, TypeError): result = {}
    tasks = (result.get("tier2_extraction") or {}).get("tasks", [])
    signals = result.get("tier1_signals", [])
    if not tasks and signals:
        task_data = {
            "title": overrides.get("title", f"Follow up: {(email['subject'] or 'Email task')[:100]}"),
            "due_date": overrides.get("due_date"), "priority": overrides.get("priority", "medium"),
            "commitment_phrase": signals[0].get("phrase", "") if signals else "", "confidence": 0.5,
        }
    elif task_index < len(tasks):
        task_data = tasks[task_index]
    else:
        return JSONResponse({"error": f"Task index {task_index} out of range ({len(tasks)} tasks)"}, status_code=400)

    title = overrides.get("title", task_data.get("title", "Untitled task"))
    due_date_str = overrides.get("due_date", task_data.get("due_date"))
    priority = overrides.get("priority", task_data.get("priority", "medium"))
    matter_id = overrides.get("matter_id", email["matched_matter_id"])
    due_date = None
    if due_date_str:
        try: due_date = datetime.fromisoformat(due_date_str)
        except (ValueError, TypeError): pass
    desc_parts = [
        f"From: {email.get('from_display') or email.get('from_email') or 'Unknown'}",
        f"Subject: {email.get('subject') or '(no subject)'}",
    ]
    commitment = task_data.get("commitment_phrase", "")
    if commitment: desc_parts.append(f'Signal: "{commitment}"')
    description = "\n".join(desc_parts)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO tasks (tenant_id, title, description, source, source_ref,
                               priority, status, due_date, matter_id,
                               created_by, tags, task_type)
            VALUES (:tid, :title, :desc, 'ai_email', :source_ref,
                    :priority, 'open', :due_date, CAST(:matter_id AS uuid),
                    :uid, CAST(:tags AS jsonb), 'ai_suggested')
        """), {
            "tid": tid, "title": title[:500], "desc": description,
            "source_ref": email_id, "priority": priority, "due_date": due_date,
            "matter_id": matter_id, "uid": uid,
            "tags": json.dumps(["ai_generated", "email_derived"]),
        })
        await db.execute(sa_text("""
            UPDATE email_routing_queue SET task_extraction_status = 'created'
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "email_id": email_id, "title": title})


# ── Dismiss ──────────────────────────────────────────────────────────────

@router.post("/dismiss")
async def task_recon_dismiss(request: Request):
    tid = _tid(request)
    body = await request.json()
    email_id = body.get("email_id")
    if not email_id:
        return JSONResponse({"error": "email_id required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE email_routing_queue SET task_extraction_status = 'dismissed'
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
              AND task_extraction_status IN ('flagged', 'extracted')
        """), {"eid": email_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True})


@router.post("/bulk-dismiss")
async def task_recon_bulk_dismiss(request: Request):
    tid = _tid(request)
    body = await request.json()
    email_ids = body.get("email_ids", [])
    if not email_ids:
        return JSONResponse({"error": "email_ids required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        for eid in email_ids[:100]:
            await db.execute(sa_text("""
                UPDATE email_routing_queue SET task_extraction_status = 'dismissed'
                WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
                  AND task_extraction_status IN ('flagged', 'extracted')
            """), {"eid": eid, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "dismissed": len(email_ids)})


# ── Trigger scan ─────────────────────────────────────────────────────────

@router.post("/scan")
async def task_recon_trigger_scan(request: Request):
    tid = _tid(request)
    try:
        from jobs.email_task_scanner import run_scan
        result = await run_scan(tid, limit=500, days=60)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        logger.exception("Scan trigger failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Created tasks list ───────────────────────────────────────────────────

@router.get("/created")
async def task_recon_created(request: Request, limit: int = 50):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = await db.execute(sa_text("""
            SELECT t.id, t.title, t.description, t.priority, t.status,
                   t.due_date, t.matter_id::text, t.source_ref, t.tags,
                   t.created_at, m.matter_name, m.matter_number
            FROM tasks t LEFT JOIN matters m ON m.id = t.matter_id
            WHERE TRIM(t.tenant_id) = :tid AND t.source = 'ai_email'
            ORDER BY
                CASE t.status WHEN 'open' THEN 1 WHEN 'pending_review' THEN 2 ELSE 3 END,
                t.due_date ASC NULLS LAST, t.created_at DESC
            LIMIT :lim
        """), {"tid": tid, "lim": limit})
        results = []
        import datetime as _dt
        for r in rows.mappings():
            row = dict(r)
            for k, v in row.items():
                if isinstance(v, (_dt.date, _dt.datetime)): row[k] = v.isoformat()
            results.append(row)
    return JSONResponse({"tasks": results, "total": len(results)})


# ── Delete AI task ───────────────────────────────────────────────────────

@router.post("/delete-task")
async def task_recon_delete_task(request: Request):
    tid = _tid(request)
    body = await request.json()
    task_id = body.get("task_id")
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        check = await db.execute(sa_text("""
            SELECT id, source_ref FROM tasks
            WHERE id = :tid_task AND TRIM(tenant_id) = :tid AND source = 'ai_email'
        """), {"tid_task": int(task_id), "tid": tid})
        task_row = check.mappings().fetchone()
        if not task_row:
            return JSONResponse({"error": "Task not found or not AI-generated"}, status_code=404)
        await db.execute(sa_text("""
            DELETE FROM tasks WHERE id = :tid_task AND TRIM(tenant_id) = :tid AND source = 'ai_email'
        """), {"tid_task": int(task_id), "tid": tid})
        source_ref = task_row["source_ref"]
        if source_ref:
            await db.execute(sa_text("""
                UPDATE email_routing_queue SET task_extraction_status = 'extracted'
                WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
                  AND task_extraction_status = 'created'
            """), {"eid": source_ref, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True, "task_id": task_id})
