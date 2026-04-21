"""
modules/ediscovery/routes/partials.py
HTMX partial endpoints for eDiscovery tab content.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ediscovery/partials")

_env = Environment(
    loader=FileSystemLoader(["/app/modules/ediscovery/templates", "/app/core/templates"]),
    autoescape=select_autoescape(["html"]),
)

def _render(template_name: str, context: dict) -> str:
    try:
        return _env.get_template(template_name).render(context)
    except Exception as e:
        logger.error("Partial render error [%s]: %s", template_name, e)
        return f'<div style="padding:16px;color:#DC2626;font-size:12px;">⚠ Error: {e}</div>'

@router.get("/collections", response_class=HTMLResponse)
async def collections_partial(request: Request, matter_id: Optional[str] = None, user=Depends(get_current_user)):
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    collections = []
    matter = None
    try:
        async with AsyncSessionLocal() as session:
            params = {"tid": tenant_id}
            matter_clause = ""
            if matter_id:
                matter_clause = " AND ec.matter_id::text = :mid"
                params["mid"] = matter_id
                r = await session.execute(text("SELECT matter_name, matter_number FROM matters WHERE id::text = :mid AND TRIM(tenant_id) = :tid"), {"mid": matter_id, "tid": tenant_id})
                matter = r.mappings().first()
            result = await session.execute(text(f"""
                SELECT ec.id::text as id, COALESCE(ec.collection_name, 'Unnamed') as collection_name,
                    ec.status, ec.source_type, ec.source_party, ec.received_date,
                    ec.total_docs, ec.reviewed_docs, ec.created_at,
                    m.matter_name, m.matter_number, m.id::text as matter_id
                FROM ediscovery_collections ec
                LEFT JOIN matters m ON m.id = ec.matter_id AND TRIM(m.tenant_id) = TRIM(ec.tenant_id)
                WHERE TRIM(ec.tenant_id) = TRIM(:tid) {matter_clause}
                ORDER BY ec.created_at DESC LIMIT 50
            """), params)
            collections = [dict(r) for r in result.mappings().fetchall()]
    except Exception as e:
        logger.error(f"collections_partial error: {e}")
    return HTMLResponse(_render("ediscovery/partials/collections_partial.html", {"collections": collections, "matter": matter, "user": user}))

@router.get("/review", response_class=HTMLResponse)
async def review_partial(request: Request, user=Depends(get_current_user)):
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    matters = []
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT m.id::text as matter_id, m.matter_name, m.matter_number,
                    COUNT(ec.id) as collection_count, SUM(ec.total_docs) as total_docs,
                    SUM(ec.reviewed_docs) as reviewed_docs, BOOL_OR(ec.status = 'review_ready') as has_ready,
                    (SELECT ec2.id::text FROM ediscovery_collections ec2
                     WHERE ec2.matter_id = m.id AND TRIM(ec2.tenant_id) = TRIM(m.tenant_id)
                     ORDER BY ec2.created_at DESC LIMIT 1) as latest_collection_id,
                    (SELECT ec2.display_name FROM ediscovery_collections ec2
                     WHERE ec2.matter_id = m.id AND TRIM(ec2.tenant_id) = TRIM(m.tenant_id)
                     ORDER BY ec2.created_at DESC LIMIT 1) as latest_collection_name
                FROM matters m
                JOIN ediscovery_collections ec ON ec.matter_id = m.id AND TRIM(ec.tenant_id) = TRIM(m.tenant_id)
                WHERE TRIM(m.tenant_id) = :tid
                GROUP BY m.id, m.matter_name, m.matter_number
                ORDER BY SUM(ec.total_docs) DESC NULLS LAST LIMIT 20
            """), {"tid": tenant_id})
            matters = [dict(r) for r in result.mappings().fetchall()]
    except Exception as e:
        logger.error(f"review_partial error: {e}")
    return HTMLResponse(_render("ediscovery/partials/review_partial.html", {"matters": matters, "user": user}))

@router.get("/matters/{matter_id}/collections-inline", response_class=HTMLResponse)
async def collections_inline(request: Request, matter_id: str):
    """Inline collection list for the review partial expand row."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    collections = []
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(text("""
                SELECT ec.id::text, ec.display_name, ec.source_party,
                       ec.status, ec.total_docs, ec.reviewed_docs,
                       ec.received_date, ec.stated_bates_range
                FROM ediscovery_collections ec
                WHERE ec.matter_id = CAST(:mid AS uuid)
                  AND TRIM(ec.tenant_id) = :tid
                ORDER BY ec.created_at DESC
            """), {"mid": matter_id, "tid": tenant_id})
            collections = [dict(r) for r in r.mappings().fetchall()]
    except Exception as e:
        logger.error(f"collections_inline error: {e}")

    rows = ""
    for c in collections:
        total = c.get("total_docs") or 0
        reviewed = c.get("reviewed_docs") or 0
        pct = int((reviewed / total * 100)) if total > 0 else 0
        rows += f"""
        <div style="display:flex; align-items:center; gap:12px; padding:6px 0;
                    border-bottom:1px solid #f1f5f9;">
          <div style="flex:1; min-width:0;">
            <a href="/ediscovery/collections/{c['id']}/review"
               style="font-size:12px; font-weight:500; color:var(--primary);
                      text-decoration:none;">
              {c.get('display_name', 'Collection')}
            </a>
            <div style="font-size:10px; color:var(--muted); margin-top:1px;">
              {c.get('source_party') or ''}{' · ' if c.get('source_party') else ''}{total} docs · {reviewed} reviewed
            </div>
          </div>
          <div style="min-width:80px;">
            <div style="height:3px; background:#E5E7EB; border-radius:3px; overflow:hidden;">
              <div style="height:100%; background:var(--primary); border-radius:3px; width:{pct}%;"></div>
            </div>
          </div>
          <a href="/ediscovery/collections/{c['id']}/review"
             style="font-size:12px; font-weight:600; color:var(--primary);
                    text-decoration:none; white-space:nowrap; flex-shrink:0;">
            Review →
          </a>
        </div>"""

    if not rows:
        rows = '<div style="font-size:11px; color:var(--muted);">No collections found.</div>'

    return HTMLResponse(f'<div style="display:flex; flex-direction:column; gap:0;">{rows}</div>')


@router.get("/productions", response_class=HTMLResponse)
async def productions_partial(request: Request, user=Depends(get_current_user)):
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    productions = []
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT p.id::text, p.production_name, p.status, p.doc_count,
                    p.bates_prefix, p.bates_start, p.bates_end, p.created_at, p.updated_at,
                    m.matter_name, m.matter_number
                FROM ediscovery_productions p
                LEFT JOIN matters m ON m.id = p.matter_id
                WHERE TRIM(p.tenant_id) = :tid
                ORDER BY p.created_at DESC LIMIT 20
            """), {"tid": tenant_id})
            productions = [dict(r) for r in result.mappings().fetchall()]
    except Exception as e:
        logger.error(f"productions_partial error: {e}")
    return HTMLResponse(_render("ediscovery/partials/productions_partial.html", {"productions": productions, "user": user}))
