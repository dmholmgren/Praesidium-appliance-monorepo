"""
core/services/email_send_connector.py — Provider-agnostic email send.

Dispatches by tenant connector type:
  exchange         → EWS impersonation (appears in user's Sent Items)
  office365        → Microsoft Graph API (future)
  google_workspace → Gmail API (future)
  smtp / fallback  → SMTP relay (system emails, external/third-party)

Usage:
    from core.services.email_send_connector import send_email
    result = await send_email(
        tenant_id=tid,
        from_email="dennis@hjmmlegal.com",
        to="client@example.com",
        cc="partner@hjmmlegal.com",
        subject="Re: Settlement",
        body_html="<p>Please review...</p>",
        body_text="Please review...",
        in_reply_to="<msg-id@exchange>",
        attachment_paths=["/mnt/praesidium/.../doc.pdf"],
    )

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
import base64
import logging
import mimetypes
import os
from typing import Optional

logger = logging.getLogger("praesidium.email_send")


async def send_email(
    tenant_id: str,
    from_email: str,
    to: str,
    subject: str = "",
    body_html: str = "",
    body_text: str = "",
    cc: str = "",
    bcc: str = "",
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    attachment_paths: Optional[list] = None,
) -> dict:
    """Send email via the tenant's configured provider. Returns result dict."""
    provider = await _detect_provider(tenant_id)
    logger.info("[email_send] tenant=%s provider=%s from=%s to=%s attachments=%d",
                tenant_id, provider, from_email, to,
                len(attachment_paths) if attachment_paths else 0)

    if provider == "stalwart":
        return await _send_stalwart(
            tenant_id, from_email, to, cc, bcc, subject,
            body_html, body_text, in_reply_to, references,
            attachment_paths,
        )
    elif provider == "exchange":
        return await _send_ews(
            tenant_id, from_email, to, cc, bcc, subject,
            body_html, body_text, in_reply_to, references,
            attachment_paths,
        )
    elif provider == "office365":
        raise NotImplementedError("Office 365 Graph send not yet implemented")
    elif provider == "google_workspace":
        raise NotImplementedError("Google Workspace send not yet implemented")
    else:
        return await _send_smtp(
            tenant_id, from_email, to, cc, bcc, subject,
            body_html, body_text, in_reply_to, references,
            attachment_paths,
        )


async def _detect_provider(tenant_id: str) -> str:
    """Check tenant_connectors for active email provider."""
    from sqlalchemy import text as sa_text
    from core.db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT connector_type FROM tenant_connectors
            WHERE TRIM(tenant_id) = TRIM(:tid)
              AND connector_type IN ('stalwart', 'exchange', 'office365', 'google_workspace')
              AND is_active = true
            ORDER BY (connector_type <> 'stalwart')
            LIMIT 1
        """), {"tid": tenant_id})
        row = r.fetchone()

    if row:
        return row[0]
    return "smtp"


async def _send_ews(
    tenant_id: str, from_email: str, to: str, cc: str, bcc: str,
    subject: str, body_html: str, body_text: str,
    in_reply_to: str, references: str,
    attachment_paths: Optional[list] = None,
) -> dict:
    """Send via Exchange Web Services with impersonation.
    Uses send_and_save() so the email appears in the user's Sent Items.
    Supports file attachments from disk paths."""
    import psycopg2
    import psycopg2.extras

    db_url = os.environ.get("DATABASE_URL", "")
    conn = _sync_db_connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT key_type, encrypted_key FROM credentials_vault "
            "WHERE TRIM(tenant_id) = TRIM(%s) AND provider = 'exchange'",
            (tenant_id,))
        raw_creds = {r["key_type"]: r["encrypted_key"] for r in cur.fetchall()}

        cur.execute(
            "SELECT config FROM tenant_connectors "
            "WHERE TRIM(tenant_id) = TRIM(%s) AND connector = 'exchange' LIMIT 1",
            (tenant_id,))
        cfg_row = cur.fetchone()
        config = cfg_row["config"] if cfg_row else {}
        cur.close()
    finally:
        conn.close()

    creds = _decrypt_creds(raw_creds)
    if not creds.get("username") or not creds.get("password"):
        raise ValueError("Exchange credentials missing from credentials_vault")

    ews_url = config.get("ews_url", "")
    domain = creds.get("domain", "")
    full_username = f"{domain}\\{creds['username']}" if domain else creds["username"]

    from exchangelib import (
        Credentials, Configuration, Account, IMPERSONATION,
        Message, Mailbox, HTMLBody, FileAttachment,
    )
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
    import urllib3
    urllib3.disable_warnings()
    BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    ews_creds = Credentials(username=full_username, password=creds["password"])
    ews_config = Configuration(
        service_endpoint=ews_url,
        credentials=ews_creds,
        auth_type="NTLM",
    )
    account = Account(
        primary_smtp_address=from_email,
        config=ews_config,
        autodiscover=False,
        access_type=IMPERSONATION,
    )

    to_list = [Mailbox(email_address=a.strip()) for a in to.split(",") if a.strip()]
    cc_list = [Mailbox(email_address=a.strip()) for a in (cc or "").split(",") if a.strip()] or None
    bcc_list = [Mailbox(email_address=a.strip()) for a in (bcc or "").split(",") if a.strip()] or None

    body = HTMLBody(body_html) if body_html else (body_text or "")

    msg = Message(
        account=account,
        subject=subject,
        body=body,
        to_recipients=to_list,
        cc_recipients=cc_list,
        bcc_recipients=bcc_list,
    )
    if in_reply_to:
        msg.in_reply_to = in_reply_to

    if attachment_paths:
        for fpath in attachment_paths:
            if not os.path.isfile(fpath):
                logger.warning("[email_send:ews] attachment not found, skipping: %s", fpath)
                continue
            fname = os.path.basename(fpath)
            ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            with open(fpath, "rb") as af:
                content = af.read()
            att = FileAttachment(
                name=fname,
                content=content,
                content_type=ct,
            )
            msg.attach(att)
        logger.info("[email_send:ews] attached %d file(s)", len(attachment_paths))

    msg.send_and_save()

    logger.info("[email_send:ews] sent from %s to %s subj=%s", from_email, to, subject[:60])
    return {
        "status": "ok",
        "method": "ews",
        "from": from_email,
        "to": to,
        "attachments": len(attachment_paths) if attachment_paths else 0,
    }


async def _send_smtp(
    tenant_id: str, from_email: str, to: str, cc: str, bcc: str,
    subject: str, body_html: str, body_text: str,
    in_reply_to: str, references: str,
    attachment_paths: Optional[list] = None,
) -> dict:
    """Send via SMTP relay. Used for system emails and non-Exchange tenants."""
    import smtplib
    import uuid as _uuid
    from datetime import datetime, timezone
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    from core.services.email_service import _load_smtp_config, _check_postfix_container
    cfg = await _load_smtp_config(tenant_id)

    from_addr = from_email or cfg["from_addr"]

    msg = MIMEMultipart("mixed")
    if body_html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text or "", "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body_text or "", "plain", "utf-8"))

    if attachment_paths:
        for fpath in attachment_paths:
            if not os.path.isfile(fpath):
                logger.warning("[email_send:smtp] attachment not found, skipping: %s", fpath)
                continue
            fname = os.path.basename(fpath)
            ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            main_t, sub_t = ct.split("/", 1)
            with open(fpath, "rb") as af:
                att = MIMEBase(main_t, sub_t)
                att.set_payload(af.read())
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(att)

    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = f"<{_uuid.uuid4()}@praesidium>"
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if cc:
        recipients.extend([a.strip() for a in cc.split(",") if a.strip()])
    if bcc:
        recipients.extend([a.strip() for a in bcc.split(",") if a.strip()])

    if _check_postfix_container():
        smtp_host, smtp_port = "praesidium-smtp", 25
    else:
        smtp_host, smtp_port = cfg["host"], cfg["port"]

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        if cfg.get("use_tls"):
            server.starttls()
        if cfg.get("username") and cfg.get("password"):
            server.login(cfg["username"], cfg["password"])
        server.sendmail(from_addr, recipients, msg.as_string())

    logger.info("[email_send:smtp] sent from %s to %s attachments=%d",
                from_addr, to, len(attachment_paths) if attachment_paths else 0)
    return {
        "status": "ok",
        "method": "smtp",
        "from": from_addr,
        "to": to,
        "message_id": msg["Message-ID"],
        "attachments": len(attachment_paths) if attachment_paths else 0,
    }


async def _send_stalwart(
    tenant_id: str, from_email: str, to: str, cc: str, bcc: str,
    subject: str, body_html: str, body_text: str,
    in_reply_to: str, references: str,
    attachment_paths=None,
) -> dict:
    """Send via Stalwart JMAP EmailSubmission -> outbound smarthost (saves to Sent).
    Uses admin/service auth; no per-user password required."""
    from core.services import stalwart_mailbox as sm
    from core.services.stalwart_service import (
        _get_stalwart_account_by_name, _username_from_email,
    )
    acct = await _get_stalwart_account_by_name(_username_from_email(from_email))
    if not acct:
        raise ValueError(f"No Stalwart account for {from_email}")
    account_id = acct["id"]

    atts = []
    if attachment_paths:
        for fpath in attachment_paths:
            if not os.path.isfile(fpath):
                logger.warning("[email_send:stalwart] attachment missing: %s", fpath)
                continue
            fname = os.path.basename(fpath)
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            with open(fpath, "rb") as af:
                atts.append((fname, ctype, af.read()))

    res = await sm.send_message(
        account_id, from_addr=from_email, to=to, cc=cc, bcc=bcc,
        subject=subject, text=body_text, html=body_html,
        attachments=atts or None, in_reply_to=in_reply_to, references=references,
    )
    if not res.get("success"):
        raise RuntimeError(f"Stalwart send failed: {res}")
    logger.info("[email_send:stalwart] sent from %s to %s email_id=%s sub=%s att=%d",
                from_email, to, res.get("email_id"), res.get("submission_id"), len(atts))
    return {
        "status": "ok", "method": "stalwart", "from": from_email, "to": to,
        "email_id": res.get("email_id"), "submission_id": res.get("submission_id"),
        "attachments": len(atts),
    }


def _sync_db_connect(db_url: str):
    """Parse DATABASE_URL and return a psycopg2 connection."""
    import psycopg2
    import psycopg2.extras

    idx = db_url.rfind("@")
    before = db_url[:idx]
    after = db_url[idx + 1:]
    scheme_end = before.index("://") + 3
    creds_str = before[scheme_end:]
    colon = creds_str.index(":")
    user, password = creds_str[:colon], creds_str[colon + 1:]
    host_db = after
    host_port, dbname = (host_db.rsplit("/", 1) if "/" in host_db
                         else (host_db, "praesidium"))
    if ":" in host_port:
        host, port = host_port.split(":", 1)
        port = int(port)
    else:
        host, port = host_port, 5432
    return psycopg2.connect(
        host=host, port=port, user=user, password=password,
        dbname=dbname, cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _decrypt_creds(raw: dict) -> dict:
    """Decrypt Fernet-encrypted credential values."""
    from cryptography.fernet import Fernet

    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)

    result = {}
    for k, v in raw.items():
        if v and v.startswith("gAAAAA"):
            try:
                result[k] = f.decrypt(v.encode()).decode()
            except Exception:
                result[k] = v
        else:
            result[k] = v
    return result