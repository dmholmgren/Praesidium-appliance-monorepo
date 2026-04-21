# modules/dms/widget_service.py
"""
Lightweight DMS service functions registered in widget_registry.data_source.
These are widget-scoped queries only — do not modify existing DMS routes or service.

data_source registry value:
    modules.dms.widget_service.get_widget_recent_documents
"""

import logging
from typing import Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def get_widget_recent_documents(
    tenant_id: str,
    user_id: int,
    matter_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """
    Returns up to 5 most recent DMS documents for the given scope.
    Called by the widget render engine via data_source resolution.

    Scope:
      - matter_id provided: scoped to that matter.
      - matter_id absent:   firm-wide, most recent 5 across all matters for this tenant.

    Returns:
        {"documents": [{"file_name": ..., "doc_type": ..., "doc_date": ...,
                        "custodian": ..., "matter_name": ...}, ...]}
    """
    tenant_id = tenant_id.strip()

    if matter_id:
        query = text(
            """
            SELECT
                d.file_name,
                d.doc_type,
                d.doc_date,
                d.custodian,
                m.matter_name
            FROM dms_documents d
            JOIN matters m
              ON d.matter_id = m.id
             AND TRIM(m.tenant_id) = :tenant_id
            WHERE TRIM(d.tenant_id) = :tenant_id
              AND d.matter_id = :matter_id
            ORDER BY d.created_at DESC
            LIMIT 5
            """
        )
        params = {"tenant_id": tenant_id, "matter_id": matter_id}
    else:
        query = text(
            """
            SELECT
                d.file_name,
                d.doc_type,
                d.doc_date,
                d.custodian,
                m.matter_name
            FROM dms_documents d
            JOIN matters m
              ON d.matter_id = m.id
             AND TRIM(m.tenant_id) = :tenant_id
            WHERE TRIM(d.tenant_id) = :tenant_id
            ORDER BY d.created_at DESC
            LIMIT 5
            """
        )
        params = {"tenant_id": tenant_id}

    async with AsyncSessionLocal() as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    documents = [dict(r) for r in rows]
    return {"documents": documents}
