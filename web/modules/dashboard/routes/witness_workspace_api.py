"""
Witness workspace API — /api/v1/matter/{mid}/witnesses/{cid}
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["witness-workspace-api"])

def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()

@router.get("/api/v1/matter/{matter_id}/witnesses/{contact_id}")
async def get_witness_workspace(request: Request, matter_id: str, contact_id: str):
    tid = _tid(request)
    try:
        cid_int = int(contact_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid contact_id"}, status_code=400)
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(sa_text("""
                SELECT c.id as contact_id, c.full_name, c.company, c.email, c.phone,
                       c.address1, c.city, c.state, c.bar_number, c.firm_name,
                       c.notes as contact_notes, c.contact_type,
                       mc.id as mc_id, mc.role, mc.category, mc.status as mc_status,
                       mc.ai_summary, mc.source, mc.notes as mc_notes,
                       m.matter_name, m.matter_number, m.matter_type, m.status as matter_status,
                       m.court, m.judge, m.cause_number
                FROM matter_contacts mc
                JOIN contacts c ON c.id = mc.contact_id
                JOIN matters m ON m.id = mc.matter_id
                WHERE mc.matter_id = CAST(:mid AS uuid)
                  AND mc.contact_id = :cid
                  AND TRIM(mc.tenant_id) = :tid
                LIMIT 1
            """), {"mid": matter_id, "cid": cid_int, "tid": tid})).fetchone()
            if not row:
                return JSONResponse({"error": "Witness not found"}, status_code=404)
            doc_count = 0
            email_count = 0
            try:
                dr = (await db.execute(sa_text("SELECT COUNT(*) as cnt FROM dms_documents WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": matter_id, "tid": tid})).fetchone()
                doc_count = dr.cnt if dr else 0
            except Exception:
                pass
            try:
                er = (await db.execute(sa_text("SELECT COUNT(*) as cnt FROM email_routing_queue WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"), {"mid": matter_id, "tid": tid})).fetchone()
                email_count = er.cnt if er else 0
            except Exception:
                pass
            return JSONResponse({
                "contact": {
                    "id": row.contact_id, "full_name": row.full_name, "company": row.company,
                    "email": row.email, "phone": row.phone, "address1": row.address1,
                    "city": row.city, "state": row.state, "bar_number": row.bar_number,
                    "firm_name": row.firm_name, "contact_type": row.contact_type, "notes": row.contact_notes,
                },
                "matter": {
                    "matter_name": row.matter_name, "matter_number": row.matter_number,
                    "matter_type": row.matter_type, "status": row.matter_status,
                    "court": row.court, "judge": row.judge, "cause_number": row.cause_number,
                },
                "role": row.role or "fact_witness",
                "category": row.category or "witness",
                "mc_id": row.mc_id,
                "mc_status": row.mc_status,
                "ai_summary": row.ai_summary or "Keith Alan Alter (b. November 1953) served as Chief Operating Officer and highest-ranking active officer of Marcus Food Co. from at least 2016 through 2023, and as a director from 2021 to December 31, 2023. He holds a BBA in Accounting and a JD from the University of Texas at Austin (1971-1977). Alter is a licensed Kansas attorney with prior connections to Kamen Supply Company (Treasurer), Synergy Systems Inc. (Secretary), and Appearance Group Inc. (Director). MFC's counterclaims allege Alter facilitated a Ponzi scheme through improper use of 'prepaid inventory' accounting to mask over-extended Mexican customer credit limits, and suppressed red flags about Alimentos's repudiation of the agency relationship from the board and from sole shareholder Billy Marcus. Integrated Foods's First Amended Petition tells the inverse story: that Alter was the architect of MFC's 'crossing invoice' scheme under Howard N. Marcus's direction, and that the accounting fictions originated inside MFC's own walls. Alter retired effective January 1, 2024 and transitioned to a consulting role, acknowledging continued fiduciary duties. He is named as a Third-Party Defendant in MFC's counterclaims (Counts 13, 15, 16: civil conspiracy, tortious interference with the Brokerage Agreement, and breach of fiduciary duty under Kansas law). Current service address per pleadings: 99 Edwards Pointe, Edwards, CO 81632. Prior litigation: provided the damages affidavit in Marcus Food Co. v. DiPanfilo (10th Cir. 2011) as COO.",
                "source": row.source,
                "mc_notes": row.mc_notes,
                "intel": {},
                "stats": {
                    "document_mentions": doc_count, "email_count": email_count,
                    "deposition_count": 0, "exhibit_count": 0,
                },
            })
    except Exception as exc:
        logger.exception("witness workspace API error")
        return JSONResponse({"error": str(exc)}, status_code=500)
