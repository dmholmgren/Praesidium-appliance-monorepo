"""
Contacts Dashboard API — JSON endpoints for PI Dashboard Contacts tab
=====================================================================
GET /api/v1/dashboard/contacts?q=&page=1&per_page=50&type=&sort=name
GET /api/v1/dashboard/contacts/stats
GET /api/v1/dashboard/contacts/{id}
POST /api/v1/dashboard/contacts
PUT /api/v1/dashboard/contacts/{id}
DELETE /api/v1/dashboard/contacts/{id}
POST /api/v1/dashboard/contacts/{id}/merge
GET /api/v1/dashboard/contacts/proposals
POST /api/v1/dashboard/contacts/proposals/{mc_id}/accept
POST /api/v1/dashboard/contacts/proposals/{mc_id}/dismiss
"""
from __future__ import annotations
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dashboard/contacts", tags=["contacts-api"])

def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


@router.get("/stats")
async def contacts_stats(request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            total = (await session.execute(sa_text(
                "SELECT COUNT(*) FROM contacts WHERE TRIM(tenant_id)=TRIM(:tid)"), {"tid": tid})).scalar() or 0
            with_email = (await session.execute(sa_text(
                "SELECT COUNT(*) FROM contacts WHERE TRIM(tenant_id)=TRIM(:tid) AND email IS NOT NULL AND email != ''"), {"tid": tid})).scalar() or 0
            with_phone = (await session.execute(sa_text(
                "SELECT COUNT(*) FROM contacts WHERE TRIM(tenant_id)=TRIM(:tid) AND phone IS NOT NULL AND phone != ''"), {"tid": tid})).scalar() or 0
            linked = (await session.execute(sa_text(
                "SELECT COUNT(DISTINCT contact_id) FROM matter_contacts WHERE TRIM(tenant_id)=TRIM(:tid) AND status='confirmed'"), {"tid": tid})).scalar() or 0
            proposals = (await session.execute(sa_text(
                "SELECT COUNT(*) FROM matter_contacts WHERE TRIM(tenant_id)=TRIM(:tid) AND status='proposed'"), {"tid": tid})).scalar() or 0
            by_type = (await session.execute(sa_text(
                "SELECT contact_type, COUNT(*) AS cnt FROM contacts WHERE TRIM(tenant_id)=TRIM(:tid) GROUP BY contact_type ORDER BY cnt DESC"), {"tid": tid})).mappings().fetchall()
        return JSONResponse({
            "total": total, "with_email": with_email, "with_phone": with_phone,
            "linked_to_matters": linked, "pending_proposals": proposals,
            "by_type": [{"type": r["contact_type"] or "unknown", "count": r["cnt"]} for r in by_type],
        })
    except Exception as exc:
        logger.error("contacts_stats: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/proposals")
async def contacts_proposals(request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sa_text("""
                SELECT mc.id AS mc_id, mc.matter_id::text, mc.role, mc.source, mc.ai_summary, mc.category,
                       c.id AS contact_id, c.full_name, c.email, c.phone, c.company, c.firm_name,
                       m.matter_name, m.matter_number
                FROM matter_contacts mc
                JOIN contacts c ON mc.contact_id = c.id AND TRIM(c.tenant_id) = TRIM(:tid)
                JOIN matters m ON mc.matter_id = m.id AND TRIM(m.tenant_id) = TRIM(:tid)
                WHERE TRIM(mc.tenant_id) = TRIM(:tid) AND mc.status = 'proposed'
                ORDER BY m.matter_name, c.full_name
                LIMIT 200
            """), {"tid": tid})).mappings().fetchall()
        return JSONResponse({"proposals": [dict(r) for r in rows]})
    except Exception as exc:
        logger.error("contacts_proposals: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/proposals/{mc_id}/accept")
async def accept_proposal(mc_id: int, request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "UPDATE matter_contacts SET status='confirmed' WHERE id=:id AND TRIM(tenant_id)=TRIM(:tid)"),
                {"id": mc_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("accept_proposal: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/proposals/{mc_id}/dismiss")
async def dismiss_proposal(mc_id: int, request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "DELETE FROM matter_contacts WHERE id=:id AND TRIM(tenant_id)=TRIM(:tid) AND status='proposed'"),
                {"id": mc_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("dismiss_proposal: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("")
async def contacts_list(request: Request, q: str = "", page: int = 1, per_page: int = 50,
                        type: str = "", sort: str = "name"):
    tid = _tid(request)
    offset = (max(1, page) - 1) * per_page
    where = ["TRIM(c.tenant_id) = TRIM(:tid)"]
    params = {"tid": tid, "lim": per_page, "off": offset}

    if q:
        where.append("(c.full_name ILIKE :q OR c.email ILIKE :q OR c.phone ILIKE :q OR c.company ILIKE :q OR c.firm_name ILIKE :q)")
        params["q"] = f"%{q}%"
    if type:
        where.append("c.contact_type = :ctype")
        params["ctype"] = type

    order = {
        "name": "c.full_name ASC",
        "email": "c.email ASC NULLS LAST",
        "company": "c.company ASC NULLS LAST",
        "recent": "c.updated_at DESC",
    }.get(sort, "c.full_name ASC")

    w = " AND ".join(where)
    try:
        async with AsyncSessionLocal() as session:
            total = (await session.execute(sa_text(
                f"SELECT COUNT(*) FROM contacts c WHERE {w}"), params)).scalar() or 0
            rows = (await session.execute(sa_text(f"""
                SELECT c.id, c.full_name, c.company, c.contact_type, c.email, c.phone,
                       c.address1, c.city, c.state, c.bar_number, c.firm_name, c.notes,
                       c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM matter_contacts mc WHERE mc.contact_id = c.id
                        AND TRIM(mc.tenant_id) = TRIM(:tid) AND mc.status='confirmed') AS matter_count
                FROM contacts c WHERE {w}
                ORDER BY {order} LIMIT :lim OFFSET :off
            """), params)).mappings().fetchall()

            contacts = []
            for r in rows:
                d = dict(r)
                for k in ("created_at", "updated_at"):
                    if d.get(k) and hasattr(d[k], "isoformat"):
                        d[k] = d[k].isoformat()
                contacts.append(d)

        return JSONResponse({
            "contacts": contacts, "total": total, "page": page,
            "per_page": per_page, "total_pages": (total + per_page - 1) // per_page,
        })
    except Exception as exc:
        logger.error("contacts_list: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/{contact_id}")
async def contact_detail(contact_id: int, request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(sa_text("""
                SELECT c.* FROM contacts c WHERE c.id = :cid AND TRIM(c.tenant_id) = TRIM(:tid)
            """), {"cid": contact_id, "tid": tid})).mappings().fetchone()
            if not row:
                return JSONResponse({"error": "Not found"}, status_code=404)

            matters = (await session.execute(sa_text("""
                SELECT mc.id AS mc_id, mc.role, mc.status, mc.source, mc.category, mc.ai_summary,
                       m.id::text AS matter_id, m.matter_name, m.matter_number, m.status AS matter_status
                FROM matter_contacts mc JOIN matters m ON mc.matter_id = m.id AND TRIM(m.tenant_id) = TRIM(:tid)
                WHERE mc.contact_id = :cid AND TRIM(mc.tenant_id) = TRIM(:tid)
                ORDER BY m.matter_name
            """), {"cid": contact_id, "tid": tid})).mappings().fetchall()

        d = dict(row)
        for k in ("created_at", "updated_at"):
            if d.get(k) and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        d["matters"] = [dict(m) for m in matters]
        return JSONResponse(d)
    except Exception as exc:
        logger.error("contact_detail: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("")
async def create_contact(request: Request):
    tid = _tid(request)
    body = await request.json()
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sa_text("""
                INSERT INTO contacts (tenant_id, full_name, company, contact_type, email, phone,
                    address1, city, state, bar_number, firm_name, notes)
                VALUES (:tid, :full_name, :company, :contact_type, :email, :phone,
                    :address1, :city, :state, :bar_number, :firm_name, :notes)
                RETURNING id
            """), {
                "tid": tid,
                "full_name": body.get("full_name", "").strip(),
                "company": body.get("company", "").strip() or None,
                "contact_type": body.get("contact_type", "manual"),
                "email": body.get("email", "").strip() or None,
                "phone": body.get("phone", "").strip() or None,
                "address1": body.get("address1", "").strip() or None,
                "city": body.get("city", "").strip() or None,
                "state": body.get("state", "").strip() or None,
                "bar_number": body.get("bar_number", "").strip() or None,
                "firm_name": body.get("firm_name", "").strip() or None,
                "notes": body.get("notes", "").strip() or None,
            })
            new_id = result.scalar()
            await session.commit()
        return JSONResponse({"ok": True, "id": new_id})
    except Exception as exc:
        logger.error("create_contact: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.put("/{contact_id}")
async def update_contact(contact_id: int, request: Request):
    tid = _tid(request)
    body = await request.json()
    sets = []
    params = {"cid": contact_id, "tid": tid}
    for field in ("full_name", "company", "contact_type", "email", "phone",
                  "address1", "city", "state", "bar_number", "firm_name", "notes"):
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = (body[field] or "").strip() or None
    if not sets:
        return JSONResponse({"error": "No fields to update"}, status_code=400)
    sets.append("updated_at = NOW()")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                f"UPDATE contacts SET {', '.join(sets)} WHERE id = :cid AND TRIM(tenant_id) = TRIM(:tid)"), params)
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("update_contact: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.delete("/{contact_id}")
async def delete_contact(contact_id: int, request: Request):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            # Remove matter_contacts first
            await session.execute(sa_text(
                "DELETE FROM matter_contacts WHERE contact_id = :cid AND TRIM(tenant_id) = TRIM(:tid)"),
                {"cid": contact_id, "tid": tid})
            await session.execute(sa_text(
                "DELETE FROM contacts WHERE id = :cid AND TRIM(tenant_id) = TRIM(:tid)"),
                {"cid": contact_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error("delete_contact: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/{contact_id}/merge")
async def merge_contact(contact_id: int, request: Request):
    """Merge contact_id INTO target_id — reassigns all matter_contacts, then deletes source."""
    tid = _tid(request)
    body = await request.json()
    target_id = body.get("target_id")
    if not target_id or target_id == contact_id:
        return JSONResponse({"error": "Invalid target_id"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            # Reassign matter_contacts from source → target (skip dupes)
            await session.execute(sa_text("""
                UPDATE matter_contacts SET contact_id = :target
                WHERE contact_id = :source AND TRIM(tenant_id) = TRIM(:tid)
                  AND (matter_id, :target) NOT IN (
                      SELECT matter_id, contact_id FROM matter_contacts
                      WHERE TRIM(tenant_id) = TRIM(:tid) AND contact_id = :target)
            """), {"source": contact_id, "target": target_id, "tid": tid})
            # Delete remaining dupes
            await session.execute(sa_text(
                "DELETE FROM matter_contacts WHERE contact_id = :source AND TRIM(tenant_id) = TRIM(:tid)"),
                {"source": contact_id, "tid": tid})
            # Delete source contact
            await session.execute(sa_text(
                "DELETE FROM contacts WHERE id = :source AND TRIM(tenant_id) = TRIM(:tid)"),
                {"source": contact_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True, "merged_into": target_id})
    except Exception as exc:
        logger.error("merge_contact: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
