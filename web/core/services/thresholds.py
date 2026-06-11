"""
Praesidium — Processing Thresholds Accessor

Read side of the processing_thresholds table (migration 0048). Resolves the
tunable pipeline gates (extraction escalation, folder-match bands, OCR gate,
etc.) with the same most-specific-wins cascade used by ai_model_routing and
prompt resolution:

    matter override  >  tenant default  >  global default

A row's specificity is read off its (tenant_id, matter_id) nullability:
    matter_id set                       -> matter override   (most specific)
    matter_id NULL, tenant_id set        -> tenant default
    matter_id NULL, tenant_id NULL       -> global default    (least specific)

Reads are uncached by design — a tuning change in tenant admin (or a per-matter
override) takes effect on the next call, no restart, matching the engine's
read-fresh-per-document discipline.

Conventions mirror modules/intelligence/anthropic_adapter.py exactly:
AsyncSessionLocal, text() queries, TRIM(tenant_id) = :tid, CAST(:mid AS uuid),
Decimal numerics.

Values are returned as Decimal (the column is NUMERIC). Comparing a Decimal
against a float confidence works directly (e.g. `conf < threshold`); if a
caller needs to do float arithmetic, wrap with float(...).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)

__all__ = [
    "get_threshold",
    "get_thresholds",
    "get_threshold_sync",
    "get_thresholds_sync",
]

# Cascade predicate + ordering shared by both queries. A matter row matches on
# its globally-unique matter_id alone; tenant/global rows must have matter_id
# NULL so a matter override never leaks across matters.
_CASCADE_WHERE = """
           AND (
                 matter_id = CAST(:mid AS uuid)
              OR (matter_id IS NULL AND TRIM(tenant_id) = :tid)
              OR (matter_id IS NULL AND tenant_id IS NULL)
               )
"""
_CASCADE_ORDER = """
         CASE WHEN matter_id IS NOT NULL THEN 0 ELSE 1 END,
         CASE WHEN tenant_id IS NOT NULL THEN 0 ELSE 1 END
"""


def _params(tenant_id: Optional[str], matter_id: Optional[Any]) -> dict:
    return {
        "tid": (tenant_id or "").strip(),
        "mid": (str(matter_id) if matter_id else None),
    }


async def get_threshold(
    module: str,
    purpose: str,
    threshold_key: str,
    tenant_id: Optional[str],
    matter_id: Optional[Any] = None,
    default: Optional[Any] = None,
) -> Optional[Decimal]:
    """
    Return the effective value for one gate, or `default` if no published row
    exists at any cascade level. matter_id=None resolves tenant>global only.
    """
    p = _params(tenant_id, matter_id)
    p.update({"module": module, "purpose": purpose, "key": threshold_key})

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT value
                  FROM processing_thresholds
                 WHERE module = :module
                   AND purpose = :purpose
                   AND threshold_key = :key
                   AND status = 'published'
                """
                + _CASCADE_WHERE
                + " ORDER BY " + _CASCADE_ORDER
                + " LIMIT 1"
            ),
            p,
        )
        row = result.first()

    if row is None:
        return default
    return row[0]


async def get_thresholds(
    module: str,
    purpose: str,
    tenant_id: Optional[str],
    matter_id: Optional[Any] = None,
) -> dict[str, Decimal]:
    """
    Return {threshold_key: value} for every gate under (module, purpose),
    each resolved independently through the cascade. One query, most-specific
    row per key via DISTINCT ON. Empty dict if nothing is defined.
    """
    p = _params(tenant_id, matter_id)
    p.update({"module": module, "purpose": purpose})

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT ON (threshold_key) threshold_key, value
                  FROM processing_thresholds
                 WHERE module = :module
                   AND purpose = :purpose
                   AND status = 'published'
                """
                + _CASCADE_WHERE
                + " ORDER BY threshold_key, " + _CASCADE_ORDER
            ),
            p,
        )
        rows = result.all()

    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Sync wrappers — for the standalone processing scripts (run_extraction.py,
# cascade_match_v2.py, the OCR pass) which run outside an event loop. Do NOT
# call these from inside async code; use the awaitables above.
# ---------------------------------------------------------------------------

def get_threshold_sync(
    module: str,
    purpose: str,
    threshold_key: str,
    tenant_id: Optional[str],
    matter_id: Optional[Any] = None,
    default: Optional[Any] = None,
) -> Optional[Decimal]:
    import asyncio
    return asyncio.run(
        get_threshold(module, purpose, threshold_key, tenant_id, matter_id, default)
    )


def get_thresholds_sync(
    module: str,
    purpose: str,
    tenant_id: Optional[str],
    matter_id: Optional[Any] = None,
) -> dict[str, Decimal]:
    import asyncio
    return asyncio.run(get_thresholds(module, purpose, tenant_id, matter_id))
