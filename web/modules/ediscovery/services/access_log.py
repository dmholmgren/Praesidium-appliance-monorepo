"""
modules/ediscovery/services/access_log.py

Append-only document access logging (Deal Center §1.10 / §4).

`log_document_access` is fire-and-forget: it opens its own session, inserts one
row into document_access_log, and NEVER raises — a logging failure must never
break document serving. Called from the byte-serving chokepoint so it captures
the deal-review viewer, the litigation review viewer, and (Unit D) guest reads.
"""
import logging

from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


def actor_from_request(request):
    """Pull (user_id, email, is_guest) off request.state.current_user, safely."""
    u = getattr(getattr(request, "state", None), "current_user", None)
    if u is None:
        return None, None, False
    uid = getattr(u, "id", None)
    email = getattr(u, "email", None) or getattr(u, "canonical_email", None)
    is_guest = getattr(u, "auth_provider", "") == "magic_link"
    return uid, email, is_guest


def context_from_referer(referer: str) -> str:
    """Classify the access surface from the Referer URL."""
    r = (referer or "").lower()
    if "/review/" in r and "/matters/" in r:
        return "deal_review"
    if "/deal-room" in r:
        return "deal_room"
    if "/portal" in r:
        return "portal"
    if "/ediscovery" in r:
        return "ediscovery_review"
    return "document_file"


async def log_document_access(
    *,
    tenant_id,
    ediscovery_document_id=None,
    matter_id=None,
    collection_id=None,
    document_name=None,
    deal_room_document_id=None,
    dms_document_id=None,
    actor_user_id=None,
    actor_email=None,
    actor_is_guest=False,
    action="view",
    context=None,
    ip=None,
    user_agent=None,
):
    """Insert one access-log row. Swallows all errors.

    Built dynamically so uuid columns are only present (as CAST(:x AS uuid))
    when a value exists — avoids asyncpg's AmbiguousParameterError on untyped
    NULL params and the house rule against binding None into a CAST."""
    cols = ["tenant_id", "document_name", "actor_user_id", "actor_email",
            "actor_is_guest", "action", "context", "ip", "user_agent"]
    vals = ["trim(:tenant_id)", ":doc_name", ":actor_user_id", ":actor_email",
            ":is_guest", ":action", ":context", ":ip", ":user_agent"]
    params = {
        "tenant_id": tenant_id,
        "doc_name": document_name,
        "actor_user_id": actor_user_id,
        "actor_email": actor_email,
        "is_guest": bool(actor_is_guest),
        "action": (action or "view")[:16],
        "context": (context[:40] if context else None),
        "ip": (str(ip)[:64] if ip else None),
        "user_agent": user_agent,
    }
    # uuid columns — include only when present
    for col, val, key in (
        ("matter_id", matter_id, "matter_id"),
        ("ediscovery_document_id", ediscovery_document_id, "edoc_id"),
        ("deal_room_document_id", deal_room_document_id, "drd_id"),
        ("dms_document_id", dms_document_id, "dms_id"),
        ("collection_id", collection_id, "col_id"),
    ):
        if val:
            cols.append(col)
            vals.append(f"CAST(:{key} AS uuid)")
            params[key] = str(val)
    sql = (f"INSERT INTO document_access_log ({', '.join(cols)}) "
           f"VALUES ({', '.join(vals)})")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(sql), params)
            await session.commit()
    except Exception as exc:  # never break serving on a logging failure
        logger.warning("log_document_access failed: %s", exc)
