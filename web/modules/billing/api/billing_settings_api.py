"""
Billing Settings API — CRUD for email routing rules, payment instructions,
and general billing settings (firm info, invoice config).
"""
from __future__ import annotations
import logging, json, uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal
import datetime as dt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-settings"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r):
    u = getattr(r.state, "current_user", None)
    return u.id if u and hasattr(u, "id") else 0

def _ser(row):
    """Serialize a DB row dict for JSON — handles UUID, Decimal, datetime."""
    out = {}
    for k, v in row.items():
        if isinstance(v, _uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  EMAIL ROUTING RULES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/email-routing-rules")
async def list_email_routing_rules(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT r.*, u.full_name AS created_by_name,
                   tk.ts_name AS attorney_name
            FROM bill_email_routing_rules r
            LEFT JOIN users u ON u.id = r.created_by_id
            LEFT JOIN ts_timekeepers tk ON tk.ts_tk_id = CAST(r.billing_attorney_id AS text)
            WHERE TRIM(r.tenant_id) = :tid AND r.is_active = true
            ORDER BY r.sort_order, r.name
        """), {"tid": tid})).fetchall()
    return JSONResponse({"rules": [_ser(dict(r._mapping)) for r in rows]})


@router.post("/email-routing-rules")
async def create_email_routing_rule(request: Request):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()

    async with AsyncSessionLocal() as db:
        if body.get("is_default"):
            await db.execute(sa_text("""
                UPDATE bill_email_routing_rules SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND is_default = true
            """), {"tid": tid})

        row = (await db.execute(sa_text("""
            INSERT INTO bill_email_routing_rules
                (tenant_id, name, description, from_name, reply_to, cc, bcc,
                 attachment_format, signature_html, billing_attorney_id,
                 client_id, matter_id, is_default, sort_order, created_by_id)
            VALUES (:tid, :name, :desc, :from_name, :reply_to, :cc, :bcc,
                    :att_fmt, :sig, :atty_id,
                    CAST(NULLIF(:client_id, '') AS uuid),
                    CAST(NULLIF(:matter_id, '') AS uuid),
                    :is_default, :sort, :uid)
            RETURNING id
        """), {
            "tid": tid, "name": body["name"],
            "desc": body.get("description"),
            "from_name": body.get("from_name"),
            "reply_to": body.get("reply_to"),
            "cc": body.get("cc"),
            "bcc": body.get("bcc"),
            "att_fmt": body.get("attachment_format", "pdf"),
            "sig": body.get("signature_html"),
            "atty_id": body.get("billing_attorney_id"),
            "client_id": body.get("client_id") or "",
            "matter_id": body.get("matter_id") or "",
            "is_default": body.get("is_default", False),
            "sort": body.get("sort_order", 0),
            "uid": uid,
        })).fetchone()
        await db.commit()
    return JSONResponse({"id": str(row[0])}, status_code=201)


@router.put("/email-routing-rules/{rule_id}")
async def update_email_routing_rule(request: Request, rule_id: str):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()

    sets = []
    params = {"tid": tid, "id": rule_id, "uid": uid}
    for key in ["name", "description", "from_name", "reply_to", "cc", "bcc",
                "attachment_format", "signature_html", "billing_attorney_id",
                "is_default", "sort_order"]:
        if key in body:
            sets.append(f"{key} = :{key}")
            params[key] = body[key]
    if "client_id" in body:
        sets.append("client_id = CAST(NULLIF(:client_id, '') AS uuid)")
        params["client_id"] = body["client_id"] or ""
    if "matter_id" in body:
        sets.append("matter_id = CAST(NULLIF(:matter_id, '') AS uuid)")
        params["matter_id"] = body["matter_id"] or ""

    sets.append("updated_by_id = :uid")
    sets.append("updated_at = now()")

    async with AsyncSessionLocal() as db:
        if body.get("is_default"):
            await db.execute(sa_text("""
                UPDATE bill_email_routing_rules SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND is_default = true
                  AND id != CAST(:id AS uuid)
            """), {"tid": tid, "id": rule_id})

        await db.execute(sa_text(f"""
            UPDATE bill_email_routing_rules SET {', '.join(sets)}
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()
    return JSONResponse({"ok": True})


@router.delete("/email-routing-rules/{rule_id}")
async def delete_email_routing_rule(request: Request, rule_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE bill_email_routing_rules SET is_active = false, updated_at = now()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": rule_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
#  PAYMENT INSTRUCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/payment-instructions")
async def list_payment_instructions(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT pi.*, u.full_name AS created_by_name
            FROM bill_payment_instructions pi
            LEFT JOIN users u ON u.id = pi.created_by_id
            WHERE TRIM(pi.tenant_id) = :tid AND pi.is_active = true
            ORDER BY pi.sort_order, pi.name
        """), {"tid": tid})).fetchall()
    return JSONResponse({"instructions": [_ser(dict(r._mapping)) for r in rows]})


@router.post("/payment-instructions")
async def create_payment_instruction(request: Request):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()

    async with AsyncSessionLocal() as db:
        if body.get("is_default"):
            await db.execute(sa_text("""
                UPDATE bill_payment_instructions SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND is_default = true
            """), {"tid": tid})

        row = (await db.execute(sa_text("""
            INSERT INTO bill_payment_instructions
                (tenant_id, name, description, instructions_text, instructions_html,
                 bank_name, routing_number, account_number, account_type,
                 payment_url, billing_terms, is_default, sort_order, created_by_id)
            VALUES (:tid, :name, :desc, :text, :html,
                    :bank, :routing, :acct, :acct_type,
                    :pay_url, :billing_terms, :is_default, :sort, :uid)
            RETURNING id
        """), {
            "tid": tid, "name": body["name"],
            "desc": body.get("description"),
            "text": body.get("instructions_text", ""),
            "html": body.get("instructions_html"),
            "bank": body.get("bank_name"),
            "routing": body.get("routing_number"),
            "acct": body.get("account_number"),
            "acct_type": body.get("account_type"),
            "pay_url": body.get("payment_url"),
            "billing_terms": body.get("billing_terms"),
            "is_default": body.get("is_default", False),
            "sort": body.get("sort_order", 0),
            "uid": uid,
        })).fetchone()
        await db.commit()
    return JSONResponse({"id": str(row[0])}, status_code=201)


@router.put("/payment-instructions/{instruction_id}")
async def update_payment_instruction(request: Request, instruction_id: str):
    tid = _tid(request)
    uid = _uid(request)
    body = await request.json()

    sets = []
    params = {"tid": tid, "id": instruction_id, "uid": uid}
    for key in ["name", "description", "instructions_text", "instructions_html",
                "bank_name", "routing_number", "account_number", "account_type",
                "payment_url", "billing_terms", "is_default", "sort_order"]:
        if key in body:
            sets.append(f"{key} = :{key}")
            params[key] = body[key]
    sets.append("updated_by_id = :uid")
    sets.append("updated_at = now()")

    async with AsyncSessionLocal() as db:
        if body.get("is_default"):
            await db.execute(sa_text("""
                UPDATE bill_payment_instructions SET is_default = false
                WHERE TRIM(tenant_id) = :tid AND is_default = true
                  AND id != CAST(:id AS uuid)
            """), {"tid": tid, "id": instruction_id})

        await db.execute(sa_text(f"""
            UPDATE bill_payment_instructions SET {', '.join(sets)}
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), params)
        await db.commit()
    return JSONResponse({"ok": True})


@router.delete("/payment-instructions/{instruction_id}")
async def delete_payment_instruction(request: Request, instruction_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE bill_payment_instructions SET is_active = false, updated_at = now()
            WHERE id = CAST(:id AS uuid) AND TRIM(tenant_id) = :tid
        """), {"id": instruction_id, "tid": tid})
        await db.commit()
    return JSONResponse({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
#  GENERAL BILLING SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/settings")
async def get_billing_settings(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text("""
            SELECT * FROM billing_settings
            WHERE TRIM(tenant_id) = :tid
            LIMIT 1
        """), {"tid": tid})).fetchone()
    if not row:
        return JSONResponse({"settings": None})
    return JSONResponse({"settings": _ser(dict(row._mapping))})


@router.put("/settings")
async def upsert_billing_settings(request: Request):
    tid = _tid(request)
    body = await request.json()

    cols = ["firm_name", "firm_address_line1", "firm_suite", "firm_city",
            "firm_state", "firm_zip", "firm_phone", "firm_email",
            "firm_logo_url", "invoice_prefix", "net_terms"]

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(sa_text("""
            SELECT id FROM billing_settings WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).fetchone()

        if existing:
            sets = []
            params = {"tid": tid}
            for c in cols:
                if c in body:
                    sets.append(f"{c} = :{c}")
                    params[c] = body[c]
            if "config_json" in body:
                sets.append("config_json = CAST(:cfg AS jsonb)")
                params["cfg"] = json.dumps(body["config_json"])
            sets.append("updated_at = now()")
            await db.execute(sa_text(f"""
                UPDATE billing_settings SET {', '.join(sets)}
                WHERE TRIM(tenant_id) = :tid
            """), params)
        else:
            params = {"tid": tid}
            insert_cols = ["tenant_id"]
            insert_vals = [":tid"]
            for c in cols:
                if c in body:
                    insert_cols.append(c)
                    insert_vals.append(f":{c}")
                    params[c] = body[c]
            if "config_json" in body:
                insert_cols.append("config_json")
                insert_vals.append("CAST(:cfg AS jsonb)")
                params["cfg"] = json.dumps(body["config_json"])
            await db.execute(sa_text(f"""
                INSERT INTO billing_settings ({', '.join(insert_cols)})
                VALUES ({', '.join(insert_vals)})
            """), params)
        await db.commit()
    return JSONResponse({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMEKEEPERS DROPDOWN
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/timekeepers-dropdown")
async def timekeepers_dropdown(request: Request):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT ts_tk_id, ts_name, ts_initials
            FROM ts_timekeepers
            WHERE TRIM(tenant_id) = :tid
            ORDER BY ts_name
        """), {"tid": tid})).fetchall()
    return JSONResponse({"timekeepers": [dict(r._mapping) for r in rows]})


# ═══════════════════════════════════════════════════════════════════════════════
#  FIRM LOGO UPLOAD
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/settings/logo")
async def upload_firm_logo(request: Request):
    import os as _os
    tid = _tid(request)
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        return JSONResponse({"error": "No file provided"}, status_code=400)
    fname = getattr(file, "filename", "logo.png") or "logo.png"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "png"
    if ext not in ("png", "jpg", "jpeg", "svg", "webp", "gif"):
        return JSONResponse({"error": "Invalid file type"}, status_code=400)
    upload_dir = "/app/static/img/tenant"
    _os.makedirs(upload_dir, exist_ok=True)
    safe_tid = tid.replace("-", "")[:12]
    filename = f"logo_{safe_tid}.{ext}"
    filepath = _os.path.join(upload_dir, filename)
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        return JSONResponse({"error": "File too large. Max 2MB."}, status_code=400)
    with open(filepath, "wb") as f:
        f.write(data)
    logo_url = f"/static/img/tenant/{filename}"
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(sa_text(
            "SELECT id FROM billing_settings WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})).fetchone()
        if existing:
            await db.execute(sa_text(
                "UPDATE billing_settings SET firm_logo_url = :url, updated_at = now() WHERE TRIM(tenant_id) = :tid"
            ), {"url": logo_url, "tid": tid})
        else:
            await db.execute(sa_text(
                "INSERT INTO billing_settings (tenant_id, firm_logo_url) VALUES (:tid, :url)"
            ), {"tid": tid, "url": logo_url})
        await db.commit()
    return JSONResponse({"logo_url": logo_url, "filename": filename})
