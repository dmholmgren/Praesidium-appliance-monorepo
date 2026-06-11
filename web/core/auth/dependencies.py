"""
core/auth/dependencies.py

FastAPI dependencies that gate routes by permission.

USAGE:

    from core.auth.dependencies import require_permission

    @router.get("/billing/invoices")
    async def list_invoices(
        request: Request,
        perm: PermissionResult = Depends(require_permission("billing", "view")),
    ):
        # perm.allowed is guaranteed True (or 403 was raised)
        # perm.scope_filters narrows the query
        matter_ids = perm.scope_filters.get("matter_id")  # None = all
        # ... build query with optional WHERE matter_id = ANY(:matter_ids)

The dependency NEVER passes silently. If the user lacks permission, it
raises HTTPException(403). If the user is unauthenticated (request.state
.current_user is None), it raises HTTPException(401) — which the auth
middleware would normally catch first, but we double-gate defensively.

The empty-list edge case (scope_filters[dim] == []) is the caller's
problem — the dependency does not refuse on this. A user who has no
matters legitimately gets allowed=True with matter_id=[]; the route
just returns an empty list to the client.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from core.auth.permissions import PermissionService, PermissionResult


def require_permission(module: str, action: str) -> Callable:
    """
    Build a FastAPI dependency that enforces `module.action` on the request's
    current_user. Returns the PermissionResult so the route handler can use
    scope_filters to narrow its queries.

    Args:
        module: e.g. "billing", "dms", "matters", "widgets"
        action: e.g. "view", "create", "edit", "finalize"

    Returns:
        async dependency that yields PermissionResult.
    """
    async def _dep(request: Request) -> PermissionResult:
        user = getattr(request.state, "current_user", None)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        perm = await PermissionService.can(user, module, action)
        if not perm.allowed:
            # Log internally — do NOT leak `perm.reason` to the client.
            import logging
            logging.getLogger(__name__).info(
                "permission denied: user_id=%s module=%s action=%s reason=%s",
                getattr(user, "id", "?"), module, action, perm.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return perm

    return _dep


def require_any_permission(*pairs: tuple[str, str]) -> Callable:
    """
    Allow access if ANY of the (module, action) pairs is permitted. The
    returned PermissionResult is the FIRST matching pair's result (so its
    scope_filters apply). Useful for routes that have multiple legitimate
    callers — e.g. a billing summary that's reachable by both
    `billing.view` and `matter_finance.view`.
    """
    async def _dep(request: Request) -> PermissionResult:
        user = getattr(request.state, "current_user", None)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        last_reason = ""
        for module, action in pairs:
            perm = await PermissionService.can(user, module, action)
            if perm.allowed:
                return perm
            last_reason = perm.reason
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    return _dep
