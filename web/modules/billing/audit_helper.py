"""Safe audit wrapper for billing module.

DMS Lesson Learned (Section 7.1): Chat 0's write_audit() actual signature is:
    write_audit(tenant_session, action, table_name, record_id, ...)
NOT the keyword-argument form that Claude tends to generate:
    write_audit(db, tenant_id=..., module=..., source=...)

This wrapper calls Chat 0's real signature and catches any mismatch
so audit failures never block billing operations.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def safe_audit(
    db,
    action: str = "",
    table_name: str = "",
    record_id: str = "",
    old_value: Optional[dict] = None,
    new_value: Optional[dict] = None,
    user_id: Optional[str] = None,
    # Accept DMS-style kwargs so existing call sites don't break
    tenant_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    module: Optional[str] = None,
    source: Optional[str] = None,
):
    """Write an audit record using Chat 0's actual write_audit signature.

    Wraps in try/except so audit failures never block billing operations.
    Falls back to structured logging if write_audit is unavailable.
    """
    # Normalize: accept either (action, table_name, record_id) positional
    # or (action=, entity_type=, entity_id=) keyword style
    _table = table_name or entity_type or "unknown"
    _record = record_id or entity_id or ""

    try:
        from core.audit import write_audit
        # Chat 0 actual signature: write_audit(session, action, table_name, record_id, ...)
        write_audit(
            db,                   # TenantSession — positional arg 1
            action,               # e.g. "billing.client.create" — positional arg 2
            _table,               # e.g. "clients" — positional arg 3
            _record,              # e.g. the client UUID — positional arg 4
            old_value=old_value,
            new_value=new_value,
            user_id=user_id,
        )
    except TypeError as e:
        # Signature mismatch — log it, don't crash
        logger.warning(
            f"write_audit signature mismatch (needs Chat 0 fix): {e}. "
            f"action={action}, table={table_name}, record={record_id}"
        )
    except ImportError:
        logger.warning(
            f"core.audit not available. action={action}, "
            f"table={table_name}, record={record_id}"
        )
    except Exception as e:
        logger.error(f"Audit write failed: {e}")
