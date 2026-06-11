"""
modules/tenant_admin/email_routes.py
Email relay configuration for tenant admins.
Routes:
  GET  /tenant-admin/email           — Email settings page
  POST /tenant-admin/email           — Save config
  GET  /tenant-admin/email/status    — JSON relay + container status
  POST /tenant-admin/email/test      — Send test email
"""

import json
import logging
import os
import smtplib
import socket

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenant-admin", tags=["tenant-admin-email"])

_templates = Jinja2Templates(directory=["core/templates", "modules/tenant_admin/templates"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _require_admin(user, request):
    role = getattr(user, "role", "")
    return role in ("admin", "platform_admin")


async def _get_smtp_config(tid: str) -> dict:
    """Read SMTP config from tenant_connectors + credentials_vault."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT config, is_active FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid) AND connector = 'smtp_relay'
            LIMIT 1
        """), {"tid": tid})
        row = r.mappings().fetchone()
        if not row:
            return {}
        config = row["config"] or {}
        if isinstance(config, str):
            config = json.loads(config)
        config["is_active"] = row["is_active"]

        # Check for credentials
        r_cred = await session.execute(text("""
            SELECT key_type, encrypted_key FROM credentials_vault
            WHERE trim(tenant_id) = trim(:tid) AND provider = 'smtp_relay'
        """), {"tid": tid})
        for cr in r_cred.mappings().fetchall():
            if cr["key_type"] == "relay_username" and cr["encrypted_key"]:
                config["has_username"] = True
            if cr["key_type"] == "relay_password" and cr["encrypted_key"]:
                config["has_password"] = True
    return config


async def _get_credential(tid: str, key_type: str) -> str:
    """Decrypt a credential from the vault."""
    from modules.connectors.registry_router import _vault_decrypt
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT encrypted_key FROM credentials_vault
            WHERE trim(tenant_id) = trim(:tid) AND provider = 'smtp_relay' AND key_type = :kt
            LIMIT 1
        """), {"tid": tid, "kt": key_type})
        row = r.fetchone()
        if not row or not row[0]:
            return ""
        return _vault_decrypt(row[0])


def _check_container() -> bool:
    """Check if praesidium-smtp container is reachable on port 25."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(("praesidium-smtp", 25))
        sock.close()
        return result == 0
    except Exception:
        return False


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/email", response_class=HTMLResponse)
async def email_settings_page(request: Request, saved: str = "", user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tid(request)
    config = await _get_smtp_config(tid)

    # Get username hint for display
    cred_username_hint = ""
    if config.get("has_username"):
        raw = await _get_credential(tid, "relay_username")
        if raw:
            cred_username_hint = raw  # Show full username — not a secret

    branding = getattr(request.state, "branding", None)
    return _templates.TemplateResponse(request, "tenant_admin/email_settings.html", {
        "config": config,
        "cred_username_hint": cred_username_hint,
        "has_password": config.get("has_password", False),
        "user": user,
        "branding": branding,
        "saved": saved,
    })


@router.post("/email")
async def email_settings_save(
    request: Request,
    relay_host: str = Form(""),
    relay_port: str = Form("25"),
    from_address: str = Form(""),
    from_name: str = Form(""),
    from_domain: str = Form(""),
    use_tls: str = Form(""),
    relay_username: str = Form(""),
    relay_password: str = Form(""),
    user=Depends(get_current_user),
):
    if not _require_admin(user, request):
        return RedirectResponse("/dashboard", status_code=303)
    tid = _tid(request)

    config = {
        "relay_host": relay_host.strip(),
        "relay_port": relay_port.strip() or "25",
        "from_address": from_address.strip(),
        "from_name": from_name.strip() or "Praesidium",
        "from_domain": from_domain.strip(),
        "use_tls": use_tls in ("on", "true", "1"),
    }

    async with AsyncSessionLocal() as session:
        # Upsert tenant_connectors
        existing = await session.execute(text("""
            SELECT id FROM tenant_connectors
            WHERE trim(tenant_id) = trim(:tid) AND connector = 'smtp_relay' LIMIT 1
        """), {"tid": tid})
        row = existing.fetchone()

        if row:
            await session.execute(text("""
                UPDATE tenant_connectors
                SET config = CAST(:cfg AS jsonb), is_active = true,
                    status = 'configured', updated_at = NOW()
                WHERE id = :rid
            """), {"cfg": json.dumps(config), "rid": row[0]})
        else:
            await session.execute(text("""
                INSERT INTO tenant_connectors
                    (tenant_id, connector, connector_type, config, is_active, status, sync_frequency, created_at, updated_at)
                VALUES (:tid, 'smtp_relay', 'smtp_relay', CAST(:cfg AS jsonb), true, 'configured', 'manual', NOW(), NOW())
            """), {"tid": tid, "cfg": json.dumps(config)})

        # Save credentials if provided
        from modules.connectors.registry_router import _vault_encrypt
        if relay_username.strip() and relay_username.strip() != "••••••••":
            encrypted = _vault_encrypt(relay_username.strip())
            await session.execute(text("""
                INSERT INTO credentials_vault (tenant_id, provider, key_type, encrypted_key, updated_at)
                VALUES (:tid, 'smtp_relay', 'relay_username', :val, NOW())
                ON CONFLICT (tenant_id, provider, key_type)
                DO UPDATE SET encrypted_key = :val, updated_at = NOW()
            """), {"tid": tid, "val": encrypted})

        if relay_password.strip() and relay_password.strip() != "••••••••":
            encrypted = _vault_encrypt(relay_password.strip())
            await session.execute(text("""
                INSERT INTO credentials_vault (tenant_id, provider, key_type, encrypted_key, updated_at)
                VALUES (:tid, 'smtp_relay', 'relay_password', :val, NOW())
                ON CONFLICT (tenant_id, provider, key_type)
                DO UPDATE SET encrypted_key = :val, updated_at = NOW()
            """), {"tid": tid, "val": encrypted})

        await session.commit()

    # Also update env-based config for the Postfix container
    # (writes to .env or env vars that docker-compose reads)
    logger.info("SMTP relay configured: %s:%s from %s", config["relay_host"], config["relay_port"], config["from_address"])

    return RedirectResponse("/tenant-admin/email?saved=1", status_code=303)


@router.get("/email/status")
async def email_status(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    config = await _get_smtp_config(tid)
    container_running = _check_container()

    return JSONResponse({
        "configured": bool(config.get("relay_host")),
        "container_running": container_running,
        "relay_host": config.get("relay_host", ""),
        "relay_port": config.get("relay_port", "25"),
        "from_address": config.get("from_address", ""),
        "is_active": config.get("is_active", False),
    })


@router.post("/email/test")
async def email_test(request: Request, user=Depends(get_current_user)):
    if not _require_admin(user, request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 403)
    tid = _tid(request)
    body = await request.json()
    to_addr = (body.get("to") or "").strip()
    if not to_addr:
        return JSONResponse({"ok": False, "error": "No recipient address"})

    config = await _get_smtp_config(tid)
    if not config.get("relay_host"):
        return JSONResponse({"ok": False, "error": "SMTP relay not configured — save settings first"})

    # Try sending through the Postfix container first, fall back to direct relay
    smtp_host = "praesidium-smtp"
    smtp_port = 25
    from_addr = config.get("from_address", "noreply@hjmmlegal.com")
    from_name = config.get("from_name", "Praesidium")

    # Check if container is reachable
    if not _check_container():
        # Try direct relay
        smtp_host = config["relay_host"]
        smtp_port = int(config.get("relay_port", 25))

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import datetime

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_addr
    msg["Subject"] = f"Praesidium Test Email — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;">
      <div style="border-bottom:3px solid #0D1F3C;padding:14px 0;margin-bottom:16px;">
        <h2 style="margin:0;font-size:16px;color:#0D1F3C;">Test Email — SMTP Relay Working</h2>
      </div>
      <p>This is a test email from Praesidium to confirm the SMTP relay is configured correctly.</p>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:14px;margin:14px 0;font-size:13px;">
        <div><strong>Relay:</strong> {config.get('relay_host','')}:{config.get('relay_port','25')}</div>
        <div><strong>From:</strong> {from_addr}</div>
        <div><strong>Container:</strong> {'praesidium-smtp' if smtp_host == 'praesidium-smtp' else 'direct relay'}</div>
        <div><strong>Sent at:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
      </div>
      <p style="font-size:12px;color:#6b7280;">If you received this, email delivery is working.</p>
    </div>
    """
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        use_tls = config.get("use_tls", False)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if use_tls and smtp_host != "praesidium-smtp":
                server.starttls()
            # Auth if credentials exist
            if smtp_host != "praesidium-smtp":
                username = await _get_credential(tid, "relay_username")
                password = await _get_credential(tid, "relay_password")
                if username and password:
                    server.login(username, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())

        return JSONResponse({
            "ok": True,
            "detail": f"Sent via {smtp_host}:{smtp_port}"
        })
    except Exception as e:
        logger.error("Test email failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})

# --- SMTP container management (appended) ---

@router.post("/email/smtp-start")
async def email_smtp_start(request: Request, user=Depends(get_current_user)):
    """Start or create the praesidium-smtp container via Docker SDK."""
    if not _require_admin(user, request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 403)
    try:
        import docker
        client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
        try:
            container = client.containers.get("praesidium-smtp")
            if container.status == "running":
                return JSONResponse({"ok": True, "detail": "Already running"})
            container.start()
            return JSONResponse({"ok": True, "detail": "Container started"})
        except docker.errors.NotFound:
            # Container doesn't exist — create it from the boky/postfix image
            # Pull image first
            try:
                client.images.get("boky/postfix:latest")
            except docker.errors.ImageNotFound:
                logger.info("Pulling boky/postfix:latest...")
                client.images.pull("boky/postfix", tag="latest")
            # Read SMTP config from DB for env vars
            tid = _tid(request)
            config = await _get_smtp_config(tid)
            relay_host = config.get("relay_host", "exchange.hjmmlegal.com")
            relay_port = config.get("relay_port", "25")
            from_domain = config.get("from_domain", "hjmmlegal.com")
            env = {
                "RELAYHOST": relay_host + ":" + str(relay_port),
                "ALLOWED_SENDER_DOMAINS": from_domain,
                "HOSTNAME": "mail." + from_domain,
                "SMTP_TLS": "yes",
                "SMTP_TLS_SECURITY_LEVEL": "may",
            }
            # Add relay auth if configured
            username = await _get_credential(tid, "relay_username")
            password = await _get_credential(tid, "relay_password")
            if username:
                env["RELAYHOST_USERNAME"] = username
            if password:
                env["RELAYHOST_PASSWORD"] = password
            container = client.containers.run(
                "boky/postfix:latest",
                name="praesidium-smtp",
                detach=True,
                restart_policy={"Name": "unless-stopped"},
                environment=env,
                network="praesidium-internal",
                volumes={"smtp-spool": {"bind": "/var/spool/postfix", "mode": "rw"}},
                healthcheck={
                    "test": ["CMD", "postfix", "status"],
                    "interval": 30000000000,
                    "timeout": 5000000000,
                    "retries": 3,
                },
            )
            return JSONResponse({"ok": True, "detail": "Container created and started"})
    except ImportError:
        return JSONResponse({"ok": False, "error": "Python docker SDK not installed. Run: pip install docker"})
    except Exception as e:
        logger.error("SMTP start failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/email/smtp-stop")
async def email_smtp_stop(request: Request, user=Depends(get_current_user)):
    """Stop the praesidium-smtp container."""
    if not _require_admin(user, request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, 403)
    try:
        import docker
        client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
        container = client.containers.get("praesidium-smtp")
        container.stop(timeout=10)
        return JSONResponse({"ok": True, "detail": "Container stopped"})
    except ImportError:
        return JSONResponse({"ok": False, "error": "Python docker SDK not installed"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
