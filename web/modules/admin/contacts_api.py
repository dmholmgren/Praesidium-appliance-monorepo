"""
modules/admin/contacts_api.py
=============================

Phase C v3 — Contacts CRUD UI with inline editing + Assign to Client.

Changes from v2:
  - BUG FIX: `to_jsonb(CAST(:cid AS bigint))` -> `to_jsonb(CAST(:cid AS bigint))`
    (asyncpg cannot parse the CAST(:param AS type) cast syntax). This was the
    cause of the GET /contacts/{id} 500.
  - INLINE EDIT: new field-by-field PATCH-ish endpoint that returns the
    rendered label fragment for HTMX swap-in-place.
  - ASSIGN TO CLIENT: new modal + POST handler. User picks an existing
    client OR creates a new one; user picks an existing matter on that
    client OR a new matter is created; matter_contacts row is written
    linking the contact in the chosen role.

Inline-edit UX:
  - Each editable field renders as a label.
  - Clicking the label swaps it for an input with Save/Cancel.
  - Saving issues POST /contacts/{id}/field/{field_name} which returns
    the rendered label fragment (HTMX swaps it back in).

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin.contacts")

router = APIRouter(prefix="/contacts", tags=["contacts"])

templates = Jinja2Templates(directory=[
    "core/templates",
    "modules/billing/templates",
])


ASSIGNABLE_ROLES = [
    "client",
    "opposing_counsel",
    "co_counsel",
    "witness",
    "expert",
    "paralegal",
    "referred_by",
    "other",
]

CONTACT_TYPES = ["exchange", "phone", "auto_email", "manual", "archived"]

# Whitelist of fields that can be inline-edited. Maps the URL field name
# to (column_name, input_type, validator). validator returns the cleaned
# value or raises HTTPException.
EDITABLE_FIELDS: Dict[str, Dict[str, Any]] = {
    "full_name":    {"col": "full_name",    "type": "text",     "required": True},
    "email":        {"col": "email",        "type": "email"},
    "phone":        {"col": "phone",        "type": "phone"},
    "company":      {"col": "company",      "type": "text"},
    "firm_name":    {"col": "firm_name",    "type": "text"},
    "contact_type": {"col": "contact_type", "type": "enum", "choices": CONTACT_TYPES},
    "notes":        {"col": "notes",        "type": "textarea"},
    # === v4 address fields ===
    "address1":     {"col": "address1",     "type": "text"},
    "city":         {"col": "city",         "type": "text"},
    "state":        {"col": "state",        "type": "text"},

}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _strip(v: Optional[str]) -> str:
    return (v or "").strip()


def _normalize_phone_for_save(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return raw.strip() or None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


async def _require_admin(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    tid = getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "Tenant not resolved")
    return {
        "user_id": int(user.id),
        "tenant_id": (tid or "").strip(),
        "role": getattr(user, "role", None) or "staff",
    }


def _ctx(request: Request, sess: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    from modules.billing.brand_helper import get_brand
    user = getattr(request.state, "current_user", None)
    return {
        "request": request,
        "brand": get_brand(request),
        "page": "billing",
        "bill_tab": "contacts",
        "user": user,
        "current_user": user,
        **kwargs,
    }


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
async def _get_contacts(
    tenant_id: str,
    search: str = "",
    contact_type: Optional[str] = None,
    only_with_matters: bool = False,
    only_pending_dedup: bool = False,
    limit: int = 200,
) -> List[Any]:
    where_parts = ["TRIM(c.tenant_id) = :tid"]
    params: Dict[str, Any] = {"tid": tenant_id, "limit": limit}

    if search:
        where_parts.append(
            "(c.full_name ILIKE :q OR c.email ILIKE :q OR c.phone ILIKE :q "
            "OR c.company ILIKE :q)"
        )
        params["q"] = f"%{search}%"

    if contact_type and contact_type in CONTACT_TYPES:
        where_parts.append("c.contact_type = :ctype")
        params["ctype"] = contact_type

    if only_with_matters:
        where_parts.append(
            "EXISTS (SELECT 1 FROM matter_contacts mc "
            "WHERE mc.contact_id = c.id "
            "AND TRIM(mc.tenant_id) = TRIM(c.tenant_id))"
        )

    if only_pending_dedup:
        # c.id is a column reference, ::bigint is fine here (not a bound param)
        where_parts.append(
            "EXISTS (SELECT 1 FROM contact_dedup_candidates dc "
            "WHERE TRIM(dc.tenant_id) = TRIM(c.tenant_id) "
            "AND dc.review_outcome IS NULL "
            "AND dc.contact_ids @> to_jsonb(c.id::bigint))"
        )

    sql = f"""
        SELECT
            c.id,
            c.full_name,
            c.email,
            c.phone,
            c.company,
            c.contact_type,
            c.firm_name,
            c.created_at,
            COALESCE((
                SELECT COUNT(*) FROM matter_contacts mc
                WHERE mc.contact_id = c.id
                  AND TRIM(mc.tenant_id) = TRIM(c.tenant_id)
            ), 0) AS n_matters,
            COALESCE((
                SELECT COUNT(*) FROM matter_contact_proposals p
                WHERE p.contact_id = c.id
                  AND TRIM(p.tenant_id) = TRIM(c.tenant_id)
                  AND p.review_status = 'pending'
            ), 0) AS n_pending_proposals
        FROM contacts c
        WHERE {' AND '.join(where_parts)}
        ORDER BY c.full_name ASC
        LIMIT :limit
    """

    async with AsyncSessionLocal() as db:
        r = await db.execute(text(sql), params)
        return r.fetchall()


async def _get_contact(contact_id: int, tenant_id: str):
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, tenant_id, full_name, email, phone,
                       company, contact_type, firm_name, address1,
                       city, state, bar_number, notes, external_id,
                       created_at, updated_at
                FROM contacts
                WHERE id = :id AND TRIM(tenant_id) = :tid
            """),
            {"id": contact_id, "tid": tenant_id},
        )
        return r.fetchone()


async def _get_linked_matters(contact_id: int, tenant_id: str) -> List[Any]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT
                    mc.id            AS link_id,
                    mc.role,
                    mc.is_primary,
                    mc.notes         AS link_notes,
                    m.id             AS matter_id,
                    m.matter_number,
                    m.matter_name,
                    m.status         AS matter_status,
                    cl.client_name
                FROM matter_contacts mc
                JOIN matters m ON m.id = mc.matter_id
                LEFT JOIN clients cl ON cl.id = m.client_id
                WHERE mc.contact_id = :cid
                  AND TRIM(mc.tenant_id) = :tid
                ORDER BY
                    CASE WHEN mc.is_primary = 'Y' THEN 0 ELSE 1 END,
                    m.matter_name
            """),
            {"cid": contact_id, "tid": tenant_id},
        )
        return r.fetchall()


async def _get_pending_proposals_for_contact(
    contact_id: int, tenant_id: str
) -> List[Any]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT
                    p.id            AS proposal_id,
                    p.matter_id,
                    p.proposed_role,
                    p.proposed_is_primary,
                    p.signal_type,
                    p.confidence,
                    p.signal_count,
                    p.notes,
                    m.matter_number,
                    m.matter_name,
                    cl.client_name
                FROM matter_contact_proposals p
                JOIN matters m ON m.id = p.matter_id
                LEFT JOIN clients cl ON cl.id = m.client_id
                WHERE p.contact_id = :cid
                  AND TRIM(p.tenant_id) = :tid
                  AND p.review_status = 'pending'
                ORDER BY p.confidence DESC, p.signal_count DESC
            """),
            {"cid": contact_id, "tid": tenant_id},
        )
        return r.fetchall()


async def _get_dedup_candidates_for_contact(
    contact_id: int, tenant_id: str
) -> List[Any]:
    """BUG FIX (v3): asyncpg cannot parse `CAST(:cid AS bigint)` cast syntax when
    `:cid` is a bound parameter. Switched to CAST(:cid AS bigint)."""
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, contact_ids, signal_type, confidence, notes
                FROM contact_dedup_candidates
                WHERE TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
                  AND contact_ids @> to_jsonb(CAST(:cid AS bigint))
                ORDER BY confidence DESC, detected_at DESC
            """),
            {"cid": contact_id, "tid": tenant_id},
        )
        return r.fetchall()


# --------------------------------------------------------------------------
# Inline-edit helpers
# --------------------------------------------------------------------------
def _label_value(field_name: str, contact: Any) -> str:
    """Get the display string for a field for the read-mode label."""
    raw = getattr(contact, field_name, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return str(raw)


def _validate_field_value(field_name: str, raw_value: str) -> Optional[str]:
    """Return cleaned value (or None for blank), raise HTTPException on error."""
    spec = EDITABLE_FIELDS.get(field_name)
    if not spec:
        raise HTTPException(400, f"Field {field_name!r} is not editable")
    raw = _strip(raw_value)
    if spec["type"] == "phone":
        return _normalize_phone_for_save(raw) if raw else None
    if spec["type"] == "email":
        if not raw:
            return None
        if not _valid_email(raw):
            raise HTTPException(400, f"Invalid email format: {raw!r}")
        return raw.lower()
    if spec["type"] == "enum":
        if raw not in spec["choices"]:
            raise HTTPException(400, f"Invalid {field_name}: {raw!r}")
        return raw
    if spec.get("required") and not raw:
        raise HTTPException(400, f"{field_name} is required")
    return raw or None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
async def contacts_list(
    request: Request,
    search: str = "",
    contact_type: Optional[str] = None,
    has_matters: int = 0,
    pending_dedup: int = 0,
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    contacts = await _get_contacts(
        tid,
        search=search,
        contact_type=contact_type,
        only_with_matters=bool(has_matters),
        only_pending_dedup=bool(pending_dedup),
    )

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT
                  (SELECT COUNT(*) FROM contact_dedup_candidates
                   WHERE TRIM(tenant_id) = :tid AND review_outcome IS NULL)
                    AS pending_dedup,
                  (SELECT COUNT(*) FROM matter_contact_proposals
                   WHERE TRIM(tenant_id) = :tid AND review_status = 'pending')
                    AS pending_proposals,
                  (SELECT COUNT(*) FROM contacts
                   WHERE TRIM(tenant_id) = :tid AND contact_type != 'archived')
                    AS total_contacts
            """),
            {"tid": tid},
        )
        counts = r.fetchone()

    return templates.TemplateResponse(
        request,
        "contacts/contacts_list.html",
        _ctx(request, sess,
             contacts=contacts,
             search=search,
             contact_type=contact_type,
             contact_types=CONTACT_TYPES,
             has_matters=bool(has_matters),
             pending_dedup_filter=bool(pending_dedup),
             counts={
                 "pending_dedup": int(counts.pending_dedup or 0) if counts else 0,
                 "pending_proposals": int(counts.pending_proposals or 0) if counts else 0,
                 "total": int(counts.total_contacts or 0) if counts else 0,
             }),
    )


@router.get("/new", response_class=HTMLResponse)
async def contact_new_form(request: Request):
    """New-contact form. Uses the v2 template (still classic form mode for
    creates — only existing-record edits are inline)."""
    sess = await _require_admin(request)
    return templates.TemplateResponse(
        request, "contacts/contact_new.html",
        _ctx(request, sess,
             contact=None,
             contact_types=CONTACT_TYPES,
             form={},
             error=None),
    )


@router.post("/new")
async def contact_create(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    company: str = Form(""),
    firm_name: str = Form(""),
    contact_type: str = Form("manual"),
    notes: str = Form(""),
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]

    full_name = _strip(full_name)
    email_v = _strip(email).lower() or None
    phone_v = _normalize_phone_for_save(phone)
    company_v = _strip(company) or None
    firm_v = _strip(firm_name) or None
    notes_v = _strip(notes) or None
    if contact_type not in CONTACT_TYPES:
        contact_type = "manual"

    def _err(msg: str):
        return templates.TemplateResponse(
            request, "contacts/contact_new.html",
            _ctx(request, sess,
                 contact=None,
                 contact_types=CONTACT_TYPES,
                 form={
                     "full_name": full_name, "email": email_v, "phone": phone_v,
                     "company": company_v, "firm_name": firm_v,
                     "contact_type": contact_type, "notes": notes_v,
                 },
                 error=msg),
        )

    if not full_name:
        return _err("Full name is required.")
    if email_v and not _valid_email(email_v):
        return _err(f"Invalid email format: {email_v!r}")

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                text("""
                    INSERT INTO contacts
                        (tenant_id, full_name, email, phone,
                         company, firm_name, contact_type, notes)
                    VALUES
                        (:tid, :name, :email, :phone,
                         :company, :firm, :ctype, :notes)
                    RETURNING id
                """),
                {
                    "tid": tid, "name": full_name, "email": email_v,
                    "phone": phone_v, "company": company_v, "firm": firm_v,
                    "ctype": contact_type, "notes": notes_v,
                },
            )
            new_id = r.scalar()
            await db.commit()
    except Exception as e:
        log.exception("Contact create failed")
        return _err(f"Database error: {str(e)[:200]}")

    return RedirectResponse(f"/contacts/{new_id}", status_code=303)


@router.get("/{contact_id}", response_class=HTMLResponse)
async def contact_edit_form(request: Request, contact_id: int):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")
    linked = await _get_linked_matters(contact_id, tid)
    pending = await _get_pending_proposals_for_contact(contact_id, tid)
    dedup = await _get_dedup_candidates_for_contact(contact_id, tid)

    return templates.TemplateResponse(
        request, "contacts/contact_edit.html",
        _ctx(request, sess,
             contact=contact,
             contact_id=contact_id,
             linked_matters=linked,
             pending_proposals=pending,
             dedup_candidates=dedup,
             tenant_id=tid,
             contact_types=CONTACT_TYPES,
             roles=ASSIGNABLE_ROLES,
             editable_fields=EDITABLE_FIELDS),
    )


# --------------------------------------------------------------------------
# Inline-edit endpoints
# --------------------------------------------------------------------------
@router.get("/{contact_id}/field/{field_name}/edit", response_class=HTMLResponse)
async def field_edit_form(request: Request, contact_id: int, field_name: str):
    """Render the input-mode partial for one field."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if field_name not in EDITABLE_FIELDS:
        raise HTTPException(400, f"Field {field_name!r} is not editable")
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return templates.TemplateResponse(
        request, "contacts/_field_input.html",
        _ctx(request, sess,
             contact=contact,
             contact_id=contact_id,
             field_name=field_name,
             field_spec=EDITABLE_FIELDS[field_name],
             current_value=_label_value(field_name, contact),
             contact_types=CONTACT_TYPES),
    )


@router.get("/{contact_id}/field/{field_name}", response_class=HTMLResponse)
async def field_label(request: Request, contact_id: int, field_name: str):
    """Render the read-mode label for one field. Used as cancel target."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if field_name not in EDITABLE_FIELDS:
        raise HTTPException(400, f"Field {field_name!r} is not editable")
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return templates.TemplateResponse(
        request, "contacts/_field_label.html",
        _ctx(request, sess,
             contact=contact,
             contact_id=contact_id,
             field_name=field_name,
             field_spec=EDITABLE_FIELDS[field_name],
             current_value=_label_value(field_name, contact)),
    )


@router.post("/{contact_id}/field/{field_name}", response_class=HTMLResponse)
async def field_save(
    request: Request,
    contact_id: int,
    field_name: str,
    value: str = Form(""),
):
    """Save a single field, return the read-mode label fragment."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if field_name not in EDITABLE_FIELDS:
        raise HTTPException(400, f"Field {field_name!r} is not editable")
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")

    cleaned = _validate_field_value(field_name, value)
    col = EDITABLE_FIELDS[field_name]["col"]

    async with AsyncSessionLocal() as db:
        # Use parameterized SQL with the column name interpolated safely
        # (col comes from EDITABLE_FIELDS whitelist, never user input)
        await db.execute(
            text(f"""
                UPDATE contacts
                   SET {col} = :val,
                       updated_at = NOW()
                 WHERE id = :id
                   AND TRIM(tenant_id) = :tid
            """),
            {"val": cleaned, "id": contact_id, "tid": tid},
        )
        await db.commit()

    # Re-fetch to pick up any normalization (phone formatting, lowercase email)
    contact = await _get_contact(contact_id, tid)
    return templates.TemplateResponse(
        request, "contacts/_field_label.html",
        _ctx(request, sess,
             contact=contact,
             contact_id=contact_id,
             field_name=field_name,
             field_spec=EDITABLE_FIELDS[field_name],
             current_value=_label_value(field_name, contact)),
    )


@router.post("/{contact_id}/delete")
async def contact_delete(request: Request, contact_id: int):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE contacts
                   SET contact_type = 'archived', updated_at = NOW()
                 WHERE id = :id AND TRIM(tenant_id) = :tid
            """),
            {"id": contact_id, "tid": tid},
        )
        await db.commit()
    return RedirectResponse("/contacts", status_code=303)


# --------------------------------------------------------------------------
# Matter linking (existing manual link)
# --------------------------------------------------------------------------
@router.post("/{contact_id}/link-matter", response_class=HTMLResponse)
async def link_matter(
    request: Request,
    contact_id: int,
    matter_id: str = Form(...),
    role: str = Form("client"),
    is_primary: str = Form("N"),
    link_notes: str = Form(""),
):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]

    if role not in ASSIGNABLE_ROLES:
        role = "other"
    if is_primary not in ("Y", "N"):
        is_primary = "N"

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT 1 FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"mid": matter_id, "tid": tid},
        )
        if not r.fetchone():
            raise HTTPException(404, "Matter not found")

        r = await db.execute(
            text("""
                SELECT id FROM matter_contacts
                WHERE contact_id = :cid
                  AND matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"cid": contact_id, "mid": matter_id, "tid": tid},
        )
        existing = r.fetchone()
        if existing:
            await db.execute(
                text("""
                    UPDATE matter_contacts
                       SET role = :role,
                           is_primary = :primary,
                           notes = :notes
                     WHERE id = :id
                """),
                {
                    "role": role, "primary": is_primary,
                    "notes": _strip(link_notes) or None,
                    "id": existing[0],
                },
            )
        else:
            await db.execute(
                text("""
                    INSERT INTO matter_contacts
                        (tenant_id, matter_id, contact_id,
                         role, is_primary, notes)
                    VALUES
                        (:tid, CAST(:mid AS uuid), :cid,
                         :role, :primary, :notes)
                """),
                {
                    "tid": tid, "mid": matter_id, "cid": contact_id,
                    "role": role, "primary": is_primary,
                    "notes": _strip(link_notes) or None,
                },
            )
        await db.commit()

    linked = await _get_linked_matters(contact_id, tid)
    return templates.TemplateResponse(
        request, "contacts/_linked_matters_table.html",
        _ctx(request, sess,
             linked_matters=linked,
             contact_id=contact_id,
             tenant_id=tid),
    )


@router.post("/{contact_id}/unlink-matter/{link_id}", response_class=HTMLResponse)
async def unlink_matter(request: Request, contact_id: int, link_id: int):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                DELETE FROM matter_contacts
                WHERE id = :id
                  AND contact_id = :cid
                  AND TRIM(tenant_id) = :tid
            """),
            {"id": link_id, "cid": contact_id, "tid": tid},
        )
        await db.commit()
    linked = await _get_linked_matters(contact_id, tid)
    return templates.TemplateResponse(
        request, "contacts/_linked_matters_table.html",
        _ctx(request, sess,
             linked_matters=linked,
             contact_id=contact_id,
             tenant_id=tid),
    )


# --------------------------------------------------------------------------
# Typeahead endpoints
# --------------------------------------------------------------------------
@router.get("/api/matter-search", response_class=HTMLResponse)
async def matter_search(request: Request, q: str = ""):
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if len(q.strip()) < 2:
        return HTMLResponse("")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, matter_number, matter_name
                FROM matters
                WHERE TRIM(tenant_id) = :tid
                  AND (matter_number ILIKE :p OR matter_name ILIKE :p)
                ORDER BY matter_name
                LIMIT 20
            """),
            {"tid": tid, "p": f"%{q.strip()}%"},
        )
        rows = r.fetchall()
    parts = []
    for row in rows:
        label = f"{row.matter_number} {row.matter_name}".strip()
        parts.append(
            f'<option value="{row.id}" data-label="{label}">{label}</option>'
        )
    return HTMLResponse("\n".join(parts))


@router.get("/api/client-search", response_class=HTMLResponse)
async def client_search(request: Request, q: str = ""):
    """Typeahead for the Assign-to-Client modal."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if len(q.strip()) < 2:
        return HTMLResponse("")
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, client_name, client_number
                FROM clients
                WHERE TRIM(tenant_id) = :tid
                  AND is_active = TRUE
                  AND client_name ILIKE :p
                ORDER BY client_name
                LIMIT 20
            """),
            {"tid": tid, "p": f"%{q.strip()}%"},
        )
        rows = r.fetchall()
    parts = []
    for row in rows:
        label = row.client_name + (f" ({row.client_number})" if row.client_number else "")
        parts.append(
            f'<option value="{row.id}" data-label="{label}">{label}</option>'
        )
    return HTMLResponse("\n".join(parts))


@router.get("/api/matters-for-client", response_class=HTMLResponse)
async def matters_for_client(request: Request, client_id: str = ""):
    """Once a client is picked in the assign modal, show that client's matters
    so the user can pick one (or pick 'create new')."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    if not client_id:
        return HTMLResponse("")

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, matter_number, matter_name, status
                FROM matters
                WHERE TRIM(tenant_id) = :tid
                  AND client_id = CAST(:cid AS uuid)
                ORDER BY
                  CASE LOWER(COALESCE(status,'')) WHEN 'active' THEN 0
                                                  WHEN 'open' THEN 0
                                                  ELSE 1 END,
                  matter_name
            """),
            {"tid": tid, "cid": client_id},
        )
        matters = r.fetchall()

    if not matters:
        return HTMLResponse(
            '<div style="font-size:12px; color:var(--muted); padding:8px;">'
            'This client has no matters yet. Create one below.'
            '</div>'
        )

    parts = ['<div style="display:flex; flex-direction:column; gap:4px;">']
    for m in matters:
        label = f"{m.matter_number or ''} {m.matter_name}".strip()
        parts.append(
            f'<label style="display:flex; align-items:center; gap:8px; padding:6px 10px; '
            f'border:1px solid var(--border); border-radius:5px; cursor:pointer; font-size:12px;">'
            f'<input type="radio" name="matter_id" value="{m.id}">'
            f'<span style="flex:1;">{label}</span>'
            f'<span style="font-size:10px; color:var(--muted); text-transform:uppercase;">'
            f'{m.status or ""}</span>'
            f'</label>'
        )
    parts.append('</div>')
    return HTMLResponse("\n".join(parts))


# --------------------------------------------------------------------------
# Assign-to-Client flow
# --------------------------------------------------------------------------
@router.get("/{contact_id}/assign-client", response_class=HTMLResponse)
async def assign_client_modal(request: Request, contact_id: int):
    """Render the assign-to-client modal (HTMX-loaded into a slot on the
    contact-edit page)."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return templates.TemplateResponse(
        request, "contacts/_assign_client_modal.html",
        _ctx(request, sess,
             contact=contact,
             contact_id=contact_id,
             roles=ASSIGNABLE_ROLES),
    )


@router.post("/{contact_id}/assign-client", response_class=HTMLResponse)
async def assign_client(
    request: Request,
    contact_id: int,
    client_id: str = Form(""),
    new_client_name: str = Form(""),
    matter_id: str = Form(""),
    new_matter_name: str = Form(""),
    new_matter_number: str = Form(""),
    role: str = Form("client"),
    is_primary: str = Form("Y"),
):
    """Process the assign-to-client form. Creates client/matter as needed,
    writes matter_contacts row. Returns the refreshed linked-matters fragment."""
    sess = await _require_admin(request)
    tid = sess["tenant_id"]
    contact = await _get_contact(contact_id, tid)
    if not contact:
        raise HTTPException(404, "Contact not found")

    if role not in ASSIGNABLE_ROLES:
        role = "client"
    if is_primary not in ("Y", "N"):
        is_primary = "Y"

    new_client_name = _strip(new_client_name)
    new_matter_name = _strip(new_matter_name)
    new_matter_number = _strip(new_matter_number) or None
    matter_id = _strip(matter_id) or None
    client_id = _strip(client_id) or None

    async with AsyncSessionLocal() as db:
        # 1. Resolve / create client
        if not client_id and not new_client_name:
            raise HTTPException(400, "Pick an existing client or enter a new client name.")

        if not client_id and new_client_name:
            r = await db.execute(
                text("""
                    INSERT INTO clients (tenant_id, client_name, is_active)
                    VALUES (:tid, :name, TRUE)
                    RETURNING id
                """),
                {"tid": tid, "name": new_client_name[:500]},
            )
            client_id = str(r.scalar())
            log.info("assign_client: created new client %s (%s)",
                     new_client_name, client_id)

        # 2. Resolve / create matter
        if not matter_id and not new_matter_name:
            raise HTTPException(400, "Pick an existing matter or enter a new matter name.")

        if not matter_id and new_matter_name:
            r = await db.execute(
                text("""
                    INSERT INTO matters
                        (tenant_id, client_id, matter_name, matter_number,
                         status, created_at, updated_at)
                    VALUES
                        (:tid, CAST(:cid AS uuid), :name, :num,
                         'active', NOW(), NOW())
                    RETURNING id
                """),
                {
                    "tid": tid, "cid": client_id,
                    "name": new_matter_name[:500],
                    "num": new_matter_number,
                },
            )
            matter_id = str(r.scalar())
            log.info("assign_client: created new matter %s (%s) on client %s",
                     new_matter_name, matter_id, client_id)
        else:
            # Verify the picked matter actually belongs to the picked client
            r = await db.execute(
                text("""
                    SELECT 1 FROM matters
                    WHERE id = CAST(:mid AS uuid)
                      AND client_id = CAST(:cid AS uuid)
                      AND TRIM(tenant_id) = :tid
                """),
                {"mid": matter_id, "cid": client_id, "tid": tid},
            )
            if not r.fetchone():
                raise HTTPException(400, "Selected matter does not belong to selected client.")

        # 3. Write or update matter_contacts row
        r = await db.execute(
            text("""
                SELECT id FROM matter_contacts
                WHERE contact_id = :cid
                  AND matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"cid": contact_id, "mid": matter_id, "tid": tid},
        )
        existing = r.fetchone()
        if existing:
            await db.execute(
                text("""
                    UPDATE matter_contacts
                       SET role = :role, is_primary = :primary
                     WHERE id = :id
                """),
                {"role": role, "primary": is_primary, "id": existing[0]},
            )
        else:
            await db.execute(
                text("""
                    INSERT INTO matter_contacts
                        (tenant_id, matter_id, contact_id, role, is_primary)
                    VALUES
                        (:tid, CAST(:mid AS uuid), :cid, :role, :primary)
                """),
                {
                    "tid": tid, "mid": matter_id, "cid": contact_id,
                    "role": role, "primary": is_primary,
                },
            )

        await db.commit()

    linked = await _get_linked_matters(contact_id, tid)
    # Return a wrapper that swaps BOTH the linked-matters table AND clears
    # the modal slot. We use OOB swap for the modal close.
    return templates.TemplateResponse(
        request, "contacts/_assign_client_response.html",
        _ctx(request, sess,
             linked_matters=linked,
             contact_id=contact_id,
             tenant_id=tid),
    )
