"""Platform Admin — MCP credential issuance & revocation.

Page + JSON API for issuing and revoking the multi-token mcp_credentials
managed by core.services.mcp_token_service. Gated by the admin-console
session (require_admin_session), consistent with the rest of /admin/*.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text as sa_text

from modules.admin.admin_panel import require_admin_session, templates
from core.db.base import AsyncSessionLocal
from core.services import mcp_token_service as mcp_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

HJMM_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
SCOPE_LABELS = {
    "mcp:user": "User \u2014 practice tools (8890)",
    "mcp:admin": "Admin \u2014 full appliance / exec (8891)",
}


@router.get("/mcp-credentials", response_class=HTMLResponse)
async def mcp_credentials_page(request: Request, _: str = Depends(require_admin_session)):
    return templates.TemplateResponse(
        "mcp_credentials.html",
        {"request": request, "page": "mcp_credentials", "scopes": SCOPE_LABELS},
    )


@router.get("/api/mcp-users")
async def mcp_users(_: str = Depends(require_admin_session), tenant: str = HJMM_TENANT):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text("""
            SELECT id, full_name, email, role::text AS role
            FROM users
            WHERE TRIM(tenant_id) = :t AND is_active = true
            ORDER BY (role::text IN ('super_admin','admin')) DESC, full_name
        """), {"t": tenant.strip()})).mappings().all()
    return {"users": [dict(r) for r in rows]}


@router.get("/api/mcp-credentials")
async def mcp_list(_: str = Depends(require_admin_session), tenant: str = HJMM_TENANT):
    return {"credentials": await mcp_svc.list_credentials(tenant_id=tenant)}


class IssueReq(BaseModel):
    user_id: int
    scope: str
    label: str
    ttl_days: Optional[int] = None
    tenant_id: str = HJMM_TENANT


@router.post("/api/mcp-credentials")
async def mcp_issue(body: IssueReq, _: str = Depends(require_admin_session)):
    if body.scope not in mcp_svc.SCOPES:
        raise HTTPException(status_code=400, detail="invalid scope")
    async with AsyncSessionLocal() as s:
        u = (await s.execute(sa_text("""
            SELECT id, email, role::text AS role
            FROM users WHERE id = :uid AND TRIM(tenant_id) = :t
        """), {"uid": body.user_id, "t": body.tenant_id.strip()})).mappings().first()
    if not u:
        raise HTTPException(status_code=404, detail="user not found in tenant")
    return await mcp_svc.issue_credential(
        user_id=u["id"], tenant_id=body.tenant_id, email=u["email"] or "",
        role=u["role"], scope=body.scope, label=body.label,
        created_by=u["id"], ttl_days=body.ttl_days,
    )


@router.post("/api/mcp-credentials/{cred_id}/revoke")
async def mcp_revoke(cred_id: str, _: str = Depends(require_admin_session),
                     tenant: str = HJMM_TENANT):
    ok = await mcp_svc.revoke_credential(
        cred_id=cred_id, tenant_id=tenant, reason="admin-console")
    if not ok:
        raise HTTPException(status_code=404, detail="not found or already revoked")
    return {"revoked": True}
