"""
jobs/send_email_job.py — Background email tasks via RQ.

Two entry points:

1. send(payload)     — Full send + file. Used as fallback if sync send
                       fails or for future batch/scheduled sends.

2. file_only(payload) — File a copy of an already-sent email to the
                        matter's Email folder. Called by the send-email
                        endpoint after synchronous connector send.

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
import logging
import os

logger = logging.getLogger(__name__)


def send(payload: dict):
    """
    Full send + file. Payload keys:
      tenant_id, from_email, to, cc, bcc, subject,
      body_html, body_text, in_reply_to, references,
      matter_id (optional), mode,
      attachment_paths (optional — list of absolute disk paths)
    """
    import asyncio

    tenant_id = payload["tenant_id"]
    from_email = payload["from_email"]
    attachment_paths = payload.get("attachment_paths") or []

    logger.info("[send_email_job] sending from=%s to=%s subj=%s attachments=%d",
                from_email, payload.get("to"), payload.get("subject", "")[:60],
                len(attachment_paths))

    try:
        from core.services.email_send_connector import send_email

        result = asyncio.run(send_email(
            tenant_id=tenant_id,
            from_email=from_email,
            to=payload.get("to", ""),
            subject=payload.get("subject", ""),
            body_html=payload.get("body_html", ""),
            body_text=payload.get("body_text", ""),
            cc=payload.get("cc", ""),
            bcc=payload.get("bcc", ""),
            in_reply_to=payload.get("in_reply_to"),
            references=payload.get("references"),
            attachment_paths=attachment_paths if attachment_paths else None,
        ))

        logger.info("[send_email_job] sent via %s from=%s to=%s",
                     result.get("method", "?"), from_email, payload.get("to"))

        if payload.get("matter_id"):
            try:
                filed = _file_sent_email(payload)
                result["filed_as"] = filed
            except Exception as fe:
                logger.warning("[send_email_job] filing failed: %s", fe)

        return result

    except Exception as e:
        logger.error("[send_email_job] FAILED from=%s to=%s: %s",
                     from_email, payload.get("to"), e)
        raise


def file_only(payload: dict):
    """File a copy of an already-sent email to the matter's Email folder.
    Called after synchronous send completes in the endpoint."""
    if not payload.get("matter_id"):
        return

    try:
        filed = _file_sent_email(payload)
        logger.info("[send_email_job:file_only] filed=%s", filed)
        return {"filed_as": filed}
    except Exception as e:
        logger.error("[send_email_job:file_only] filing failed: %s", e)
        raise


def _file_sent_email(payload: dict) -> str:
    """File a copy of the sent email to the matter's Email folder.
    Builds a complete .eml with attachments for the DMS record.
    Returns the filed filename or empty string on failure."""
    import hashlib
    import mimetypes
    import uuid as _uuid
    from datetime import datetime, timezone
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    import psycopg2
    import psycopg2.extras

    tid = payload["tenant_id"]
    mid = payload["matter_id"]
    PROOT = "/mnt/praesidium"

    msg = MIMEMultipart("mixed")
    msg["From"] = payload["from_email"]
    msg["To"] = payload.get("to", "")
    msg["Subject"] = payload.get("subject", "")
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if payload.get("cc"):
        msg["Cc"] = payload["cc"]
    if payload.get("in_reply_to"):
        msg["In-Reply-To"] = payload["in_reply_to"]
        msg["References"] = payload.get("references") or payload["in_reply_to"]

    body_html = payload.get("body_html", "")
    body_text = payload.get("body_text", "")
    if body_html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text or "", "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body_text or "", "plain", "utf-8"))

    for fpath in (payload.get("attachment_paths") or []):
        if not os.path.isfile(fpath):
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

    db_url = os.environ.get("DATABASE_URL", "")
    from core.services.email_send_connector import _sync_db_connect
    conn = _sync_db_connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.matter_name, c.client_name FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND trim(c.tenant_id) = trim(m.tenant_id)
            WHERE m.id = CAST(%s AS uuid) AND trim(m.tenant_id) = trim(%s)
        """, (mid, tid))
        row = cur.fetchone()
        if not row:
            return ""

        root = os.path.join(PROOT, tid, "matters", row["client_name"], row["matter_name"])
        if not os.path.isdir(root):
            return ""

        email_folder = None
        for candidate in ["15-Email", "13-Email", "09-Email", "08-Email", "Email"]:
            cp = os.path.join(root, candidate)
            if os.path.isdir(cp):
                email_folder = cp
                break
        if not email_folder:
            email_folder = os.path.join(root, "15-Email")
            os.makedirs(email_folder, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_subj = "".join(c for c in (payload.get("subject", "sent"))[:60]
                            if c.isalnum() or c in " -_").strip()
        eml_filename = f"{ts}_{safe_subj}.eml"
        eml_path = os.path.join(email_folder, eml_filename)

        eml_raw = msg.as_string()
        with open(eml_path, "w") as ef:
            ef.write(eml_raw)

        doc_id = str(_uuid.uuid4())
        eml_cs = hashlib.sha256(eml_raw.encode()).hexdigest()
        eml_sz = os.path.getsize(eml_path)

        cur.execute("""
            INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename,
                mime_type, file_size, storage_path, checksum, version_number, status, created_at, updated_at)
            VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), %s, %s,
                'message/rfc822', %s, %s, %s, 1, 'active', NOW(), NOW())
            ON CONFLICT DO NOTHING
        """, (doc_id, tid, mid, eml_filename, eml_filename, eml_sz, eml_path, eml_cs))

        dms_doc_id = str(_uuid.uuid4())
        cur.execute("""
            INSERT INTO dms_documents (id, tenant_id, file_path, folder_root, file_hash,
                file_size_bytes, extraction_status, source, updated_at)
            VALUES (CAST(%s AS uuid), %s, %s, %s, %s,
                %s, 'complete', 'email_send', NOW())
            ON CONFLICT DO NOTHING
        """, (dms_doc_id, tid, eml_path, root, eml_cs, eml_sz))

        conn.commit()
        logger.info("[send_email_job] filed to %s", eml_path)
        return eml_filename
    finally:
        conn.close()