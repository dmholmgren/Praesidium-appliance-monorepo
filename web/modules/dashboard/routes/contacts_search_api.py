"""
Contact search API for typeahead.
GET /api/v1/contacts/search?q=smith&limit=8
Fuzzy search using pg_trgm similarity + ILIKE.
"""
from fastapi import APIRouter, Request, Query
from core.db.base import AsyncSessionLocal
from sqlalchemy import text

router = APIRouter(prefix="/api/v1/contacts", tags=["contacts"])


@router.get("/search")
async def search_contacts(request: Request, q: str = Query(..., min_length=1), limit: int = Query(10, le=25)):
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                WITH ranked AS (
                    SELECT id, full_name, email, company, firm_name, contact_type, phone,
                        GREATEST(
                            COALESCE(similarity(full_name, :q), 0),
                            COALESCE(similarity(email, :q), 0),
                            COALESCE(similarity(company, :q), 0),
                            COALESCE(similarity(firm_name, :q), 0)
                        ) AS sim,
                        CASE
                            WHEN full_name ILIKE :exact THEN 0
                            WHEN email ILIKE :exact THEN 0
                            WHEN full_name ILIKE :starts THEN 1
                            WHEN email ILIKE :starts THEN 1
                            WHEN full_name ILIKE :contains THEN 2
                            WHEN email ILIKE :contains THEN 2
                            WHEN company ILIKE :contains THEN 3
                            WHEN firm_name ILIKE :contains THEN 3
                            ELSE 4
                        END AS rank
                    FROM contacts
                    WHERE TRIM(tenant_id) = :tid
                      AND (
                          full_name ILIKE :contains
                          OR email ILIKE :contains
                          OR company ILIKE :contains
                          OR firm_name ILIKE :contains
                          OR similarity(full_name, :q) > 0.15
                          OR similarity(COALESCE(email,''), :q) > 0.15
                      )
                )
                SELECT id, full_name, email, company, firm_name, contact_type, phone,
                       sim, rank
                FROM ranked
                WHERE email IS NOT NULL AND email != ''
                ORDER BY rank, sim DESC, full_name
                LIMIT :lim
            """),
            {
                "tid": tenant_id,
                "q": q,
                "exact": q,
                "starts": f"{q}%",
                "contains": f"%{q}%",
                "lim": limit,
            },
        )
        rows = [dict(r) for r in result.mappings().all()]
        # Clean up internal fields
        for r in rows:
            r.pop("sim", None)
            r.pop("rank", None)

    return {"contacts": rows, "query": q}
