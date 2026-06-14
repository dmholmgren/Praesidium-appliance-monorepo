"""
Task & Project API Router — v2 (Unit 2 enhancements)

RESTful endpoints for tasks, projects, scopes, and dependencies.

Routes:
  === TASKS ===
  GET    /api/tasks                             list tasks (enhanced filters)
  POST   /api/tasks                             create task (with optional scopes[])
  GET    /api/tasks/my                          current user's tasks
  GET    /api/tasks/{task_id}                   get task detail (includes scopes, deps)
  PUT    /api/tasks/{task_id}                   update task
  DELETE /api/tasks/{task_id}                   soft/hard delete
  PUT    /api/tasks/{task_id}/status            quick status change
  PUT    /api/tasks/{task_id}/assign            assign/reassign
  POST   /api/tasks/{task_id}/complete          mark complete
  POST   /api/tasks/{task_id}/reopen            reopen completed task
  POST   /api/tasks/reorder                     bulk reorder

  === TASK SCOPES ===
  GET    /api/tasks/{task_id}/scopes            list scopes
  POST   /api/tasks/{task_id}/scopes            add scope
  DELETE /api/tasks/{task_id}/scopes/{scope_id} remove scope

  === TASK DEPENDENCIES ===
  GET    /api/tasks/{task_id}/dependencies         list deps (predecessors + successors)
  POST   /api/tasks/{task_id}/dependencies         add dependency
  DELETE /api/tasks/{task_id}/dependencies/{dep_id} remove dependency

  === PROJECTS ===
  GET    /api/projects                          list projects
  POST   /api/projects                          create project
  GET    /api/projects/{project_id}             get project detail
  PUT    /api/projects/{project_id}             update project
  DELETE /api/projects/{project_id}             archive project
  GET    /api/projects/{project_id}/tasks       project tasks with deps + rollup
  GET    /api/projects/{project_id}/summary     rollup stats
  POST   /api/projects/{project_id}/close       close project
  POST   /api/projects/{project_id}/reopen      reopen project
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks-projects"])


# ═══════════════════════════════════════════════════════════════════════════════
#  PROJECTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/projects", response_class=JSONResponse)
async def list_projects(
    request: Request,
    matter_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    tenant_id = _tid(request)
    clauses = ["TRIM(p.tenant_id) = TRIM(:tid)"]
    params = {"tid": tenant_id, "limit": limit, "offset": offset}
    if matter_id:
        clauses.append("p.matter_id = CAST(:matter_id AS uuid)")
        params["matter_id"] = matter_id
    if status:
        clauses.append("p.status = :status")
        params["status"] = status
    where = " AND ".join(clauses)

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text(f"""
            SELECT p.*,
                   m.matter_name, m.matter_number,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.tenant_id = p.tenant_id) as task_count,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.tenant_id = p.tenant_id AND t.status = 'complete') as completed_count,
                   u.full_name as created_by_name
            FROM projects p
            LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN users u ON p.created_by = u.id AND TRIM(p.tenant_id) = TRIM(u.tenant_id)
            WHERE {where}
            ORDER BY p.sort_order ASC, p.created_at DESC
            LIMIT :limit OFFSET :offset
        """), params)
        rows = [dict(r._mapping) for r in result.fetchall()]
    return {"projects": _serialize_rows(rows), "count": len(rows)}


@router.post("/api/projects", response_class=JSONResponse)
async def create_project(request: Request):
    tenant_id = _tid(request)
    user = _user(request)
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    matter_id = body.get("matter_id")
    if not matter_id:
        raise HTTPException(400, "matter_id is required")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            INSERT INTO projects
                (tenant_id, matter_id, title, description, template_type,
                 status, priority, due_date, created_by, lead_attorney_id,
                 members, config, sort_order, project_type, budget_hours, budget_dollars,
                 created_at, updated_at)
            VALUES
                (:tid, CAST(:matter_id AS uuid), :title, :description, :template_type,
                 'active', :priority, :due_date, :created_by, :lead_attorney_id,
                 CAST(:members AS jsonb), CAST(:config AS jsonb), 0,
                 :project_type, :budget_hours, :budget_dollars,
                 NOW(), NOW())
            RETURNING id::text
        """), {
            "tid": tenant_id, "matter_id": matter_id, "title": title,
            "description": body.get("description"),
            "template_type": body.get("template_type"),
            "priority": body.get("priority", "medium"),
            "due_date": _parse_dt(body.get("due_date")),
            "created_by": user.id if user else None,
            "lead_attorney_id": body.get("lead_attorney_id"),
            "members": json.dumps(body.get("members", [])),
            "config": json.dumps(body.get("config", {})),
            "project_type": body.get("project_type", "general"),
            "budget_hours": body.get("budget_hours"),
            "budget_dollars": body.get("budget_dollars"),
        })
        project_id = result.fetchone()[0]
        await session.commit()
    return {"id": project_id, "status": "active"}


@router.get("/api/projects/{project_id}", response_class=JSONResponse)
async def get_project(request: Request, project_id: str):
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT p.*, m.matter_name, m.matter_number,
                   u.full_name as created_by_name,
                   la.full_name as lead_attorney_name
            FROM projects p
            LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN users u ON p.created_by = u.id AND TRIM(p.tenant_id) = TRIM(u.tenant_id)
            LEFT JOIN users la ON p.lead_attorney_id = la.id AND TRIM(p.tenant_id) = TRIM(la.tenant_id)
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = TRIM(:tid)
        """), {"pid": project_id, "tid": tenant_id})
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return {"project": _serialize_row(dict(row))}


@router.put("/api/projects/{project_id}", response_class=JSONResponse)
async def update_project(request: Request, project_id: str):
    tenant_id = _tid(request)
    body = await request.json()
    sets = ["updated_at = NOW()"]
    params = {"pid": project_id, "tid": tenant_id}
    for field in ("title", "description", "template_type", "status", "priority", "project_type"):
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if "due_date" in body:
        sets.append("due_date = :due_date")
        params["due_date"] = _parse_dt(body["due_date"])
    if "lead_attorney_id" in body:
        sets.append("lead_attorney_id = :lead_attorney_id")
        params["lead_attorney_id"] = body["lead_attorney_id"]
    if "members" in body:
        sets.append("members = CAST(:members AS jsonb)")
        params["members"] = json.dumps(body["members"])
    if "budget_hours" in body:
        sets.append("budget_hours = :budget_hours")
        params["budget_hours"] = body["budget_hours"]
    if "budget_dollars" in body:
        sets.append("budget_dollars = :budget_dollars")
        params["budget_dollars"] = body["budget_dollars"]
    if body.get("status") == "completed":
        sets.append("completed_at = NOW()")

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(f"""
            UPDATE projects SET {', '.join(sets)}
            WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
        """), params)
        await session.commit()
    return {"id": project_id, "updated": True}


@router.delete("/api/projects/{project_id}", response_class=JSONResponse)
async def archive_project(request: Request, project_id: str):
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE projects SET status = 'archived', updated_at = NOW()
            WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
        """), {"pid": project_id, "tid": tenant_id})
        await session.commit()
    return {"id": project_id, "status": "archived"}


# ─── Project PM Endpoints (Unit 2) ────────────────────────────────────────────

@router.get("/api/projects/{project_id}/tasks", response_class=JSONResponse)
async def project_tasks(request: Request, project_id: str):
    """All tasks for a project, with dependencies and scope info."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT t.*, m.matter_name,
                   STRING_AGG(DISTINCT u.full_name, ', ') as assignee_names,
                   creator.full_name as created_by_name,
                   (SELECT COUNT(*) FROM tasks sub WHERE sub.parent_task_id = t.id) as subtask_count
            FROM tasks t
            LEFT JOIN matters m ON t.matter_id = m.id AND TRIM(t.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN task_assignments ta ON t.id = ta.task_id AND TRIM(t.tenant_id) = TRIM(ta.tenant_id)
            LEFT JOIN users u ON ta.user_id = u.id AND TRIM(ta.tenant_id) = TRIM(u.tenant_id)
            LEFT JOIN users creator ON t.created_by = creator.id AND TRIM(t.tenant_id) = TRIM(creator.tenant_id)
            WHERE t.project_id = CAST(:pid AS uuid) AND TRIM(t.tenant_id) = TRIM(:tid)
            GROUP BY t.id, m.matter_name, creator.full_name
            ORDER BY t.sort_order ASC, t.due_date ASC NULLS LAST
        """), {"pid": project_id, "tid": tenant_id})
        tasks = [dict(r._mapping) for r in result.fetchall()]

        # Fetch dependencies for all project tasks
        task_ids = [t["id"] for t in tasks]
        deps = []
        if task_ids:
            dep_result = await session.execute(sa_text("""
                SELECT td.*, pt.title as predecessor_title, st.title as successor_title
                FROM task_dependencies td
                LEFT JOIN tasks pt ON td.depends_on_id = pt.id
                LEFT JOIN tasks st ON td.task_id = st.id
                WHERE TRIM(td.tenant_id) = TRIM(:tid)
                  AND (td.task_id = ANY(:ids) OR td.depends_on_id = ANY(:ids))
            """), {"tid": tenant_id, "ids": task_ids})
            deps = [dict(r._mapping) for r in dep_result.fetchall()]

    return {
        "tasks": _serialize_rows(tasks),
        "dependencies": _serialize_rows(deps),
        "count": len(tasks),
    }


@router.get("/api/projects/{project_id}/summary", response_class=JSONResponse)
async def project_summary(request: Request, project_id: str):
    """Project rollup: % complete, budget vs actual, overdue count, milestone status."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT
                p.id, p.title, p.status, p.budget_hours, p.budget_dollars,
                p.due_date, p.project_type,
                COUNT(t.id) as total_tasks,
                COUNT(t.id) FILTER (WHERE t.status = 'complete') as completed_tasks,
                COUNT(t.id) FILTER (WHERE t.status NOT IN ('complete','cancelled','deleted')
                    AND t.due_date < NOW()) as overdue_tasks,
                COUNT(t.id) FILTER (WHERE t.status = 'in_progress') as in_progress_tasks,
                COALESCE(AVG(t.completion_pct) FILTER (WHERE t.status != 'cancelled'), 0) as avg_completion_pct,
                COALESCE(SUM(t.estimated_minutes), 0) as total_estimated_minutes,
                COALESCE(SUM(t.actual_minutes), 0) as total_actual_minutes
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id AND TRIM(t.tenant_id) = TRIM(p.tenant_id)
                AND t.status != 'deleted'
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = TRIM(:tid)
            GROUP BY p.id
        """), {"pid": project_id, "tid": tenant_id})
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(404, "Project not found")

    d = dict(row)
    total = d["total_tasks"] or 0
    completed = d["completed_tasks"] or 0
    pct = round((completed / total) * 100, 1) if total > 0 else 0.0

    return {
        "project_id": str(d["id"]),
        "title": d["title"],
        "status": d["status"],
        "project_type": d["project_type"],
        "percent_complete": pct,
        "avg_task_completion": round(float(d["avg_completion_pct"]), 1),
        "total_tasks": total,
        "completed_tasks": completed,
        "in_progress_tasks": d["in_progress_tasks"] or 0,
        "overdue_tasks": d["overdue_tasks"] or 0,
        "budget_hours": float(d["budget_hours"]) if d["budget_hours"] else None,
        "budget_dollars": float(d["budget_dollars"]) if d["budget_dollars"] else None,
        "actual_hours": round((d["total_actual_minutes"] or 0) / 60, 2),
        "estimated_hours": round((d["total_estimated_minutes"] or 0) / 60, 2),
        "due_date": d["due_date"].isoformat() if d["due_date"] else None,
    }


@router.post("/api/projects/{project_id}/close", response_class=JSONResponse)
async def close_project(request: Request, project_id: str):
    """Close a project with reason and notes."""
    tenant_id = _tid(request)
    user = _user(request)
    body = await request.json()

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE projects SET
                status = 'closed',
                closed_at = NOW(),
                closed_by = :closed_by,
                closure_reason = :reason,
                closure_notes = :notes,
                updated_at = NOW()
            WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
              AND status != 'closed'
        """), {
            "pid": project_id, "tid": tenant_id,
            "closed_by": user.id if user else None,
            "reason": body.get("reason", "completed"),
            "notes": body.get("notes"),
        })
        await session.commit()
    return {"id": project_id, "status": "closed"}


@router.post("/api/projects/{project_id}/reopen", response_class=JSONResponse)
async def reopen_project(request: Request, project_id: str):
    """Reopen a closed project."""
    tenant_id = _tid(request)
    user = _user(request)

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE projects SET
                status = 'active',
                reopened_at = NOW(),
                reopened_by = :reopened_by,
                closed_at = NULL,
                closed_by = NULL,
                closure_reason = NULL,
                closure_notes = NULL,
                updated_at = NOW()
            WHERE id = CAST(:pid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
              AND status = 'closed'
        """), {
            "pid": project_id, "tid": tenant_id,
            "reopened_by": user.id if user else None,
        })
        await session.commit()
    return {"id": project_id, "status": "active"}


# ═══════════════════════════════════════════════════════════════════════════════
#  TASKS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/tasks", response_class=JSONResponse)
async def list_tasks(
    request: Request,
    matter_id: Optional[str] = None,
    project_id: Optional[str] = None,
    assignee_id: Optional[int] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    scope_type: Optional[str] = None,
    scope_id: Optional[str] = None,
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    tenant_id = _tid(request)
    clauses = ["TRIM(t.tenant_id) = TRIM(:tid)", "t.status != 'deleted'"]
    params = {"tid": tenant_id, "limit": limit, "offset": offset}
    if matter_id:
        clauses.append("t.matter_id = CAST(:matter_id AS uuid)")
        params["matter_id"] = matter_id
    if project_id:
        clauses.append("t.project_id = CAST(:project_id AS uuid)")
        params["project_id"] = project_id
    if assignee_id:
        clauses.append("EXISTS (SELECT 1 FROM task_assignments ta WHERE ta.task_id = t.id AND ta.user_id = :assignee_id)")
        params["assignee_id"] = assignee_id
    if status:
        clauses.append("t.status = :status")
        params["status"] = status
    if priority:
        clauses.append("t.priority = :priority")
        params["priority"] = priority
    if scope_type and scope_id:
        clauses.append("""EXISTS (SELECT 1 FROM task_scopes ts
            WHERE ts.task_id = t.id AND TRIM(ts.tenant_id) = TRIM(:tid)
            AND ts.scope_type = :scope_type AND ts.scope_id = :scope_id)""")
        params["scope_type"] = scope_type
        params["scope_id"] = scope_id
    elif scope_type:
        clauses.append("""EXISTS (SELECT 1 FROM task_scopes ts
            WHERE ts.task_id = t.id AND TRIM(ts.tenant_id) = TRIM(:tid)
            AND ts.scope_type = :scope_type)""")
        params["scope_type"] = scope_type
    if due_before:
        clauses.append("t.due_date <= :due_before")
        params["due_before"] = _parse_dt(due_before)
    if due_after:
        clauses.append("t.due_date >= :due_after")
        params["due_after"] = _parse_dt(due_after)

    where = " AND ".join(clauses)

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text(f"""
            SELECT t.*, m.matter_name, p.title as project_title,
                   STRING_AGG(DISTINCT u.full_name, ', ') as assignee_names,
                   STRING_AGG(DISTINCT u.id::text, ',') as assignee_ids,
                   creator.full_name as created_by_name,
                   (SELECT COUNT(*) FROM tasks sub WHERE sub.parent_task_id = t.id) as subtask_count
            FROM tasks t
            LEFT JOIN matters m ON t.matter_id = m.id AND TRIM(t.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN projects p ON t.project_id = p.id
            LEFT JOIN task_assignments ta ON t.id = ta.task_id AND TRIM(t.tenant_id) = TRIM(ta.tenant_id)
            LEFT JOIN users u ON ta.user_id = u.id AND TRIM(ta.tenant_id) = TRIM(u.tenant_id)
            LEFT JOIN users creator ON t.created_by = creator.id AND TRIM(t.tenant_id) = TRIM(creator.tenant_id)
            WHERE {where}
            GROUP BY t.id, m.matter_name, p.title, creator.full_name
            ORDER BY t.sort_order ASC, t.due_date ASC NULLS LAST, t.created_at DESC
            LIMIT :limit OFFSET :offset
        """), params)
        rows = [dict(r._mapping) for r in result.fetchall()]
    return {"tasks": _serialize_rows(rows), "count": len(rows)}


@router.post("/api/tasks", response_class=JSONResponse)
async def create_task(request: Request):
    tenant_id = _tid(request)
    user = _user(request)
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "title is required")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            INSERT INTO tasks
                (tenant_id, matter_id, project_id, title, description,
                 source, priority, status, task_type, due_date, start_date,
                 estimated_minutes, parent_task_id, tags, sort_order,
                 assignee_user_id, client_visible, notes, completion_pct,
                 created_by, created_at, updated_at)
            VALUES
                (:tid, CAST(:matter_id AS uuid), CAST(:project_id AS uuid), :title, :description,
                 :source, :priority, 'open', :task_type, :due_date, :start_date,
                 :estimated_minutes, :parent_task_id, CAST(:tags AS jsonb), :sort_order,
                 :assignee_user_id, :client_visible, :notes, 0,
                 :created_by, NOW(), NOW())
            RETURNING id
        """), {
            "tid": tenant_id, "matter_id": body.get("matter_id"),
            "project_id": body.get("project_id"), "title": title,
            "description": body.get("description"),
            "source": body.get("source", "manual"),
            "priority": body.get("priority", "medium"),
            "task_type": body.get("task_type", "general"),
            "due_date": _parse_dt(body.get("due_date")),
            "start_date": _parse_dt(body.get("start_date")),
            "estimated_minutes": body.get("estimated_minutes"),
            "parent_task_id": body.get("parent_task_id"),
            "tags": json.dumps(body.get("tags", [])),
            "sort_order": body.get("sort_order", 0),
            "assignee_user_id": body.get("assignee_user_id"),
            "client_visible": body.get("client_visible", False),
            "notes": body.get("notes"),
            "created_by": user.id if user else None,
        })
        task_id = result.fetchone()[0]

        # Legacy: task_assignments
        assigned_to = body.get("assigned_to", [])
        if isinstance(assigned_to, int):
            assigned_to = [assigned_to]
        for uid in assigned_to:
            await session.execute(sa_text("""
                INSERT INTO task_assignments
                    (tenant_id, task_id, user_id, assigned_by, assigned_at)
                VALUES (:tid, :task_id, :uid, :assigned_by, NOW())
            """), {"tid": tenant_id, "task_id": task_id, "uid": uid,
                   "assigned_by": user.id if user else None})

        # Scopes (Unit 2)
        scopes = body.get("scopes", [])
        for i, scope in enumerate(scopes):
            st = scope.get("scope_type")
            si = scope.get("scope_id")
            if st and si:
                await session.execute(sa_text("""
                    INSERT INTO task_scopes (tenant_id, task_id, scope_type, scope_id, is_primary)
                    VALUES (:tid, :task_id, :scope_type, :scope_id, :is_primary)
                    ON CONFLICT (tenant_id, task_id, scope_type, scope_id) DO NOTHING
                """), {
                    "tid": tenant_id, "task_id": task_id,
                    "scope_type": st, "scope_id": si,
                    "is_primary": scope.get("is_primary", i == 0),
                })

        await session.commit()
    return {"id": task_id, "status": "open"}


@router.get("/api/tasks/my", response_class=JSONResponse)
async def my_tasks(request: Request):
    tenant_id = _tid(request)
    user = _user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT t.*, m.matter_name, m.matter_number, p.title as project_title
            FROM tasks t
            JOIN task_assignments ta ON t.id = ta.task_id AND TRIM(t.tenant_id) = TRIM(ta.tenant_id)
            LEFT JOIN matters m ON t.matter_id = m.id AND TRIM(t.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE TRIM(t.tenant_id) = TRIM(:tid) AND ta.user_id = :uid
              AND t.status NOT IN ('complete', 'cancelled', 'deleted')
            ORDER BY
              CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
              t.due_date ASC NULLS LAST, t.created_at DESC
        """), {"tid": tenant_id, "uid": user.id})
        rows = [dict(r._mapping) for r in result.fetchall()]
    return {"tasks": _serialize_rows(rows), "count": len(rows)}


@router.get("/api/tasks/{task_id}", response_class=JSONResponse)
async def get_task(request: Request, task_id: int):
    """Task detail — includes scopes and dependencies."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        # Main task
        result = await session.execute(sa_text("""
            SELECT t.*, m.matter_name, m.matter_number, p.title as project_title,
                   creator.full_name as created_by_name,
                   STRING_AGG(DISTINCT u.full_name, ', ') as assignee_names,
                   STRING_AGG(DISTINCT u.id::text, ',') as assignee_ids
            FROM tasks t
            LEFT JOIN matters m ON t.matter_id = m.id AND TRIM(t.tenant_id) = TRIM(m.tenant_id)
            LEFT JOIN projects p ON t.project_id = p.id
            LEFT JOIN users creator ON t.created_by = creator.id AND TRIM(t.tenant_id) = TRIM(creator.tenant_id)
            LEFT JOIN task_assignments ta ON t.id = ta.task_id AND TRIM(t.tenant_id) = TRIM(ta.tenant_id)
            LEFT JOIN users u ON ta.user_id = u.id AND TRIM(ta.tenant_id) = TRIM(u.tenant_id)
            WHERE t.id = :task_id AND TRIM(t.tenant_id) = TRIM(:tid)
            GROUP BY t.id, m.matter_name, m.matter_number, p.title, creator.full_name
        """), {"task_id": task_id, "tid": tenant_id})
        row = result.mappings().fetchone()
        if not row:
            raise HTTPException(404, "Task not found")

        # Scopes
        scope_result = await session.execute(sa_text("""
            SELECT ts.id, ts.scope_type, ts.scope_id, ts.is_primary, ts.created_at,
                   CASE ts.scope_type
                       WHEN 'matter' THEN (SELECT matter_name FROM matters WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       WHEN 'project' THEN (SELECT title FROM projects WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       WHEN 'workspace' THEN (SELECT title FROM meeting_workspaces WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       ELSE NULL
                   END as scope_name
            FROM task_scopes ts
            WHERE ts.task_id = :task_id AND TRIM(ts.tenant_id) = TRIM(:tid)
            ORDER BY ts.is_primary DESC, ts.created_at ASC
        """), {"task_id": task_id, "tid": tenant_id})
        scopes = [dict(r._mapping) for r in scope_result.fetchall()]

        # Dependencies — predecessors (tasks this depends on)
        pred_result = await session.execute(sa_text("""
            SELECT td.id as dep_id, td.depends_on_id as task_id, td.dependency_type, td.lag_days,
                   pt.title, pt.status, pt.due_date
            FROM task_dependencies td
            JOIN tasks pt ON td.depends_on_id = pt.id
            WHERE td.task_id = :task_id AND TRIM(td.tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        predecessors = [dict(r._mapping) for r in pred_result.fetchall()]

        # Dependencies — successors (tasks that depend on this)
        succ_result = await session.execute(sa_text("""
            SELECT td.id as dep_id, td.task_id, td.dependency_type, td.lag_days,
                   st.title, st.status, st.due_date
            FROM task_dependencies td
            JOIN tasks st ON td.task_id = st.id
            WHERE td.depends_on_id = :task_id AND TRIM(td.tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        successors = [dict(r._mapping) for r in succ_result.fetchall()]

        # Subtasks
        sub_result = await session.execute(sa_text("""
            SELECT id, title, status, priority, due_date, completion_pct, sort_order
            FROM tasks
            WHERE parent_task_id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
              AND status != 'deleted'
            ORDER BY sort_order ASC, due_date ASC NULLS LAST
        """), {"task_id": task_id, "tid": tenant_id})
        subtasks = [dict(r._mapping) for r in sub_result.fetchall()]

    task_data = _serialize_row(dict(row))
    task_data["scopes"] = _serialize_rows(scopes)
    task_data["predecessors"] = _serialize_rows(predecessors)
    task_data["successors"] = _serialize_rows(successors)
    task_data["subtasks"] = _serialize_rows(subtasks)
    return {"task": task_data}


@router.put("/api/tasks/{task_id}", response_class=JSONResponse)
async def update_task(request: Request, task_id: int):
    tenant_id = _tid(request)
    body = await request.json()
    sets = ["updated_at = NOW()"]
    params = {"task_id": task_id, "tid": tenant_id}
    for field in ("title", "description", "priority", "status", "task_type", "source", "notes"):
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if "due_date" in body:
        sets.append("due_date = :due_date")
        params["due_date"] = _parse_dt(body["due_date"])
    if "start_date" in body:
        sets.append("start_date = :start_date")
        params["start_date"] = _parse_dt(body["start_date"])
    if "estimated_minutes" in body:
        sets.append("estimated_minutes = :estimated_minutes")
        params["estimated_minutes"] = body["estimated_minutes"]
    if "actual_minutes" in body:
        sets.append("actual_minutes = :actual_minutes")
        params["actual_minutes"] = body["actual_minutes"]
    if "completion_pct" in body:
        sets.append("completion_pct = :completion_pct")
        params["completion_pct"] = body["completion_pct"]
    if "project_id" in body:
        sets.append("project_id = CAST(:project_id AS uuid)")
        params["project_id"] = body["project_id"]
    if "matter_id" in body:
        if body["matter_id"]:
            sets.append("matter_id = CAST(:matter_id AS uuid)")
            params["matter_id"] = body["matter_id"]
        else:
            sets.append("matter_id = NULL")
    if "assignee_user_id" in body:
        if body["assignee_user_id"]:
            sets.append("assignee_user_id = :assignee_user_id")
            params["assignee_user_id"] = body["assignee_user_id"]
        else:
            sets.append("assignee_user_id = NULL")
    if "client_visible" in body:
        sets.append("client_visible = :client_visible")
        params["client_visible"] = body["client_visible"]
    if "tags" in body:
        sets.append("tags = CAST(:tags AS jsonb)")
        params["tags"] = json.dumps(body["tags"])
    if body.get("status") == "complete":
        sets.append("completed_at = NOW()")
        sets.append("completion_pct = 100")
    elif body.get("status") == "in_progress":
        sets.append("started_at = COALESCE(started_at, NOW())")

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(f"""
            UPDATE tasks SET {', '.join(sets)}
            WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
        """), params)
        await session.commit()
    return {"id": task_id, "updated": True}


@router.delete("/api/tasks/{task_id}", response_class=JSONResponse)
async def delete_task(request: Request, task_id: int):
    """Soft-delete manual tasks (set status=deleted), hard-delete AI-generated tasks."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        # Check source
        result = await session.execute(sa_text("""
            SELECT source FROM tasks
            WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(404, "Task not found")

        source = row[0]
        if source and source.startswith("ai_"):
            # Hard delete AI tasks + their scopes/deps (CASCADE)
            await session.execute(sa_text("""
                DELETE FROM tasks WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            """), {"task_id": task_id, "tid": tenant_id})
        else:
            # Soft delete
            await session.execute(sa_text("""
                UPDATE tasks SET status = 'deleted', updated_at = NOW()
                WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            """), {"task_id": task_id, "tid": tenant_id})
        await session.commit()
    return {"id": task_id, "deleted": True}


@router.put("/api/tasks/{task_id}/status", response_class=JSONResponse)
async def update_task_status(request: Request, task_id: int):
    tenant_id = _tid(request)
    body = await request.json()
    new_status = body.get("status", "").strip()
    if not new_status:
        raise HTTPException(400, "status is required")
    sets = ["status = :status", "updated_at = NOW()"]
    params = {"task_id": task_id, "tid": tenant_id, "status": new_status}
    if new_status == "complete":
        sets.append("completed_at = NOW()")
        sets.append("completion_pct = 100")
    elif new_status == "in_progress":
        sets.append("started_at = COALESCE(started_at, NOW())")

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text(f"""
            UPDATE tasks SET {', '.join(sets)}
            WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
        """), params)
        await session.commit()
    return {"id": task_id, "status": new_status}


@router.put("/api/tasks/{task_id}/assign", response_class=JSONResponse)
async def assign_task(request: Request, task_id: int):
    tenant_id = _tid(request)
    user = _user(request)
    body = await request.json()
    user_ids = body.get("user_ids", [])

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            DELETE FROM task_assignments WHERE task_id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        for uid in user_ids:
            await session.execute(sa_text("""
                INSERT INTO task_assignments (tenant_id, task_id, user_id, assigned_by, assigned_at)
                VALUES (:tid, :task_id, :uid, :assigned_by, NOW())
            """), {"tid": tenant_id, "task_id": task_id, "uid": uid,
                   "assigned_by": user.id if user else None})

        # Also set assignee_user_id to first user (primary)
        if user_ids:
            await session.execute(sa_text("""
                UPDATE tasks SET assignee_user_id = :uid, updated_at = NOW()
                WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            """), {"uid": user_ids[0], "task_id": task_id, "tid": tenant_id})

        await session.commit()
    return {"id": task_id, "assigned_to": user_ids}


@router.post("/api/tasks/{task_id}/complete", response_class=JSONResponse)
async def complete_task(request: Request, task_id: int):
    """Mark task complete — sets status, completed_at, and completion_pct=100."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            UPDATE tasks SET
                status = 'complete',
                completed_at = NOW(),
                completion_pct = 100,
                updated_at = NOW()
            WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
              AND status NOT IN ('complete', 'deleted')
            RETURNING id
        """), {"task_id": task_id, "tid": tenant_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(404, "Task not found or already complete")
        await session.commit()
    return {"id": task_id, "status": "complete"}


@router.post("/api/tasks/{task_id}/reopen", response_class=JSONResponse)
async def reopen_task(request: Request, task_id: int):
    """Reopen a completed task — clears completed_at, resets completion_pct to 0."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            UPDATE tasks SET
                status = 'open',
                completed_at = NULL,
                completion_pct = 0,
                updated_at = NOW()
            WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
              AND status = 'complete'
            RETURNING id
        """), {"task_id": task_id, "tid": tenant_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(404, "Task not found or not complete")
        await session.commit()
    return {"id": task_id, "status": "open"}


@router.post("/api/tasks/reorder", response_class=JSONResponse)
async def reorder_tasks(request: Request):
    tenant_id = _tid(request)
    body = await request.json()
    items = body.get("items", [])
    async with AsyncSessionLocal() as session:
        for item in items:
            await session.execute(sa_text("""
                UPDATE tasks SET sort_order = :sort_order, updated_at = NOW()
                WHERE id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            """), {"sort_order": item["sort_order"], "task_id": item["id"], "tid": tenant_id})
        await session.commit()
    return {"reordered": len(items)}


# ═══════════════════════════════════════════════════════════════════════════════
#  TASK SCOPES (Unit 2)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/tasks/{task_id}/scopes", response_class=JSONResponse)
async def list_task_scopes(request: Request, task_id: int):
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT ts.id, ts.scope_type, ts.scope_id, ts.is_primary, ts.created_at,
                   CASE ts.scope_type
                       WHEN 'matter' THEN (SELECT matter_name FROM matters WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       WHEN 'project' THEN (SELECT title FROM projects WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       WHEN 'workspace' THEN (SELECT title FROM meeting_workspaces WHERE id = CAST(ts.scope_id AS uuid) LIMIT 1)
                       ELSE NULL
                   END as scope_name
            FROM task_scopes ts
            WHERE ts.task_id = :task_id AND TRIM(ts.tenant_id) = TRIM(:tid)
            ORDER BY ts.is_primary DESC, ts.created_at ASC
        """), {"task_id": task_id, "tid": tenant_id})
        rows = [dict(r._mapping) for r in result.fetchall()]
    return {"scopes": _serialize_rows(rows)}


@router.post("/api/tasks/{task_id}/scopes", response_class=JSONResponse)
async def add_task_scope(request: Request, task_id: int):
    tenant_id = _tid(request)
    body = await request.json()
    scope_type = body.get("scope_type", "").strip()
    scope_id = body.get("scope_id", "").strip()
    if not scope_type or not scope_id:
        raise HTTPException(400, "scope_type and scope_id are required")
    if scope_type not in ("project", "workspace", "matter", "person"):
        raise HTTPException(400, "scope_type must be project, workspace, matter, or person")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            INSERT INTO task_scopes (tenant_id, task_id, scope_type, scope_id, is_primary)
            VALUES (:tid, :task_id, :scope_type, :scope_id, :is_primary)
            ON CONFLICT (tenant_id, task_id, scope_type, scope_id) DO NOTHING
            RETURNING id
        """), {
            "tid": tenant_id, "task_id": task_id,
            "scope_type": scope_type, "scope_id": scope_id,
            "is_primary": body.get("is_primary", False),
        })
        row = result.fetchone()
        await session.commit()
        if not row:
            return {"id": None, "message": "Scope already exists"}
    return {"id": row[0], "task_id": task_id, "scope_type": scope_type, "scope_id": scope_id}


@router.delete("/api/tasks/{task_id}/scopes/{scope_id}", response_class=JSONResponse)
async def remove_task_scope(request: Request, task_id: int, scope_id: int):
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            DELETE FROM task_scopes
            WHERE id = :scope_id AND task_id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            RETURNING id
        """), {"scope_id": scope_id, "task_id": task_id, "tid": tenant_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(404, "Scope not found")
        await session.commit()
    return {"deleted": True, "scope_id": scope_id}


# ═══════════════════════════════════════════════════════════════════════════════
#  TASK DEPENDENCIES (Unit 2)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/tasks/{task_id}/dependencies", response_class=JSONResponse)
async def list_task_dependencies(request: Request, task_id: int):
    """List both predecessors (tasks this depends on) and successors (tasks depending on this)."""
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        # Predecessors
        pred_result = await session.execute(sa_text("""
            SELECT td.id as dep_id, td.depends_on_id as task_id, td.dependency_type, td.lag_days,
                   pt.title, pt.status, pt.due_date
            FROM task_dependencies td
            JOIN tasks pt ON td.depends_on_id = pt.id
            WHERE td.task_id = :task_id AND TRIM(td.tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        predecessors = [dict(r._mapping) for r in pred_result.fetchall()]

        # Successors
        succ_result = await session.execute(sa_text("""
            SELECT td.id as dep_id, td.task_id, td.dependency_type, td.lag_days,
                   st.title, st.status, st.due_date
            FROM task_dependencies td
            JOIN tasks st ON td.task_id = st.id
            WHERE td.depends_on_id = :task_id AND TRIM(td.tenant_id) = TRIM(:tid)
        """), {"task_id": task_id, "tid": tenant_id})
        successors = [dict(r._mapping) for r in succ_result.fetchall()]

    return {
        "predecessors": _serialize_rows(predecessors),
        "successors": _serialize_rows(successors),
    }


@router.post("/api/tasks/{task_id}/dependencies", response_class=JSONResponse)
async def add_task_dependency(request: Request, task_id: int):
    tenant_id = _tid(request)
    body = await request.json()
    depends_on_id = body.get("depends_on_id")
    if not depends_on_id:
        raise HTTPException(400, "depends_on_id is required")
    if int(depends_on_id) == task_id:
        raise HTTPException(400, "A task cannot depend on itself")

    dep_type = body.get("dependency_type", "finish_start")
    if dep_type not in ("finish_start", "start_start", "finish_finish", "start_finish"):
        raise HTTPException(400, "Invalid dependency_type")

    async with AsyncSessionLocal() as session:
        # Verify both tasks exist and belong to this tenant
        check = await session.execute(sa_text("""
            SELECT COUNT(*) FROM tasks
            WHERE id IN (:t1, :t2) AND TRIM(tenant_id) = TRIM(:tid)
        """), {"t1": task_id, "t2": int(depends_on_id), "tid": tenant_id})
        if check.fetchone()[0] < 2:
            raise HTTPException(404, "One or both tasks not found")

        result = await session.execute(sa_text("""
            INSERT INTO task_dependencies (tenant_id, task_id, depends_on_id, dependency_type, lag_days)
            VALUES (:tid, :task_id, :depends_on_id, :dep_type, :lag_days)
            ON CONFLICT (tenant_id, task_id, depends_on_id) DO NOTHING
            RETURNING id
        """), {
            "tid": tenant_id, "task_id": task_id,
            "depends_on_id": int(depends_on_id),
            "dep_type": dep_type,
            "lag_days": body.get("lag_days", 0),
        })
        row = result.fetchone()
        await session.commit()
        if not row:
            return {"id": None, "message": "Dependency already exists"}
    return {"id": row[0], "task_id": task_id, "depends_on_id": depends_on_id}


@router.delete("/api/tasks/{task_id}/dependencies/{dep_id}", response_class=JSONResponse)
async def remove_task_dependency(request: Request, task_id: int, dep_id: int):
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            DELETE FROM task_dependencies
            WHERE id = :dep_id AND task_id = :task_id AND TRIM(tenant_id) = TRIM(:tid)
            RETURNING id
        """), {"dep_id": dep_id, "task_id": task_id, "tid": tenant_id})
        row = result.fetchone()
        if not row:
            raise HTTPException(404, "Dependency not found")
        await session.commit()
    return {"deleted": True, "dep_id": dep_id}


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _tid(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()

def _user(request: Request):
    return getattr(request.state, "current_user", None)

def _parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

def _serialize_rows(rows):
    return [_serialize_row(r) for r in rows]

def _serialize_row(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "hex"):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  PROJECT TEMPLATES + GANTT (Unit 7)
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import date as _g_date, time as _g_time, timedelta as _g_td
from math import ceil as _g_ceil

_G_HOURS_PER_DAY = 8


def _g_next_busday(d):
    while d.weekday() >= 5:
        d += _g_td(days=1)
    return d


def _g_add_busdays(d, n):
    while n > 0:
        d += _g_td(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _g_sub_busdays(d, n):
    while n > 0:
        d -= _g_td(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _g_busdays_between(a, b):
    # inclusive busday count a..b; a,b assumed busdays, a <= b
    n, d = 0, a
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += _g_td(days=1)
    return max(1, n)


def _g_parse_refs(raw):
    # dependency_refs jsonb -> [(ref_id, lag_days)]
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    out = []
    for e in (raw or []):
        if isinstance(e, dict):
            out.append((e.get("ref"), int(e.get("lag_days", 0))))
        else:
            out.append((e, 0))
    return out


@router.post("/api/projects/from-template", response_class=JSONResponse)
async def create_project_from_template(request: Request):
    # Instantiate a project template: project + tasks + dependencies + scopes,
    # forward-scheduled through the dependency graph (8 productive hrs/day,
    # business days, parallel branches overlap).
    tenant_id = _tid(request)
    user = _user(request)
    body = await request.json()

    matter_id = body.get("matter_id")
    if not matter_id:
        raise HTTPException(400, "matter_id is required")
    template_id = body.get("template_id", "a0000001-0000-0000-0000-000000000001")
    try:
        proj_start = (_g_date.fromisoformat(body["start_date"])
                      if body.get("start_date") else _g_date.today())
    except (ValueError, TypeError):
        raise HTTPException(400, "start_date must be YYYY-MM-DD")
    proj_start = _g_next_busday(proj_start)

    async with AsyncSessionLocal() as session:
        res = await session.execute(sa_text("""
            SELECT id, name, description, project_type,
                   default_budget_hours, default_budget_dollars
            FROM project_templates WHERE id = CAST(:tplid AS uuid)
        """), {"tplid": template_id})
        tpl = res.mappings().fetchone()
        if not tpl:
            raise HTTPException(404, "Template not found")

        res = await session.execute(sa_text("""
            SELECT id, title, description, task_type, phase, sort_order,
                   estimated_minutes, priority, default_lag_days, dependency_refs
            FROM project_template_tasks
            WHERE template_id = CAST(:tplid AS uuid)
            ORDER BY sort_order, id
        """), {"tplid": template_id})
        rows = res.mappings().fetchall()
        if not rows:
            raise HTTPException(422, "Template has no tasks")

        tt = {r["id"]: dict(r) for r in rows}
        deps = {}
        for r in rows:
            deps[r["id"]] = [(ref, lag) for ref, lag in _g_parse_refs(r["dependency_refs"])
                             if ref in tt]

        # Kahn topological sort (sort_order tie-break)
        indeg = {k: len(v) for k, v in deps.items()}
        succ = {k: [] for k in deps}
        for k, v in deps.items():
            for ref, _lag in v:
                succ[ref].append(k)
        ready = sorted([k for k, d0 in indeg.items() if d0 == 0],
                       key=lambda k: tt[k]["sort_order"])
        order = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    ready.append(s)
            ready.sort(key=lambda k: tt[k]["sort_order"])
        if len(order) != len(deps):
            raise HTTPException(422, "Template dependency graph contains a cycle")

        # forward schedule
        sched = {}
        for n in order:
            est = tt[n]["estimated_minutes"] or 60
            dur = max(1, _g_ceil(est / 60 / _G_HOURS_PER_DAY))
            start = proj_start
            for ref, lag in deps[n]:
                cand = _g_add_busdays(sched[ref][1], 1 + max(0, lag))
                if cand > start:
                    start = cand
            start = _g_next_busday(start)
            finish = _g_add_busdays(start, dur - 1)
            sched[n] = (start, finish, dur)
        proj_end = max(f for (_s, f, _d) in sched.values())

        total_hours = round(sum((tt[n]["estimated_minutes"] or 60) for n in order) / 60, 2)
        budget_hours = (float(tpl["default_budget_hours"])
                        if tpl["default_budget_hours"] is not None else total_hours)
        title = (body.get("title") or "").strip() or tpl["name"]

        res = await session.execute(sa_text("""
            INSERT INTO projects
                (tenant_id, matter_id, title, description, template_type,
                 status, priority, due_date, created_by, lead_attorney_id,
                 members, config, sort_order, project_type, budget_hours, budget_dollars,
                 created_at, updated_at)
            VALUES
                (:tid, CAST(:matter_id AS uuid), :title, :description, :template_type,
                 'active', :priority, :due_date, :created_by, :lead_attorney_id,
                 CAST(:members AS jsonb), CAST(:config AS jsonb), 0,
                 :project_type, :budget_hours, :budget_dollars, NOW(), NOW())
            RETURNING id::text
        """), {
            "tid": tenant_id, "matter_id": matter_id, "title": title,
            "description": body.get("description") or tpl["description"],
            "template_type": "pm",
            "priority": body.get("priority", "medium"),
            "due_date": datetime.combine(proj_end, _g_time(17, 0)),
            "created_by": user.id if user else None,
            "lead_attorney_id": body.get("lead_attorney_id"),
            "members": json.dumps(body.get("members", [])),
            "config": json.dumps({"source_template_id": str(tpl["id"]),
                                  "scheduled_start": proj_start.isoformat(),
                                  "hours_per_day": _G_HOURS_PER_DAY}),
            "project_type": tpl["project_type"] or "litigation",
            "budget_hours": budget_hours,
            "budget_dollars": (float(tpl["default_budget_dollars"])
                               if tpl["default_budget_dollars"] is not None else None),
        })
        project_id = res.fetchone()[0]

        ref_to_id = {}
        for n in order:
            t = tt[n]
            start, finish, _dur = sched[n]
            res = await session.execute(sa_text("""
                INSERT INTO tasks
                    (tenant_id, matter_id, project_id, title, description,
                     source, source_ref, priority, status, due_date, start_date,
                     estimated_minutes, task_type, sort_order, tags, created_by,
                     created_at, updated_at)
                VALUES
                    (:tid, CAST(:matter_id AS uuid), CAST(:pid AS uuid), :title, :description,
                     'template', :source_ref, :priority, 'open', :due_date, :start_date,
                     :est, :task_type, :sort_order, CAST(:tags AS jsonb), :created_by,
                     NOW(), NOW())
                RETURNING id
            """), {
                "tid": tenant_id, "matter_id": matter_id, "pid": project_id,
                "title": t["title"], "description": t["description"],
                "source_ref": "tpl:" + str(n),
                "priority": t["priority"] or "medium",
                "due_date": datetime.combine(finish, _g_time(17, 0)),
                "start_date": start,
                "est": t["estimated_minutes"],
                "task_type": t["task_type"] or "general",
                "sort_order": t["sort_order"],
                "tags": json.dumps({"phase": t["phase"]} if t["phase"] else {}),
                "created_by": user.id if user else None,
            })
            new_id = res.fetchone()[0]
            ref_to_id[n] = new_id
            await session.execute(sa_text("""
                INSERT INTO task_scopes
                    (tenant_id, task_id, scope_type, scope_id, is_primary, created_at)
                VALUES (:tid, :task_id, 'project', :scope_id, true, NOW())
                ON CONFLICT DO NOTHING
            """), {"tid": tenant_id, "task_id": new_id, "scope_id": str(project_id)})

        dep_count = 0
        for n in order:
            for ref, lag in deps[n]:
                await session.execute(sa_text("""
                    INSERT INTO task_dependencies
                        (tenant_id, task_id, depends_on_id, dependency_type, lag_days, created_at)
                    VALUES (:tid, :task_id, :dep_id, 'finish_start', :lag, NOW())
                    ON CONFLICT DO NOTHING
                """), {"tid": tenant_id, "task_id": ref_to_id[n],
                       "dep_id": ref_to_id[ref], "lag": lag})
                dep_count += 1

        await session.commit()

    return {
        "id": project_id,
        "title": title,
        "tasks_created": len(order),
        "dependencies_created": dep_count,
        "scheduled_start": proj_start.isoformat(),
        "scheduled_end": proj_end.isoformat(),
        "budget_hours": budget_hours,
        "total_estimated_hours": total_hours,
    }


@router.get("/api/projects/{project_id}/gantt", response_class=JSONResponse)
async def project_gantt(request: Request, project_id: str):
    # Gantt payload: dated task bars + dependency edges + CPM critical path.
    # Bars render from stored start_date/due_date; criticality is computed by
    # classic CPM over estimated durations + lags (business days), so hand-edited
    # dates do not corrupt the critical path.
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        res = await session.execute(sa_text("""
            SELECT p.id::text AS id, p.title, p.status, p.due_date, p.created_at,
                   p.budget_hours, p.config, m.matter_name
            FROM projects p
            LEFT JOIN matters m ON p.matter_id = m.id AND TRIM(p.tenant_id) = TRIM(m.tenant_id)
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = TRIM(:tid)
        """), {"pid": project_id, "tid": tenant_id})
        proj = res.mappings().fetchone()
        if not proj:
            raise HTTPException(404, "Project not found")

        res = await session.execute(sa_text("""
            SELECT id, title, status, priority, completion_pct, sort_order,
                   start_date, due_date, estimated_minutes, tags
            FROM tasks
            WHERE project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
              AND status NOT IN ('deleted', 'cancelled')
            ORDER BY sort_order, id
        """), {"pid": project_id, "tid": tenant_id})
        trows = [dict(r) for r in res.mappings().fetchall()]

        edges = []
        if trows:
            ids = [t["id"] for t in trows]
            res = await session.execute(sa_text("""
                SELECT task_id, depends_on_id, dependency_type, lag_days
                FROM task_dependencies
                WHERE TRIM(tenant_id) = TRIM(:tid)
                  AND task_id = ANY(:ids) AND depends_on_id = ANY(:ids)
            """), {"tid": tenant_id, "ids": ids})
            edges = [dict(r) for r in res.mappings().fetchall()]

    today = _g_date.today()
    nodes = {}
    for t in trows:
        est = t["estimated_minutes"] or 60
        dur = max(1, _g_ceil(est / 60 / _G_HOURS_PER_DAY))
        start = t["start_date"]
        end = t["due_date"].date() if t["due_date"] else None
        if start and end and end >= start:
            dur = _g_busdays_between(start, end)
        elif start and not end:
            end = _g_add_busdays(start, dur - 1)
        elif end and not start:
            start = _g_sub_busdays(end, dur - 1)
        elif not start and not end:
            start = _g_next_busday(today)
            end = _g_add_busdays(start, dur - 1)
        nodes[t["id"]] = {"task": t, "start": start, "end": end, "dur": dur}

    # CPM over durations + lags
    preds = {tid: [] for tid in nodes}
    succs = {tid: [] for tid in nodes}
    for e in edges:
        preds[e["task_id"]].append((e["depends_on_id"], e["lag_days"] or 0))
        succs[e["depends_on_id"]].append((e["task_id"], e["lag_days"] or 0))

    indeg = {tid: len(v) for tid, v in preds.items()}
    ready = [tid for tid, d0 in indeg.items() if d0 == 0]
    topo = []
    while ready:
        n = ready.pop(0)
        topo.append(n)
        for s, _lag in succs[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
    has_cycle = len(topo) != len(nodes)

    critical_tasks, critical_edges = set(), set()
    if not has_cycle and topo:
        ES, EF = {}, {}
        for n in topo:
            es = 0
            for p, lag in preds[n]:
                es = max(es, EF[p] + max(0, lag))
            ES[n] = es
            EF[n] = es + nodes[n]["dur"]
        makespan = max(EF.values())
        LF, LS = {}, {}
        for n in reversed(topo):
            lf = makespan
            for s, lag in succs[n]:
                lf = min(lf, LS[s] - max(0, lag))
            LF[n] = lf
            LS[n] = lf - nodes[n]["dur"]
        critical_tasks = {n for n in topo if LS[n] - ES[n] == 0}
        for e in edges:
            a, b, lag = e["depends_on_id"], e["task_id"], e["lag_days"] or 0
            if a in critical_tasks and b in critical_tasks and EF[a] + max(0, lag) == ES[b]:
                critical_edges.add((a, b))

    out_tasks = []
    for tid_, nd in nodes.items():
        t = nd["task"]
        tags = t["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (ValueError, TypeError):
                tags = {}
        phase = tags.get("phase") if isinstance(tags, dict) else None
        out_tasks.append({
            "id": t["id"],
            "title": t["title"],
            "status": t["status"],
            "priority": t["priority"],
            "completion_pct": t["completion_pct"],
            "sort_order": t["sort_order"],
            "phase": phase,
            "start": nd["start"].isoformat(),
            "end": nd["end"].isoformat(),
            "duration_days": nd["dur"],
            "is_critical": tid_ in critical_tasks,
            "is_overdue": (t["status"] not in ("complete",)
                           and nd["end"] < today),
        })
    out_tasks.sort(key=lambda x: (x["start"], x["sort_order"]))

    span_start = min((n["start"] for n in nodes.values()), default=today)
    span_end = max((n["end"] for n in nodes.values()), default=today)

    return {
        "project": {
            "id": proj["id"], "title": proj["title"], "status": proj["status"],
            "matter_name": proj["matter_name"],
            "budget_hours": float(proj["budget_hours"]) if proj["budget_hours"] else None,
        },
        "span": {"start": span_start.isoformat(), "end": span_end.isoformat(),
                 "today": today.isoformat()},
        "has_cycle": has_cycle,
        "tasks": out_tasks,
        "edges": [{
            "from": e["depends_on_id"], "to": e["task_id"],
            "type": e["dependency_type"], "lag_days": e["lag_days"] or 0,
            "is_critical": (e["depends_on_id"], e["task_id"]) in critical_edges,
        } for e in edges],
    }


@router.get("/api/project-templates", response_class=JSONResponse)
async def list_project_templates(request: Request):
    # Active project templates visible to this tenant (global = tenant_id NULL).
    tenant_id = _tid(request)
    async with AsyncSessionLocal() as session:
        res = await session.execute(sa_text("""
            SELECT pt.id::text AS id, pt.name, pt.description, pt.project_type,
                   pt.matter_type, pt.default_budget_hours, pt.default_budget_dollars,
                   COUNT(ptt.id) AS task_count,
                   COUNT(DISTINCT ptt.phase) AS phase_count,
                   COALESCE(SUM(ptt.estimated_minutes), 0) AS total_estimated_minutes
            FROM project_templates pt
            LEFT JOIN project_template_tasks ptt ON ptt.template_id = pt.id
            WHERE pt.is_active = true
              AND (pt.tenant_id IS NULL OR TRIM(pt.tenant_id) = TRIM(:tid))
            GROUP BY pt.id
            ORDER BY pt.name
        """), {"tid": tenant_id})
        rows = [dict(r) for r in res.mappings().fetchall()]
    for r in rows:
        r["total_estimated_hours"] = round((r.pop("total_estimated_minutes") or 0) / 60, 1)
        if r["default_budget_hours"] is not None:
            r["default_budget_hours"] = float(r["default_budget_hours"])
        if r["default_budget_dollars"] is not None:
            r["default_budget_dollars"] = float(r["default_budget_dollars"])
    return {"templates": rows, "count": len(rows)}
