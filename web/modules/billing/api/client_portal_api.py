"""
Client Portal Provisioning API — v4 (adds contacts lookup).
Appended to existing client_portal_api.py endpoints.
"""
# This file REPLACES client_portal_api.py — includes all v3 endpoints plus contacts.
from __future__ import annotations
import logging, secrets, hashlib, json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing/client-portal", tags=["client-portal"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r): return getattr(getattr(r.state, "current_user", None), "id", None)

STANDARD_FOLDERS = {
    "litigation": ["01-Pleadings","02-Discovery","03-Correspondence","04-Research","05-Court Filings","06-Depositions","07-Experts","08-Mediation","09-Orders","10-Working Docs","11-eDiscovery","12-Billing","13-Closing","14-Trial Preparation","15-Email"],
    "transactional_loan": ["01-Loan Documents","02-Title","03-Appraisal","04-Borrower Documents","05-Property","06-Insurance","07-Environmental","08-Closing","09-Post-Closing","10-Correspondence","11-Billing","12-Email"],
    "default": ["01-Client Documents","02-Pleadings","03-Discovery","04-Correspondence","05-Research","06-Court Filings","07-Experts","08-Working Docs","09-Billing","10-Email"],
}
DEFAULT_EXCLUDED = {"10-Working Docs","04-Research","11-eDiscovery","08-Working Docs","14-Trial Preparation","15-Email"}
DEFAULT_SCOPE_CONFIG = {"exclude_email":True,"exclude_work_product":True,"exclude_billing_internals":True,"exclude_attorney_notes":True}
DEFAULT_MODULES = [
    {"key":"dms","label":"Documents","default":True},{"key":"billing_view","label":"Invoices","default":True},
    {"key":"calendar","label":"Calendar","default":True},{"key":"tasks","label":"Tasks","default":True},
    {"key":"messaging","label":"Messages","default":True},{"key":"intake_forms","label":"Intake Forms","default":False},
]

async def _portal(tid, db):
    r = await db.execute(sa_text("SELECT TRIM(id) AS id, domain FROM tenants WHERE TRIM(parent_tenant_id) = :ptid AND sub_tenant_type = 'client_portal' AND is_active = true LIMIT 1"), {"ptid": tid})
    return r.mappings().fetchone()


@router.get("/{client_id}")
async def portal_status(request: Request, client_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"has_portal":False,"error":"No client portal configured"})
        ptid = portal["id"]

        # Users
        pu = await db.execute(sa_text("SELECT id, username, email, full_name, role, is_active, created_at, last_login FROM users WHERE TRIM(tenant_id) = :ptid AND portal_access_client_id = CAST(:cid AS uuid) ORDER BY id"), {"ptid":ptid,"cid":client_id})
        users = []
        for r in pu.mappings():
            ma = await db.execute(sa_text("SELECT module_key, is_enabled FROM portal_module_access WHERE user_id = :uid"), {"uid":r["id"]})
            mod_map = {m["module_key"]:m["is_enabled"] for m in ma.mappings()}
            modules = [{**m,"enabled":mod_map.get(m["key"],m["default"])} for m in DEFAULT_MODULES]
            ml = await db.execute(sa_text("SELECT token, expires_at FROM portal_magic_links WHERE user_id = :uid AND used_at IS NULL AND expires_at > NOW() ORDER BY created_at DESC LIMIT 1"), {"uid":r["id"]})
            ml_row = ml.mappings().fetchone()
            la = await db.execute(sa_text("SELECT created_at, ip_address, action FROM user_activity_log WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"), {"uid":r["id"]})
            la_row = la.mappings().fetchone()
            mlh = await db.execute(sa_text("SELECT used_at, used_ip, created_at FROM portal_magic_links WHERE user_id = :uid AND used_at IS NOT NULL ORDER BY used_at DESC LIMIT 5"), {"uid":r["id"]})
            ml_history = [{"used_at":str(m["used_at"])[:16],"ip":m["used_ip"],"sent_at":str(m["created_at"])[:16]} for m in mlh.mappings()]
            users.append({"id":r["id"],"email":r["email"],"full_name":r["full_name"],"role":r["role"],
                "is_active":r["is_active"],"created_at":str(r["created_at"])[:10] if r["created_at"] else "",
                "last_login":str(r["last_login"])[:10] if r["last_login"] else "Never","modules":modules,
                "pending_link":{"token":ml_row["token"][:8]+"...","expires":ml_row["expires_at"].isoformat()} if ml_row else None,
                "last_access":{"time":str(la_row["created_at"])[:16],"ip":la_row["ip_address"],"action":la_row["action"]} if la_row else None,
                "link_history":ml_history})

        # Scope
        scope = await db.execute(sa_text("SELECT s.id, s.matter_id::text, s.granted_at, s.revoked_at, m.matter_name, m.matter_number, m.status, m.practice_area FROM sub_tenant_matter_scope s JOIN matters m ON m.id = s.matter_id AND TRIM(m.tenant_id) = :tid WHERE TRIM(s.tenant_id) = :ptid AND TRIM(s.parent_tenant_id) = :tid ORDER BY s.revoked_at NULLS FIRST, m.matter_name"), {"ptid":ptid,"tid":tid})
        scoped = []
        for r in scope.mappings():
            fs = await db.execute(sa_text("SELECT folder_key, is_visible FROM portal_folder_scope WHERE scope_id = :sid"), {"sid":r["id"]})
            folder_map = {f["folder_key"]:f["is_visible"] for f in fs.mappings()}
            pa = (r["practice_area"] or "litigation").lower().replace(" ","_")
            ft = STANDARD_FOLDERS.get(pa, STANDARD_FOLDERS["default"])
            folders = [{"key":fk,"visible":folder_map.get(fk, fk not in DEFAULT_EXCLUDED),"standard":True} for fk in ft]
            for fk, vis in folder_map.items():
                if fk not in [f["key"] for f in folders]:
                    folders.append({"key":fk,"visible":vis,"standard":False})
            scoped.append({"scope_id":r["id"],"matter_id":r["matter_id"],"matter_name":r["matter_name"],"matter_number":r["matter_number"],"status":r["status"],"practice_area":r["practice_area"],"granted_at":str(r["granted_at"])[:10] if r["granted_at"] else "","revoked":r["revoked_at"] is not None,"folders":folders})

        all_m = await db.execute(sa_text("SELECT id::text, matter_name, matter_number, status FROM matters WHERE client_id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, matter_name"), {"cid":client_id,"tid":tid})

        # Contacts from matter_contacts for this client's matters
        contacts = []
        mc = await db.execute(sa_text("""
            SELECT DISTINCT c.id, c.full_name, c.email, c.company, c.contact_type, mc.role
            FROM matter_contacts mc
            JOIN contacts c ON c.id = mc.contact_id AND TRIM(c.tenant_id) = :tid
            JOIN matters m ON m.id = mc.matter_id AND TRIM(m.tenant_id) = :tid
            WHERE m.client_id = CAST(:cid AS uuid) AND c.email IS NOT NULL AND c.email != ''
            ORDER BY c.full_name
        """), {"tid":tid,"cid":client_id})
        for c in mc.mappings():
            contacts.append({"id":c["id"],"full_name":c["full_name"],"email":c["email"],"company":c["company"],"role":c["role"]})

        # Access log tail
        all_uids = [u["id"] for u in users]
        log_entries = []
        if all_uids:
            logs = await db.execute(sa_text("SELECT al.action, al.ip_address, al.created_at, u.full_name FROM user_activity_log al JOIN users u ON u.id = al.user_id WHERE al.user_id = ANY(:uids) ORDER BY al.created_at DESC LIMIT 10"), {"uids":all_uids})
            log_entries = [{"action":l["action"],"ip":l["ip_address"],"time":l["created_at"].isoformat() if l["created_at"] else "","name":l["full_name"]} for l in logs.mappings()]

        return JSONResponse({"has_portal":len(users)>0,"portal_domain":portal["domain"],"portal_tenant_id":ptid,
            "users":users,"scoped_matters":scoped,"all_matters":[dict(r) for r in all_m.mappings()],
            "contacts":contacts,"access_log_tail":log_entries})


@router.post("/{client_id}/provision")
async def provision_user(request: Request, client_id: str):
    tid=_tid(request); uid=_uid(request); body=await request.json()
    email=body.get("email","").strip(); full_name=body.get("full_name","").strip()
    role=body.get("role","client"); matter_ids=body.get("matter_ids",[])
    if not email: return JSONResponse({"error":"Email required"},status_code=400)
    if not full_name: return JSONResponse({"error":"Name required"},status_code=400)
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db)
        if not portal: return JSONResponse({"error":"No portal"},status_code=400)
        ptid=portal["id"]
        dup=await db.execute(sa_text("SELECT id FROM users WHERE TRIM(tenant_id)=:ptid AND email=:e AND portal_access_client_id=CAST(:cid AS uuid)"),{"ptid":ptid,"e":email,"cid":client_id})
        if dup.fetchone(): return JSONResponse({"error":"User already exists for this client"},status_code=409)
        username=email.split("@")[0].lower().replace(".","_")[:50]
        pw_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        await db.execute(sa_text("INSERT INTO users (tenant_id,username,email,full_name,role,password_hash,is_active,auth_provider,portal_access_client_id) VALUES (:ptid,:u,:e,:fn,CAST(:r AS user_role_enum),:pw,true,'magic_link',CAST(:cid AS uuid))"),{"ptid":ptid,"u":username,"e":email,"fn":full_name,"r":role,"pw":pw_hash,"cid":client_id})
        nu=await db.execute(sa_text("SELECT id FROM users WHERE TRIM(tenant_id)=:ptid AND email=:e ORDER BY id DESC LIMIT 1"),{"ptid":ptid,"e":email})
        new_uid=nu.fetchone().id
        await db.execute(sa_text("INSERT INTO user_tenant_memberships (canonical_email,tenant_id,user_id,role_in_tenant,is_primary,granted_by) VALUES (:e,:ptid,:uid,CAST(:r AS user_role_enum),false,:gby)"),{"e":email,"ptid":ptid,"uid":new_uid,"r":role,"gby":uid})
        for m in DEFAULT_MODULES:
            await db.execute(sa_text("INSERT INTO portal_module_access (tenant_id,user_id,module_key,is_enabled,granted_by) VALUES (:ptid,:uid,:mk,:en,:gby)"),{"ptid":ptid,"uid":new_uid,"mk":m["key"],"en":m["default"],"gby":uid})
        scope_cfg = json.dumps(DEFAULT_SCOPE_CONFIG)
        for mid in matter_ids:
            await db.execute(sa_text("INSERT INTO sub_tenant_matter_scope (tenant_id,parent_tenant_id,matter_id,granted_by,scope_config) VALUES (:ptid,:tid,CAST(:mid AS uuid),:gby,CAST(:cfg AS jsonb)) ON CONFLICT (tenant_id,matter_id) DO UPDATE SET revoked_at=NULL"),{"ptid":ptid,"tid":tid,"mid":mid,"gby":uid,"cfg":scope_cfg})
            sid_r=await db.execute(sa_text("SELECT id FROM sub_tenant_matter_scope WHERE TRIM(tenant_id)=:ptid AND matter_id=CAST(:mid AS uuid)"),{"ptid":ptid,"mid":mid})
            sid=sid_r.fetchone()
            if sid:
                for fk in STANDARD_FOLDERS.get("default",[]):
                    await db.execute(sa_text("INSERT INTO portal_folder_scope (scope_id,folder_key,is_visible,granted_by) VALUES (:sid,:fk,:vis,:gby) ON CONFLICT (scope_id,folder_key) DO NOTHING"),{"sid":sid.id,"fk":fk,"vis":fk not in DEFAULT_EXCLUDED,"gby":uid})
        token=secrets.token_urlsafe(48); expires=datetime.now(timezone.utc)+timedelta(hours=24 * 365)
        await db.execute(sa_text("INSERT INTO portal_magic_links (tenant_id,user_id,token,expires_at,created_by) VALUES (:ptid,:uid,:tok,:exp,:gby)"),{"ptid":ptid,"uid":new_uid,"tok":token,"exp":expires,"gby":uid})
        await db.commit()
    return JSONResponse({"status":"provisioned","user_id":new_uid,"email":email,"magic_link":f"https://{portal['domain']}/auth/magic?token={token}","expires_at":expires.isoformat()})


@router.post("/{client_id}/magic-link/{user_id}")
async def gen_magic_link(request: Request, client_id: str, user_id: int):
    tid=_tid(request); admin_uid=_uid(request)
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db)
        if not portal: return JSONResponse({"error":"No portal"},status_code=400)
        ptid=portal["id"]
        await db.execute(sa_text("UPDATE portal_magic_links SET expires_at=NOW() WHERE user_id=:uid AND used_at IS NULL AND expires_at>NOW()"),{"uid":user_id})
        token=secrets.token_urlsafe(48); expires=datetime.now(timezone.utc)+timedelta(hours=24 * 365)
        await db.execute(sa_text("INSERT INTO portal_magic_links (tenant_id,user_id,token,expires_at,created_by) VALUES (:ptid,:uid,:tok,:exp,:gby)"),{"ptid":ptid,"uid":user_id,"tok":token,"exp":expires,"gby":admin_uid})
        await db.commit()
    return JSONResponse({"magic_link":f"https://{portal['domain']}/auth/magic?token={token}","expires_at":expires.isoformat()})


@router.post("/{client_id}/user/{user_id}/revoke")
async def revoke_user(request: Request, client_id: str, user_id: int):
    tid=_tid(request)
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db); ptid=portal["id"]
        await db.execute(sa_text("UPDATE users SET is_active=false WHERE id=:uid AND TRIM(tenant_id)=:ptid"),{"uid":user_id,"ptid":ptid})
        await db.execute(sa_text("UPDATE portal_magic_links SET expires_at=NOW() WHERE user_id=:uid AND used_at IS NULL"),{"uid":user_id})
        await db.commit()
    return JSONResponse({"status":"revoked"})


@router.post("/{client_id}/user/{user_id}/reinstate")
async def reinstate_user(request: Request, client_id: str, user_id: int):
    tid=_tid(request)
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db); ptid=portal["id"]
        await db.execute(sa_text("UPDATE users SET is_active=true WHERE id=:uid AND TRIM(tenant_id)=:ptid"),{"uid":user_id,"ptid":ptid})
        await db.commit()
    return JSONResponse({"status":"reinstated"})


@router.put("/{client_id}/scope")
async def update_scope(request: Request, client_id: str):
    tid=_tid(request); uid=_uid(request); body=await request.json()
    add_ids=body.get("add",[]); remove_ids=body.get("remove",[])
    scope_cfg=json.dumps(DEFAULT_SCOPE_CONFIG)
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db); ptid=portal["id"]
        for mid in add_ids:
            await db.execute(sa_text("INSERT INTO sub_tenant_matter_scope (tenant_id,parent_tenant_id,matter_id,granted_by,scope_config) VALUES (:ptid,:tid,CAST(:mid AS uuid),:gby,CAST(:cfg AS jsonb)) ON CONFLICT (tenant_id,matter_id) DO UPDATE SET revoked_at=NULL"),{"ptid":ptid,"tid":tid,"mid":mid,"gby":uid,"cfg":scope_cfg})
            sid_r=await db.execute(sa_text("SELECT id FROM sub_tenant_matter_scope WHERE TRIM(tenant_id)=:ptid AND matter_id=CAST(:mid AS uuid)"),{"ptid":ptid,"mid":mid})
            sid=sid_r.fetchone()
            if sid:
                for fk in STANDARD_FOLDERS.get("default",[]):
                    await db.execute(sa_text("INSERT INTO portal_folder_scope (scope_id,folder_key,is_visible,granted_by) VALUES (:sid,:fk,:vis,:gby) ON CONFLICT (scope_id,folder_key) DO NOTHING"),{"sid":sid.id,"fk":fk,"vis":fk not in DEFAULT_EXCLUDED,"gby":uid})
        for sid in remove_ids:
            await db.execute(sa_text("UPDATE sub_tenant_matter_scope SET revoked_at=NOW() WHERE id=:sid AND TRIM(tenant_id)=:ptid"),{"sid":sid,"ptid":ptid})
        await db.commit()
    return JSONResponse({"status":"updated"})


@router.put("/{client_id}/folders/{scope_id}")
async def update_folders(request: Request, client_id: str, scope_id: int):
    uid=_uid(request); body=await request.json()
    async with AsyncSessionLocal() as db:
        for fk, visible in body.items():
            await db.execute(sa_text("INSERT INTO portal_folder_scope (scope_id,folder_key,is_visible,granted_by) VALUES (:sid,:fk,:vis,:gby) ON CONFLICT (scope_id,folder_key) DO UPDATE SET is_visible=:vis"),{"sid":scope_id,"fk":fk,"vis":bool(visible),"gby":uid})
        await db.commit()
    return JSONResponse({"status":"updated"})


@router.put("/{client_id}/modules/{user_id}")
async def update_modules(request: Request, client_id: str, user_id: int):
    tid=_tid(request); uid=_uid(request); body=await request.json()
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db); ptid=portal["id"]
        for mk, enabled in body.items():
            await db.execute(sa_text("INSERT INTO portal_module_access (tenant_id,user_id,module_key,is_enabled,granted_by) VALUES (:ptid,:uid,:mk,:en,:gby) ON CONFLICT (user_id,module_key) DO UPDATE SET is_enabled=:en"),{"ptid":ptid,"uid":user_id,"mk":mk,"en":bool(enabled),"gby":uid})
        await db.commit()
    return JSONResponse({"status":"updated"})


@router.post("/{client_id}/add-custom-folder/{scope_id}")
async def add_custom_folder(request: Request, client_id: str, scope_id: int):
    uid=_uid(request); body=await request.json()
    fk=body.get("folder_key","").strip()
    if not fk: return JSONResponse({"error":"Required"},status_code=400)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("INSERT INTO portal_folder_scope (scope_id,folder_key,is_visible,granted_by) VALUES (:sid,:fk,:vis,:gby) ON CONFLICT (scope_id,folder_key) DO UPDATE SET is_visible=:vis"),{"sid":scope_id,"fk":fk,"vis":body.get("visible",False),"gby":uid})
        await db.commit()
    return JSONResponse({"status":"added"})


@router.get("/{client_id}/access-log")
async def access_log(request: Request, client_id: str):
    tid=_tid(request); limit=int(request.query_params.get("limit",50))
    async with AsyncSessionLocal() as db:
        portal=await _portal(tid,db)
        if not portal: return JSONResponse({"logs":[]})
        ptid=portal["id"]
        uids=await db.execute(sa_text("SELECT id FROM users WHERE TRIM(tenant_id)=:ptid AND portal_access_client_id=CAST(:cid AS uuid)"),{"ptid":ptid,"cid":client_id})
        user_ids=[r.id for r in uids]
        if not user_ids: return JSONResponse({"logs":[]})
        logs=await db.execute(sa_text("SELECT al.action,al.details,al.ip_address,al.created_at,u.full_name,u.email FROM user_activity_log al JOIN users u ON u.id=al.user_id WHERE al.user_id=ANY(:uids) ORDER BY al.created_at DESC LIMIT :lim"),{"uids":user_ids,"lim":limit})
        return JSONResponse({"logs":[{"action":r["action"],"ip":r["ip_address"],"time":r["created_at"].isoformat() if r["created_at"] else "","name":r["full_name"],"details":r["details"]} for r in logs.mappings()]})
