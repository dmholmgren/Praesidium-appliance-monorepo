"""
core/services/email_service.py
==============================
Outbound email — reads relay config from tenant_connectors (smtp_relay),
falls back to env vars. Routes through praesidium-smtp Postfix container
when available, or directly to the configured relay.

Usage:
    from core.services.email_service import send_email, send_share_link_email

    await send_email(
        tenant_id="986c0fee-...",
        to="recipient@firm.com",
        subject="Production Ready",
        body_html="<p>Your production is ready.</p>",
    )
"""

import json
import logging
import os
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

# Env var fallbacks (used when no tenant_connectors config exists)
_ENV_SMTP_HOST = os.environ.get("SMTP_HOST", "praesidium-smtp")
_ENV_SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
_ENV_SMTP_FROM = os.environ.get("SMTP_FROM_ADDRESS", "noreply@hjmmlegal.com")
_ENV_SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Praesidium")


async def _load_smtp_config(tenant_id: str) -> dict:
    """Load SMTP config from tenant_connectors, fall back to env vars."""
    if not tenant_id:
        return {
            "host": _ENV_SMTP_HOST, "port": _ENV_SMTP_PORT,
            "from_addr": _ENV_SMTP_FROM, "from_name": _ENV_SMTP_FROM_NAME,
            "use_tls": False, "username": "", "password": "",
        }
    try:
        from sqlalchemy import text
        from core.db.base import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            r = await session.execute(text("""
                SELECT config FROM tenant_connectors
                WHERE trim(tenant_id) = trim(:tid) AND connector = 'smtp_relay' AND is_active = true
                LIMIT 1
            """), {"tid": tenant_id})
            row = r.fetchone()
            if not row or not row[0]:
                return {
                    "host": _ENV_SMTP_HOST, "port": _ENV_SMTP_PORT,
                    "from_addr": _ENV_SMTP_FROM, "from_name": _ENV_SMTP_FROM_NAME,
                    "use_tls": False, "username": "", "password": "",
                }
            cfg = row[0] if isinstance(row[0], dict) else json.loads(row[0])

            # Decrypt credentials
            username = ""
            password = ""
            try:
                from modules.connectors.registry_router import _vault_decrypt
                r_creds = await session.execute(text("""
                    SELECT key_type, encrypted_key FROM credentials_vault
                    WHERE trim(tenant_id) = trim(:tid) AND provider = 'smtp_relay'
                """), {"tid": tenant_id})
                for cr in r_creds.mappings().fetchall():
                    if cr["key_type"] == "relay_username":
                        username = _vault_decrypt(cr["encrypted_key"] or "")
                    elif cr["key_type"] == "relay_password":
                        password = _vault_decrypt(cr["encrypted_key"] or "")
            except Exception as e:
                logger.warning("Failed to load SMTP credentials: %s", e)

            return {
                "host": cfg.get("relay_host", _ENV_SMTP_HOST),
                "port": int(cfg.get("relay_port", _ENV_SMTP_PORT)),
                "from_addr": cfg.get("from_address", _ENV_SMTP_FROM),
                "from_name": cfg.get("from_name", _ENV_SMTP_FROM_NAME),
                "use_tls": cfg.get("use_tls", False),
                "username": username,
                "password": password,
            }
    except Exception as e:
        logger.warning("Failed to load SMTP config from DB, using env: %s", e)
        return {
            "host": _ENV_SMTP_HOST, "port": _ENV_SMTP_PORT,
            "from_addr": _ENV_SMTP_FROM, "from_name": _ENV_SMTP_FROM_NAME,
            "use_tls": False, "username": "", "password": "",
        }


def _check_postfix_container() -> bool:
    """Check if praesidium-smtp container is reachable."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(("praesidium-smtp", 25))
        sock.close()
        return result == 0
    except Exception:
        return False


def _build_message(to, subject, body_html, body_text, from_addr, from_name, reply_to, cc):
    import re
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to

    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    else:
        plain = re.sub(r"<[^>]+>", "", body_html)
        plain = re.sub(r"\n\s*\n", "\n\n", plain).strip()
        msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    tenant_id: str = "",
    body_text: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[str] = None,
) -> bool:
    """Send email. Tries Postfix container first, falls back to direct relay."""
    cfg = await _load_smtp_config(tenant_id)
    msg = _build_message(to, subject, body_html, body_text,
                         cfg["from_addr"], cfg["from_name"], reply_to, cc)

    # Determine SMTP target: prefer Postfix container, fall back to relay
    if _check_postfix_container():
        smtp_host, smtp_port = "praesidium-smtp", 25
        use_tls = False
        auth_user, auth_pass = "", ""
    else:
        smtp_host = cfg["host"]
        smtp_port = cfg["port"]
        use_tls = cfg["use_tls"]
        auth_user = cfg["username"]
        auth_pass = cfg["password"]

    try:
        recipients = [to]
        if cc:
            recipients.extend([a.strip() for a in cc.split(",") if a.strip()])
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if use_tls:
                server.starttls()
            if auth_user and auth_pass:
                server.login(auth_user, auth_pass)
            server.sendmail(cfg["from_addr"], recipients, msg.as_string())
        logger.info("Email sent to %s via %s:%s: %s", to, smtp_host, smtp_port, subject)
        return True
    except Exception as e:
        logger.error("Email send failed to %s via %s:%s: %s", to, smtp_host, smtp_port, e)
        return False


async def send_share_link_email(
    to: str,
    production_name: str,
    matter_name: str,
    download_url: str,
    tenant_id: str = "",
    sender_name: str = "",
    message: str = "",
    expires_at: Optional[str] = None,
) -> bool:
    """Send a production share link email with branded template."""
    expiry_note = f"<p style='color:#6b7280;font-size:13px;'>This link expires {expires_at}.</p>" if expires_at else ""
    custom_msg = f"<p style='margin:12px 0;'>{message}</p>" if message else ""
    sender_line = f"<p style='font-size:13px;color:#6b7280;'>Sent by {sender_name}</p>" if sender_name else ""

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a;">
      <div style="border-bottom:3px solid #0D1F3C;padding:16px 0;margin-bottom:20px;">
        <h2 style="margin:0;font-size:18px;color:#0D1F3C;">Production Ready for Download</h2>
      </div>
      <p>A document production is available for download:</p>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:16px;margin:16px 0;">
        <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Production</div>
        <div style="font-size:15px;font-weight:600;">{production_name}</div>
        <div style="font-size:13px;color:#6b7280;margin-top:8px;margin-bottom:4px;">Matter</div>
        <div style="font-size:14px;">{matter_name}</div>
      </div>
      {custom_msg}
      <div style="text-align:center;margin:24px 0;">
        <a href="{download_url}" style="display:inline-block;padding:12px 32px;background:#0D1F3C;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;">Download Production</a>
      </div>
      {expiry_note}
      {sender_line}
      <div style="border-top:1px solid #e2e8f0;margin-top:24px;padding-top:12px;font-size:11px;color:#9ca3af;">
        This is an automated message. Do not reply directly to this email.
      </div>
    </div>
    """

    return await send_email(
        to=to,
        subject=f"Production: {production_name} \u2014 {matter_name}",
        body_html=html,
        tenant_id=tenant_id,
    )