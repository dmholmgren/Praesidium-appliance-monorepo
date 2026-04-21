"""
Audit helper — write_audit() must be called on every database write.

This is Mandatory Rule 5: All DB writes go through write_audit() via core/audit.py.
"""

from datetime import datetime
from typing import Any, Optional

from core.db.base import TenantSession
from core.models.audit import AuditLog


def write_audit(
    tenant_session: TenantSession,
    action: str,
    table_name: str,
    record_id: Any,
    old_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
    user_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """
    Write an audit log entry.

    Args:
        tenant_session: The active TenantSession
        action: One of 'create', 'update', 'delete'
        table_name: Database table name
        record_id: Primary key of the affected record
        old_values: Previous values (for update/delete)
        new_values: New values (for create/update)
        user_id: ID of the user performing the action
        ip_address: Client IP address
        user_agent: Client user agent string
        request_id: Unique request correlation ID
    """
    log_entry = AuditLog(
        tenant_id=tenant_session.tenant_id,
        user_id=user_id,
        action=action,
        table_name=table_name,
        record_id=str(record_id),
        old_values=old_values,
        new_values=new_values,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        created_at=datetime.utcnow(),
    )
    # Use raw session to avoid TenantSession auto-setting tenant_id
    # (AuditLog already has it set explicitly)
    tenant_session.raw_session.add(log_entry)
