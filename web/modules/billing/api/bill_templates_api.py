"""
Bill Templates API — CRUD + assignment management.

GET    /api/v1/billing/templates           — list all templates
GET    /api/v1/billing/templates/{id}      — get template detail
POST   /api/v1/billing/templates           — create template
PUT    /api/v1/billing/templates/{id}      — update template
DELETE /api/v1/billing/templates/{id}      — deactivate template
POST   /api/v1/billing/templates/upload    — drag-and-drop HTML upload
GET    /api/v1/billing/template-assignments?entity_type=&entity_id= — lookup
POST   /api/v1/billing/template-assignments — assign template to entity
DELETE /api/v1/billing/template-assignments/{id} — remove assignment
GET    /api/v1/billing/templates/resolve?matter_id=&client_id=&bill_run_id=
         — resolve which template applies (matter>client>run>default)
"""
from __future__ import annotations
import logging, re, json
from datetime import datetime, timezone
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-templates"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r):
    u = getattr(r.state, "current_user", None)
    return u.id if u and hasattr(u, "id") else 0

MERGE_FIELD_RE = re.compile(r'\{\{(\w+)\}\}')

def discover_merge_fields(html):
    """Extract {{field_name}} patterns from template HTML."""
    fields = set(MERGE_FIELD_RE.findall(html or ""))
    return [{"field": f, "label": f.replace("_", " ").title()} for f in sorted(fields)]

def _ser(row):
    from decimal import Decimal
    import datetime as dt
    import uuid as _uuid
    out = {}
    for k, v in row.items():
        if isinstance(v, _uuid.UUID): out[k] = str(v)
        elif isinstance(v, Decimal): out[k] = float(v)
        elif isinstance(v, (dt.date, dt.datetime)): out[k] = v.isoformat()
        else: out[k] = v
    return out


# ── List templates ───────────────────────────────────────────────────────────
@router.get("/templates")
async def list_templates(request: Request, category: str = ""):
    tid = _tid(request)
    where = "WHERE TRIM(bt.tenant_id) = :tid AND bt.is_active = true"
    params = {"tid": tid}
    if category:
        where += " AND bt.category = :cat"
        params["cat"] = category
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(f"""
            SELECT bt.id, bt.name, bt.description, bt.category,
                   bt.is_default, bt.sort_order, bt.email_subject,
                   bt.merge_fields, bt.created_at, bt.updated_at,
                   u.full_name AS created_by_name
            FROM bill_templates bt
            LEFT JOIN users u ON u.id = bt.created_by_id
            {where}
            ORDER BY bt.sort_order, bt.name
        """), params)).fetchall()
    return JSONResponse({"templates": [_ser(dict(r._mapping)) for r in rows]})


# ── Get template detail ──────────────────────────────────────────────────────
@router.get("/templates/{template_id}")
async def get_template(request: Request, template_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT * FROM bill_templates
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": template_id, "tid": tid})).fetchone()
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"template": _ser(dict(row._mapping))})


# ── Create template ──────────────────────────────────────────────────────────
@router.post("/templates")
async def create_template(request: Request):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    html = body.get("html_content", "")
    fields = discover_merge_fields(html)

    async with AsyncSessionLocal() as db:
        # If is_default, clear other defaults in same category
        if body.get("is_default"):
            await db.execute(sa_text("""
                UPDATE bill_templates SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND category = :cat AND is_default = true
            """), {"tid": tid, "cat": body.get("category", "standard")})

        row = (await db.execute(sa_text("""
            INSERT INTO bill_templates
                (tenant_id, name, description, category, html_content,
                 email_subject, email_body, css_content, merge_fields,
                 is_default, sort_order, created_by_id)
            VALUES (:tid, :name, :desc, :cat, :html,
                    :subj, :ebody, :css, CAST(:fields AS jsonb),
                    :default, :sort, :uid)
            RETURNING id
        """), {
            "tid": tid, "name": body["name"],
            "desc": body.get("description"),
            "cat": body.get("category", "standard"),
            "html": html,
            "subj": body.get("email_subject"),
            "ebody": body.get("email_body"),
            "css": body.get("css_content"),
            "fields": json.dumps(fields),
            "default": body.get("is_default", False),
            "sort": body.get("sort_order", 0),
            "uid": uid,
        })).fetchone()
        await db.commit()
    return JSONResponse({"id": str(row[0]), "merge_fields": fields}, status_code=201)


# ── Update template ──────────────────────────────────────────────────────────
@router.put("/templates/{template_id}")
async def update_template(request: Request, template_id: str):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()
    html = body.get("html_content")
    fields = discover_merge_fields(html) if html else None

    sets = []
    params = {"tid": tid, "id": template_id, "uid": uid}
    for key in ["name", "description", "category", "html_content", "email_subject",
                "email_body", "css_content", "is_default", "sort_order"]:
        if key in body:
            sets.append(f"{key} = :{key}")
            params[key] = body[key]
    if fields is not None:
        sets.append("merge_fields = CAST(:fields AS jsonb)")
        params["fields"] = json.dumps(fields)
    sets.append("updated_by_id = :uid")
    sets.append("updated_at = now()")

    if not sets:
        return JSONResponse({"error": "Nothing to update"}, status_code=400)

    async with AsyncSessionLocal() as db:
        if body.get("is_default"):
            cat = body.get("category", "standard")
            await db.execute(sa_text("""
                UPDATE bill_templates SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND category = :cat
                  AND is_default = true AND id != CAST(:id AS uuid)
            """), {"tid": tid, "cat": cat, "id": template_id})

        await db.execute(sa_text(f"""
            UPDATE bill_templates SET {', '.join(sets)}
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()
    return JSONResponse({"ok": True})


# ── Delete (deactivate) template ─────────────────────────────────────────────
@router.delete("/templates/{template_id}")
async def delete_template(request: Request, template_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE bill_templates SET is_active = false, updated_at = now()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": template_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True})


# ── Upload HTML template via drag-and-drop ───────────────────────────────────
@router.post("/templates/upload")
async def upload_template(request: Request, file: UploadFile = File(...),
                          name: str = Form(""), category: str = Form("standard")):
    tid = _tid(request)
    uid = _uid(request)
    content = (await file.read()).decode("utf-8", errors="replace")
    tpl_name = name or file.filename or "Uploaded Template"
    fields = discover_merge_fields(content)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            INSERT INTO bill_templates
                (tenant_id, name, category, html_content, merge_fields, created_by_id)
            VALUES (:tid, :name, :cat, :html, CAST(:fields AS jsonb), :uid)
            RETURNING id
        """), {
            "tid": tid, "name": tpl_name, "cat": category,
            "html": content, "fields": json.dumps(fields), "uid": uid,
        })).fetchone()
        await db.commit()
    return JSONResponse({"id": str(row[0]), "name": tpl_name, "merge_fields": fields}, status_code=201)


# ── Template Assignments ─────────────────────────────────────────────────────
@router.get("/template-assignments")
async def list_assignments(request: Request, entity_type: str = "", entity_id: str = ""):
    tid = _tid(request)
    where = "WHERE TRIM(bta.tenant_id) = :tid"
    params = {"tid": tid}
    if entity_type:
        where += " AND bta.entity_type = :etype"
        params["etype"] = entity_type
    if entity_id:
        where += " AND bta.entity_id = CAST(:eid AS uuid)"
        params["eid"] = entity_id
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(f"""
            SELECT bta.*, bt.name AS template_name, bt.category
            FROM bill_template_assignments bta
            JOIN bill_templates bt ON bt.id = bta.template_id
            {where}
            ORDER BY bta.entity_type, bta.created_at
        """), params)).fetchall()
    return JSONResponse({"assignments": [_ser(dict(r._mapping)) for r in rows]})


@router.post("/template-assignments")
async def create_assignment(request: Request):
    tid = _tid(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            INSERT INTO bill_template_assignments
                (tenant_id, template_id, entity_type, entity_id, notes)
            VALUES (:tid, CAST(:tmpl AS uuid), :etype, CAST(:eid AS uuid), :notes)
            ON CONFLICT (tenant_id, entity_type, entity_id)
            DO UPDATE SET template_id = CAST(:tmpl AS uuid), notes = :notes
            RETURNING id
        """), {
            "tid": tid, "tmpl": body["template_id"],
            "etype": body["entity_type"], "eid": body["entity_id"],
            "notes": body.get("notes"),
        })).fetchone()
        await db.commit()
    return JSONResponse({"id": str(row[0])}, status_code=201)


@router.delete("/template-assignments/{assignment_id}")
async def delete_assignment(request: Request, assignment_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            DELETE FROM bill_template_assignments
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": assignment_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True})


# ── Resolve template for a matter ────────────────────────────────────────────
@router.get("/templates/resolve")
async def resolve_template(request: Request, matter_id: str = "",
                           client_id: str = "", bill_run_id: str = ""):
    """Resolve which template applies: matter > client > bill_run > tenant default."""
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        # 1. Matter-level assignment
        if matter_id:
            row = (await db.execute(sa_text("""
                SELECT bt.* FROM bill_template_assignments bta
                JOIN bill_templates bt ON bt.id = bta.template_id
                WHERE TRIM(bta.tenant_id) = :tid AND bta.entity_type = 'matter'
                  AND bta.entity_id = CAST(:eid AS uuid) AND bt.is_active = true
            """), {"tid": tid, "eid": matter_id})).fetchone()
            if row:
                return JSONResponse({"template": _ser(dict(row._mapping)), "source": "matter"})

        # 2. Client-level assignment
        if client_id:
            row = (await db.execute(sa_text("""
                SELECT bt.* FROM bill_template_assignments bta
                JOIN bill_templates bt ON bt.id = bta.template_id
                WHERE TRIM(bta.tenant_id) = :tid AND bta.entity_type = 'client'
                  AND bta.entity_id = CAST(:eid AS uuid) AND bt.is_active = true
            """), {"tid": tid, "eid": client_id})).fetchone()
            if row:
                return JSONResponse({"template": _ser(dict(row._mapping)), "source": "client"})

        # 3. Bill run default
        if bill_run_id:
            row = (await db.execute(sa_text("""
                SELECT bt.* FROM bill_runs br
                JOIN bill_templates bt ON bt.id = br.template_id
                WHERE br.id = CAST(:brid AS bigint) AND br.tenant_id = :tid AND bt.is_active = true
            """), {"tid": tid, "brid": bill_run_id})).fetchone()
            if row:
                return JSONResponse({"template": _ser(dict(row._mapping)), "source": "bill_run"})

        # 4. Tenant default
        row = (await db.execute(sa_text("""
            SELECT * FROM bill_templates
            WHERE TRIM(tenant_id) = :tid AND is_default = true AND is_active = true
            ORDER BY sort_order LIMIT 1
        """), {"tid": tid})).fetchone()
        if row:
            return JSONResponse({"template": _ser(dict(row._mapping)), "source": "default"})

    return JSONResponse({"template": None, "source": None})


# ── SMTP Config ──────────────────────────────────────────────────────────────
@router.get("/smtp-config")
async def get_smtp_config(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT id, connector, connector_type, config, is_active, status
            FROM tenant_connectors
            WHERE TRIM(tenant_id) = :tid
              AND (connector_type ILIKE \'%%smtp%%\' OR connector ILIKE \'%%smtp%%\')
            LIMIT 1
        """), {"tid": tid})).fetchone()
    if not row:
        return JSONResponse({"error": "no_smtp_connector"})
    import json as _json
    cfg = row.config if isinstance(row.config, dict) else _json.loads(row.config or "{}")
    return JSONResponse({"config": cfg, "is_active": row.is_active, "status": row.status})
