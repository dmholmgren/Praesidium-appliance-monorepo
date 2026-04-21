"""Safe audit wrapper — matches Chat 0's actual write_audit() signature.

DMS Lesson 7.1: Chat 0's write_audit() takes positional args:
    write_audit(tenant_session, action, table_name, record_id, 
                old_values=None, new_values=None, user_id=None)

NOT keyword args like (tenant_id=, module=, source=).

All audit calls are wrapped in try/except so failures never block operations.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def safe_audit(
    ts,
    action: str,
    table_name: str,
    record_id: Any,
    *,
    old_values: dict | None = None,
    new_values: dict | None = None,
    user_id: int | None = None,
) -> None:
    """Wrap write_audit in try/except — audit failure must never block ops."""
    try:
        from core.audit import write_audit
        await write_audit(
            ts,              # positional: TenantSession
            action,          # positional: "CREATE", "UPDATE", "DELETE", etc.
            table_name,      # positional: table name string
            str(record_id),  # positional: record_id as string (Chat 0 uses VARCHAR(100))
            old_values,      # keyword-safe: old values dict
            new_values,      # keyword-safe: new values dict
            user_id,         # keyword-safe: user id
        )
    except Exception as e:
        logger.warning(
            "Audit write failed (non-blocking): %s on %s.%s — %s",
            action, table_name, record_id, str(e)[:200],
        )
