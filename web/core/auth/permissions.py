"""
core/auth/permissions.py
Phase 1 permission service.

CONTRACT (Pattern A):
    result = await PermissionService.can(user, module, action)
    -> PermissionResult:
         allowed: bool
         scope_filters: dict[str, list | None]   # None = unrestricted

USAGE:

    # Per-resource check (route handler):
    perm = await PermissionService.can(user, "bill_run", "create")
    if not perm.allowed:
        raise HTTPException(403)
    if perm.scope_filters.get("matter_id") is not None:
        if matter_id not in perm.scope_filters["matter_id"]:
            raise HTTPException(403)

    # List-render filter (dashboard / surface):
    perm = await PermissionService.can(user, "widgets", "view")
    slugs = perm.scope_filters.get("widget_slug")  # None = all
    sql = "SELECT * FROM widget_registry WHERE category = :surface"
    if slugs is not None:
        sql += " AND widget_slug = ANY(:slugs)"

DECISIONS LOCKED 2026-04-30:
  - Default-allow for staff/attorney roles when no matrix row exists
  - Default-deny for client/deal_room_guest roles when no matrix row exists
  - Firm admin can override anything for their tenant (locked_by_admin ignored
    in Phase 1; column kept in schema for future platform-level invariants)
  - super_admin role bypasses everything (break-glass)

  - scope_filters semantics:
        None     = no restriction on this filter dimension
        []       = empty allow-list — caller must NOT execute "WHERE x IN ()"
                   (Postgres rejects empty IN lists). Treat as "deny all".
        [a, b]   = restrict to these IDs

  - Matter scope is computed from matter_timekeepers UNION matter_access_grants
    where is_active=True and (expires_at IS NULL OR expires_at > NOW()).

  - External users (client, deal_room_guest) get scope from external_user_scopes.
    For client role: scope_filters['matter_id'] = matters where matter.client_id
    is in the user's allowed client scopes.
    For deal_room_guest: scope_filters['deal_room_id'] = explicit grants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants — role classes drive default-allow vs default-deny behavior.
# ─────────────────────────────────────────────────────────────────────────────

# Roles that get "default allow" when no matrix row exists.
INTERNAL_ROLES = frozenset({
    "super_admin",
    "admin",
    "partner",
    "attorney",
    "paralegal",
    "staff",
    "read_only",
})

# Roles that get "default deny" when no matrix row exists.
EXTERNAL_ROLES = frozenset({
    "client",
    "deal_room_guest",
})

# super_admin bypasses every check (break-glass).
ROOT_ROLES = frozenset({"super_admin"})


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PermissionResult:
    """
    Outcome of a permission check.

    allowed:
        True  → caller may proceed (subject to scope_filters)
        False → caller must refuse (403)

    scope_filters:
        dict mapping filter dimension → list of allowed IDs.
        Missing key OR value=None means "no restriction on this dimension".
        Value=[] means "empty allow-list" — caller must treat as deny-all
        for that dimension and NEVER emit `WHERE x IN ()` to the database.

    reason:
        Human-readable explanation, used for logs and the 403 message.
        Never expose internal detail to end users.
    """
    allowed: bool
    scope_filters: dict[str, Optional[list]] = field(default_factory=dict)
    reason: str = ""

    def filter_for(self, dimension: str) -> Optional[list]:
        """
        Return the allowed-id list for `dimension`, or None if unrestricted.
        Caller must check for empty list separately and treat as deny-all.
        """
        return self.scope_filters.get(dimension)


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class PermissionService:
    """
    Stateless service. All methods are classmethods; the service holds no
    state of its own. DB access goes through AsyncSessionLocal directly per
    the architectural constant (never get_session_factory).
    """

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    async def can(
        cls,
        user,
        module: str,
        action: str,
    ) -> PermissionResult:
        """
        Decide whether `user` may perform `action` on `module`, and return
        the scope filters that narrow which resources within that module
        the user can touch.

        `user` must have attributes: id, tenant_id, role, is_active.
        `user` may be None — treated as deny.

        See module docstring for the contract and semantics.
        """
        # ── Sanity gates ──────────────────────────────────────────────────
        if user is None:
            return PermissionResult(False, reason="no user")
        if not getattr(user, "is_active", False):
            return PermissionResult(False, reason="user inactive")

        role = (getattr(user, "role", None) or "").strip()
        tenant_id = (getattr(user, "tenant_id", None) or "").strip()
        if not role or not tenant_id:
            return PermissionResult(False, reason="user missing role or tenant")

        # ── Root bypass ───────────────────────────────────────────────────
        if role in ROOT_ROLES:
            return PermissionResult(True, scope_filters={}, reason="super_admin bypass")

        # ── Matrix lookup ─────────────────────────────────────────────────
        matrix_row = await cls._lookup_matrix(tenant_id, role, module, action)

        if matrix_row is None:
            # No matrix row — apply role-class default
            if role in INTERNAL_ROLES:
                allowed = True
                reason = f"default-allow (internal role={role}, no matrix row)"
            elif role in EXTERNAL_ROLES:
                allowed = False
                reason = f"default-deny (external role={role}, no matrix row)"
            else:
                # Unknown role — deny defensively
                allowed = False
                reason = f"unknown role={role!r}, denying"
        else:
            allowed = bool(matrix_row["allowed"])
            reason = f"matrix row (allowed={allowed})"

        if not allowed:
            return PermissionResult(False, reason=reason)

        # ── Scope filter assembly ─────────────────────────────────────────
        scope_filters: dict[str, Optional[list]] = {}

        # Admin gets unrestricted scope across the tenant.
        if role == "admin":
            return PermissionResult(True, scope_filters={}, reason=f"admin in tenant: {reason}")

        # Internal roles below admin: scope by matter assignment for matter-scoped
        # modules. The "matter-scoped" modules are: matters, dms, billing, bill_run,
        # time_entries, ediscovery, drafting, intelligence. The widgets module is
        # NOT in this list — widget visibility is governed by per-slug scope which
        # we leave as None (all visible) at this layer; the caller's per-widget
        # data_source enforces matter scope inside its own query.
        if module in MATTER_SCOPED_MODULES and role in INTERNAL_ROLES:
            matter_ids = await cls._user_matter_ids(tenant_id, user.id)
            scope_filters["matter_id"] = matter_ids

        # External roles: scope from external_user_scopes.
        if role == "client":
            client_ids = await cls._user_client_scopes(tenant_id, user.id)
            scope_filters["client_id"] = client_ids
            # Client portal users implicitly see only matters under their clients.
            if module in MATTER_SCOPED_MODULES:
                matter_ids = await cls._matter_ids_for_clients(tenant_id, client_ids)
                scope_filters["matter_id"] = matter_ids

        elif role == "deal_room_guest":
            deal_room_ids = await cls._user_deal_room_scopes(tenant_id, user.id)
            scope_filters["deal_room_id"] = deal_room_ids

        return PermissionResult(True, scope_filters=scope_filters, reason=reason)

    # ── Internal helpers ──────────────────────────────────────────────────

    @classmethod
    async def _lookup_matrix(
        cls,
        tenant_id: str,
        role: str,
        module: str,
        action: str,
    ) -> Optional[dict]:
        """
        Single-row lookup against permission_matrix.
        Returns dict (allowed, locked_by_admin) or None if no row matches.
        """
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT allowed, locked_by_admin
                    FROM permission_matrix
                    WHERE TRIM(tenant_id) = :tid
                      AND role = :role
                      AND module = :module
                      AND action = :action
                    LIMIT 1
                """),
                {"tid": tenant_id, "role": role, "module": module, "action": action},
            )
            row = r.mappings().first()
            return dict(row) if row else None

    @classmethod
    async def _user_matter_ids(cls, tenant_id: str, user_id: int) -> list[str]:
        """
        Return the list of matter UUIDs (as str) the user has access to.
        Source: matter_timekeepers UNION matter_access_grants (active + unexpired).
        Empty list = user has no matter access.
        """
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT DISTINCT matter_id::text AS matter_id FROM (
                        SELECT matter_id
                          FROM matter_timekeepers
                         WHERE TRIM(tenant_id) = :tid
                           AND user_id = :uid
                        UNION ALL
                        SELECT matter_id
                          FROM matter_access_grants
                         WHERE TRIM(tenant_id) = :tid
                           AND user_id = :uid
                           AND is_active = TRUE
                           AND (expires_at IS NULL OR expires_at > NOW())
                    ) AS combined
                """),
                {"tid": tenant_id, "uid": user_id},
            )
            return [row[0] for row in r.fetchall()]

    @classmethod
    async def _user_client_scopes(cls, tenant_id: str, user_id: int) -> list[str]:
        """
        Return list of client UUIDs (as str) granted to a client-portal user
        via external_user_scopes(scope_type='client').
        """
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT scope_id
                      FROM external_user_scopes
                     WHERE TRIM(tenant_id) = :tid
                       AND user_id = :uid
                       AND scope_type = 'client'
                       AND is_active = TRUE
                       AND (expires_at IS NULL OR expires_at > NOW())
                """),
                {"tid": tenant_id, "uid": user_id},
            )
            return [row[0] for row in r.fetchall()]

    @classmethod
    async def _user_deal_room_scopes(cls, tenant_id: str, user_id: int) -> list[str]:
        """
        Return list of deal_room UUIDs (as str) granted to a deal_room_guest
        via external_user_scopes(scope_type='deal_room').
        """
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT scope_id
                      FROM external_user_scopes
                     WHERE TRIM(tenant_id) = :tid
                       AND user_id = :uid
                       AND scope_type = 'deal_room'
                       AND is_active = TRUE
                       AND (expires_at IS NULL OR expires_at > NOW())
                """),
                {"tid": tenant_id, "uid": user_id},
            )
            return [row[0] for row in r.fetchall()]

    @classmethod
    async def _matter_ids_for_clients(
        cls,
        tenant_id: str,
        client_ids: list[str],
    ) -> list[str]:
        """
        Resolve a client-portal user's allowed matter IDs by joining
        their granted client scopes to the matters table.
        Returns empty list if client_ids is empty (avoids empty IN clause).
        """
        if not client_ids:
            return []
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT id::text AS matter_id
                      FROM matters
                     WHERE TRIM(tenant_id) = :tid
                       AND client_id::text = ANY(:cids)
                """),
                {"tid": tenant_id, "cids": client_ids},
            )
            return [row[0] for row in r.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Module-level configuration
# ─────────────────────────────────────────────────────────────────────────────

# Modules whose resources are scoped per-matter for non-admin internal roles.
# Listed here to avoid scattering this knowledge across the service. Add to
# this set when introducing new matter-scoped modules.
MATTER_SCOPED_MODULES = frozenset({
    "matters",
    "dms",
    "billing",
    "bill_run",
    "time_entries",
    "ediscovery",
    "drafting",
    "intelligence",
})
