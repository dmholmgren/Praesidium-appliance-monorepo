"""
M-DESK C1 — Checkout state service.

# PATENT-S4-CANDIDATE:
# This is the locus of the Series 4 candidate claim:
# "deterministic merge of native and web edits via server-arbitrated
# checkout-with-TTL." The TTL is the novelty — desktop checkout systems
# historically either (a) hold locks indefinitely (Logikcull, iManage)
# requiring admin unlock, or (b) hold no lock at all (web SaaS DMSes)
# allowing silent last-write-wins overwrite. The TTL split lets the
# server arbitrate without trusting the client's lifecycle.

Checkout state lives in `documents.metadata` JSONB. No new schema. The
five keys written under metadata when a document is checked out:

  checked_out_by        users.id stringified
  checked_out_by_email  cached on the lock for fast UI display
  checked_out_at        ISO-8601 UTC timestamp
  checked_out_client    'desktop-vsto-1.0.0' (or future variants)
  checkout_lock_ttl     integer seconds — usually DESKTOP_CHECKOUT_TTL_SECONDS

When a checkout is released, all five keys are removed. Other metadata
keys are untouched.

Concurrency model:
  attempt_checkout uses a single conditional UPDATE...RETURNING. The WHERE
  clause atomically verifies "no live checkout, OR expired lock, OR
  same user re-claiming" in the database. There is no read-modify-write
  window; if two clients race, exactly one row is updated.

Concurrency model is NOT replication-safe across multiple Postgres
primaries (we only run one). It IS replica-safe — read replicas don't
serve writes.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

# The five checkout-state keys. Any change here must be coordinated with
# the VSTO client and with `release_checkout`.
CHECKOUT_KEYS = (
    "checked_out_by",
    "checked_out_by_email",
    "checked_out_at",
    "checked_out_client",
    "checkout_lock_ttl",
)

# Same role rank table as widget_routes.py. Mirrored here so we don't
# import from a sibling module that has its own auth side effects. Keep
# in sync if the canonical table ever changes.
_ROLE_RANK: dict[str, int] = {
    "read_only":   0,
    "staff":       1,
    "paralegal":   1,
    "attorney":    2,
    "partner":     3,
    "admin":       4,
    "super_admin": 5,
}

ADMIN_RANK = 4  # admin and above can force-release any checkout


def _checkout_ttl_seconds() -> int:
    return int(os.environ.get("DESKTOP_CHECKOUT_TTL_SECONDS", str(8 * 3600)))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════════════════
# Data shapes
# ═════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CheckoutState:
    """Read-only view of a document's checkout state, if any."""
    doc_id: str
    tenant_id: str
    checked_out_by: str
    checked_out_by_email: str
    checked_out_at: datetime
    checked_out_client: str
    checkout_lock_ttl: int

    @property
    def expires_at(self) -> datetime:
        return self.checked_out_at + timedelta(seconds=self.checkout_lock_ttl)

    @property
    def is_expired(self) -> bool:
        return _now_utc() >= self.expires_at

    def held_by_user(self, user_id: int) -> bool:
        return self.checked_out_by == str(user_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id":               self.doc_id,
            "checked_out_by":       self.checked_out_by,
            "checked_out_by_email": self.checked_out_by_email,
            "checked_out_at":       self.checked_out_at.isoformat(),
            "checked_out_client":   self.checked_out_client,
            "checkout_lock_ttl":    self.checkout_lock_ttl,
            "expires_at":           self.expires_at.isoformat(),
            "is_expired":           self.is_expired,
        }


# ═════════════════════════════════════════════════════════════════════════
# Exceptions
# ═════════════════════════════════════════════════════════════════════════

class CheckoutError(Exception):
    """Base class for checkout state errors."""
    http_status = 500


class DocumentNotFound(CheckoutError):
    """Document does not exist in the tenant."""
    http_status = 404


class CheckoutConflict(CheckoutError):
    """Document is checked out by someone else and the lock is still fresh.

    .state holds the active CheckoutState for inclusion in the 409 response.
    """
    http_status = 409

    def __init__(self, state: CheckoutState):
        super().__init__(f"document held by {state.checked_out_by_email}")
        self.state = state


class NotPermitted(CheckoutError):
    """Caller is neither the holder nor an admin."""
    http_status = 403


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

def _parse_iso(value: Any) -> datetime:
    """JSONB stores timestamps as strings — parse back to aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise ValueError(f"invalid checked_out_at: {value!r}")
    # fromisoformat handles "+00:00" but not "Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _state_from_metadata(
    doc_id: str, tenant_id: str, metadata: dict[str, Any]
) -> Optional[CheckoutState]:
    """Build a CheckoutState if metadata has the five checkout keys, else None."""
    if not metadata or not metadata.get("checked_out_by"):
        return None
    try:
        return CheckoutState(
            doc_id=doc_id,
            tenant_id=tenant_id,
            checked_out_by=str(metadata["checked_out_by"]),
            checked_out_by_email=str(metadata.get("checked_out_by_email") or ""),
            checked_out_at=_parse_iso(metadata["checked_out_at"]),
            checked_out_client=str(metadata.get("checked_out_client") or ""),
            checkout_lock_ttl=int(metadata.get("checkout_lock_ttl") or _checkout_ttl_seconds()),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "[m-desk] checkout metadata for doc=%s malformed: %s; treating as no lock",
            doc_id, exc,
        )
        return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


# ═════════════════════════════════════════════════════════════════════════
# Read-only operations
# ═════════════════════════════════════════════════════════════════════════

async def read_checkout(
    *, doc_id: str, tenant_id: str
) -> Optional[CheckoutState]:
    """Return the document's current checkout state, or None if not held.

    Raises DocumentNotFound if the doc_id doesn't exist in the tenant.
    """
    if not _is_uuid(doc_id):
        raise DocumentNotFound("invalid document id")

    tid = (tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT id::text AS id, metadata
                FROM documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": tid},
        )
        row = result.mappings().first()
        if not row:
            raise DocumentNotFound(f"document {doc_id} not found")
        return _state_from_metadata(row["id"], tid, row["metadata"] or {})


# ═════════════════════════════════════════════════════════════════════════
# Checkout — atomic conditional UPDATE
# ═════════════════════════════════════════════════════════════════════════

async def attempt_checkout(
    *,
    doc_id: str,
    tenant_id: str,
    user_id: int,
    user_email: str,
    client: str,
    ttl_seconds: Optional[int] = None,
) -> CheckoutState:
    """Atomically claim or re-claim a checkout on a document.

    Behavior:
      * No active lock                          -> claim, return new state.
      * Active lock held by same user           -> idempotent re-claim,
                                                   timestamp refreshed.
      * Active lock held by other, expired      -> override, return new state,
                                                   audit-log the override.
      * Active lock held by other, fresh        -> raise CheckoutConflict.
      * Document missing                        -> raise DocumentNotFound.

    Atomicity is enforced by the UPDATE...WHERE...RETURNING below. The
    WHERE clause encodes all four "permitted" cases; if no row is updated,
    we re-read and raise the appropriate exception.
    """
    if not _is_uuid(doc_id):
        raise DocumentNotFound("invalid document id")

    tid = (tenant_id or "").strip()
    user_str = str(user_id)
    ttl = int(ttl_seconds) if ttl_seconds is not None else _checkout_ttl_seconds()
    now = _now_utc()

    # The new metadata block we want to install. Built as a Python dict
    # then JSON-encoded; passed as a parameter and CAST to jsonb. We do
    # NOT use jsonb_set five times — the merge `metadata || CAST(:patch AS jsonb)`
    # is one operation and preserves all unrelated keys.
    patch = {
        "checked_out_by":       user_str,
        "checked_out_by_email": user_email,
        "checked_out_at":       now.isoformat(),
        "checked_out_client":   client,
        "checkout_lock_ttl":    ttl,
    }
    patch_json = json.dumps(patch)

    # The TTL cutoff: if checked_out_at <= cutoff, the lock is expired.
    # We compute it in Python and pass as a param rather than relying on
    # NOW() inside the SQL, so that "now" is consistent across the SELECT
    # and the UPDATE.
    expired_cutoff = now - timedelta(seconds=ttl)

    async with AsyncSessionLocal() as session:
        # First read the document so we can audit-log the override case.
        select_result = await session.execute(
            sa_text("""
                SELECT id::text AS id, metadata
                FROM documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": tid},
        )
        existing = select_result.mappings().first()
        if not existing:
            raise DocumentNotFound(f"document {doc_id} not found")

        prior_state = _state_from_metadata(
            existing["id"], tid, existing["metadata"] or {}
        )

        # Atomic claim. The WHERE clause is the entire concurrency control:
        update_result = await session.execute(
            sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                               || CAST(:patch AS jsonb),
                    updated_at = :now
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND (
                    metadata IS NULL
                    OR metadata->>'checked_out_by' IS NULL
                    OR (metadata->>'checked_out_by') = :user_str
                    OR (metadata->>'checked_out_at')::timestamptz <= :cutoff
                  )
                RETURNING id::text AS id
            """),
            {
                "patch":    patch_json,
                "now":      now,
                "doc":      doc_id,
                "tid":      tid,
                "user_str": user_str,
                "cutoff":   expired_cutoff,
            },
        )
        updated = update_result.mappings().first()

        if not updated:
            # Lost the race — re-read for the conflict response.
            await session.rollback()
            current_state = prior_state  # last value we saw is the conflict
            if current_state is None:
                # Race-of-the-race: between SELECT and UPDATE someone else
                # released the lock and someone else claimed it. Re-read.
                re_read = await session.execute(
                    sa_text("""
                        SELECT id::text AS id, metadata
                        FROM documents
                        WHERE id = CAST(:doc AS uuid)
                          AND TRIM(tenant_id) = :tid
                        LIMIT 1
                    """),
                    {"doc": doc_id, "tid": tid},
                )
                fresh = re_read.mappings().first()
                if fresh:
                    current_state = _state_from_metadata(
                        fresh["id"], tid, fresh["metadata"] or {}
                    )
            if current_state is None:
                # Still no state — document was deleted out from under us.
                raise DocumentNotFound(f"document {doc_id} disappeared")
            raise CheckoutConflict(current_state)

        await session.commit()

        # Audit-log the override case if applicable. Done after commit so a
        # logging failure cannot roll back a successful checkout.
        if prior_state and not prior_state.held_by_user(user_id):
            logger.info(
                "[m-desk] checkout override: doc=%s tenant=%s "
                "prior_holder=%s acquired_by=%s prior_age_s=%.0f",
                doc_id, tid,
                prior_state.checked_out_by, user_str,
                (now - prior_state.checked_out_at).total_seconds(),
            )

    return CheckoutState(
        doc_id=doc_id,
        tenant_id=tid,
        checked_out_by=user_str,
        checked_out_by_email=user_email,
        checked_out_at=now,
        checked_out_client=client,
        checkout_lock_ttl=ttl,
    )


# ═════════════════════════════════════════════════════════════════════════
# Heartbeat — extend a checkout the holder still owns
# ═════════════════════════════════════════════════════════════════════════

async def extend_checkout(
    *, doc_id: str, tenant_id: str, user_id: int
) -> Optional[CheckoutState]:
    """Bump checked_out_at to NOW for a checkout held by user_id.

    Used by the tray heartbeat. Returns the new state if the bump
    succeeded, or None if the user no longer holds the lock (e.g. it
    expired and someone else claimed it).

    Does NOT raise — heartbeats run on a background timer and should
    not produce noisy errors. Loss of lock is reported via None and the
    client's tray UI should react.
    """
    if not _is_uuid(doc_id):
        return None

    tid = (tenant_id or "").strip()
    user_str = str(user_id)
    now = _now_utc()

    patch = {"checked_out_at": now.isoformat()}
    patch_json = json.dumps(patch)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                               || CAST(:patch AS jsonb),
                    updated_at = :now
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND metadata->>'checked_out_by' = :user_str
                RETURNING id::text AS id, metadata
            """),
            {
                "patch":    patch_json,
                "now":      now,
                "doc":      doc_id,
                "tid":      tid,
                "user_str": user_str,
            },
        )
        row = result.mappings().first()
        if not row:
            await session.rollback()
            return None
        await session.commit()
        return _state_from_metadata(row["id"], tid, row["metadata"] or {})


# ═════════════════════════════════════════════════════════════════════════
# Release — clear all five checkout keys
# ═════════════════════════════════════════════════════════════════════════

async def release_checkout(
    *,
    doc_id: str,
    tenant_id: str,
    requester_user_id: int,
    requester_role: str,
) -> CheckoutState:
    """Clear the checkout state on a document.

    Permission:
      * Holder may always release their own lock.
      * Anyone with role rank >= ADMIN_RANK (admin, super_admin) may
        release any lock.

    Returns the state that was cleared (for audit-logging in the route
    layer). Raises:

      * DocumentNotFound — bad doc_id or wrong tenant
      * CheckoutError    — document not currently checked out
      * NotPermitted     — neither holder nor admin
    """
    if not _is_uuid(doc_id):
        raise DocumentNotFound("invalid document id")

    tid = (tenant_id or "").strip()
    requester_role = (requester_role or "staff").strip().lower()
    is_admin = _ROLE_RANK.get(requester_role, 0) >= ADMIN_RANK

    state = await read_checkout(doc_id=doc_id, tenant_id=tid)
    if state is None:
        raise CheckoutError("document is not checked out")

    if not state.held_by_user(requester_user_id) and not is_admin:
        raise NotPermitted(
            f"user {requester_user_id} is neither holder nor admin"
        )

    # Strip the five keys with #- (jsonb operator). We list them as a
    # text[] and let `metadata - :keys` remove all in one expression.
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                               - CAST(:keys AS text[]),
                    updated_at = :now
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {
                "keys": list(CHECKOUT_KEYS),
                "now":  _now_utc(),
                "doc":  doc_id,
                "tid":  tid,
            },
        )
        await session.commit()

    logger.info(
        "[m-desk] checkout released: doc=%s tenant=%s released_by=%s "
        "was_held_by=%s admin_force=%s",
        doc_id, tid, requester_user_id, state.checked_out_by,
        is_admin and not state.held_by_user(requester_user_id),
    )
    return state


# ═════════════════════════════════════════════════════════════════════════
# Bulk extension — used by /heartbeat with a list of doc_ids
# ═════════════════════════════════════════════════════════════════════════

async def bulk_extend_checkouts(
    *, doc_ids: list[str], tenant_id: str, user_id: int
) -> dict[str, bool]:
    """Extend all listed checkouts the user still holds.

    Returns a dict {doc_id: True if extended, False otherwise} matching
    the input order semantics. Doc IDs the user no longer holds (lock
    expired and reclaimed by another, or document deleted) come back as
    False — the tray UI uses that to clear stale local state.
    """
    if not doc_ids:
        return {}

    # Filter to syntactically valid UUIDs only; bad ones come back False.
    valid: list[str] = [d for d in doc_ids if _is_uuid(d)]
    invalid = {d: False for d in doc_ids if not _is_uuid(d)}

    out: dict[str, bool] = dict(invalid)
    for doc_id in valid:
        state = await extend_checkout(
            doc_id=doc_id, tenant_id=tenant_id, user_id=user_id
        )
        out[doc_id] = state is not None
    return out
