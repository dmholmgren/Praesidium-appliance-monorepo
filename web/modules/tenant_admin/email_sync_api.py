"""
modules/tenant_admin/email_sync_api.py
API for email sync + routing + FILING from the Metadata Reconciliation page.

v2 — adds POST /file-email endpoint that exports email from Exchange
to the matter's Email folder on disk (15-Email or 09-Email), creates
dms_documents + documents rows, and auto-populates matter_contacts.
"""
import logging, json, re, os, hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenant-admin/email-sync/api", tags=["email-sync"])


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _safe_filename(s: str, max_len: int = 80) -> str:
    """Sanitize a string for use as a filename component."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s)
    s = s.strip('. ')
    return s[:max_len] if s else 'untitled'


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_email_folder(session, tid: str, matter_id: str) -> str | None:
    """Find the matter's Praesidium folder, then locate the Email subfolder."""
    row = (await session.execute(text("""
        SELECT disk_root FROM matter_folders
        WHERE matter_id = CAST(:mid AS uuid)
          AND TRIM(tenant_id) = :tid
          AND disk_root LIKE '/mnt/praesidium/%%'
        LIMIT 1
    """), {"mid": matter_id, "tid": tid})).fetchone()
    if not row:
        return None

    praesidium_root = row[0]
    if not os.path.isdir(praesidium_root):
        return None

    # Check for Email subfolder — any numbered prefix ending in -Email
    for candidate in sorted(os.listdir(praesidium_root)):
        if candidate.lower().endswith('-email') or candidate.lower() == 'email':
            return os.path.join(praesidium_root, candidate)

    # Not found — create 15-Email as fallback
    email_dir = os.path.join(praesidium_root, "15-Email")
    os.makedirs(email_dir, exist_ok=True)
    return email_dir


async def _get_exchange_creds(session, tid: str) -> dict:
    """Decrypt Exchange credentials from credentials_vault."""
    import base64
    from cryptography.fernet import Fernet

    rows = (await session.execute(text("""
        SELECT key_type, encrypted_key FROM credentials_vault
        WHERE TRIM(tenant_id) = :tid AND provider = 'exchange'
    """), {"tid": tid})).fetchall()
    raw = {r[0]: r[1] for r in rows}

    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)

    creds = {}
    for k, v in raw.items():
        if v and v.startswith("gAAAAA"):
            try:
                creds[k] = f.decrypt(v.encode()).decode()
            except Exception:
                creds[k] = v
        else:
            creds[k] = v
    return creds


async def _get_exchange_config(session, tid: str) -> dict:
    row = (await session.execute(text("""
        SELECT config FROM tenant_connectors
        WHERE TRIM(tenant_id) = :tid AND connector = 'exchange' LIMIT 1
    """), {"tid": tid})).fetchone()
    return row[0] if row else {}


async def _export_email_from_exchange(session, tid: str, message_id: str,
                                        eq_attorney_user_id=None) -> bytes | None:
    """Connect to Exchange via EWS, export the full MIME content of an email."""
    creds = await _get_exchange_creds(session, tid)
    if not creds.get('username') or not creds.get('password'):
        return None

    ews_cfg_data = await _get_exchange_config(session, tid)
    ews_url = ews_cfg_data.get('ews_url', '')

    # Determine which mailbox to impersonate
    mailbox_email = None
    if eq_attorney_user_id:
        mb_row = (await session.execute(text("""
            SELECT entity_email FROM connector_entity_map
            WHERE mapped_user_id = :uid AND TRIM(tenant_id) = :tid
              AND connector_type = 'exchange' AND is_active = true
            LIMIT 1
        """), {"uid": eq_attorney_user_id, "tid": tid})).fetchone()
        if mb_row:
            mailbox_email = mb_row[0]

    if not mailbox_email:
        fb = (await session.execute(text("""
            SELECT entity_email FROM connector_entity_map
            WHERE TRIM(tenant_id) = :tid AND connector_type = 'exchange'
              AND is_active = true AND entity_type IN ('mailbox', 'shared_mailbox')
            LIMIT 1
        """), {"tid": tid})).fetchone()
        if not fb:
            return None
        mailbox_email = fb[0]

    try:
        from exchangelib import Credentials, Configuration, Account, IMPERSONATION
        from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
        import urllib3
        urllib3.disable_warnings()
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

        domain = creds.get('domain', '')
        username = creds['username']
        password = creds['password']
        full_username = f"{domain}\\{username}" if domain else username

        ews_creds = Credentials(username=full_username, password=password)
        ews_config = Configuration(
            service_endpoint=ews_url,
            credentials=ews_creds,
            auth_type='NTLM',
        )
        account = Account(
            primary_smtp_address=mailbox_email,
            config=ews_config,
            autodiscover=False,
            access_type=IMPERSONATION,
        )

        # Search inbox, then sent
        items = list(account.inbox.filter(message_id=message_id).only('mime_content')[:1])
        if not items:
            items = list(account.sent.filter(message_id=message_id).only('mime_content')[:1])
        if not items:
            return None
        return items[0].mime_content

    except Exception as exc:
        logger.exception("EWS export failed for message_id=%s: %s", message_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def email_stats(request: Request, user=Depends(get_current_user)):
    """Email queue stats."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE eq.routing_status = 'pending') AS pending,
                   COUNT(*) FILTER (WHERE eq.routing_status = 'matched') AS matched,
                   COUNT(*) FILTER (WHERE eq.routing_status = 'filed') AS filed,
                   COUNT(*) FILTER (WHERE eq.routing_status = 'skipped') AS skipped,
                   COUNT(*) FILTER (WHERE eq.filed_to_dms = true) AS filed_to_dms,
                   MIN(eq.received_at) AS earliest,
                   MAX(eq.received_at) AS latest
            FROM email_routing_queue eq WHERE TRIM(eq.tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

        last_sync = (await session.execute(text("""
            SELECT last_sync_at FROM tenant_connectors
            WHERE TRIM(tenant_id) = :tid AND connector = 'exchange'
        """), {"tid": tid})).fetchone()

        mailbox_count = (await session.execute(text("""
            SELECT COUNT(*) FROM connector_entity_map
            WHERE TRIM(tenant_id) = :tid AND connector_type = 'exchange' AND is_active = true
        """), {"tid": tid})).scalar()

    def _ser(d):
        import datetime as _dt
        from decimal import Decimal as _D
        out = {}
        for k, v in d.items():
            if isinstance(v, (_dt.datetime, _dt.date)):
                out[k] = v.isoformat()
            elif isinstance(v, _D):
                out[k] = float(v)
            else:
                out[k] = v
        return out

    return JSONResponse({
        "queue": _ser(dict(r)) if r else {},
        "last_sync": str(last_sync[0]) if last_sync and last_sync[0] else None,
        "mailbox_count": mailbox_count or 0,
    })


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/queue")
async def email_queue(request: Request, page: int = 1, status: str = "",
                      search: str = "", user=Depends(get_current_user)):
    """Paginated email queue with search/filter."""
    tid = _tid(request)
    per_page = 50
    offset = (page - 1) * per_page

    filters = ["TRIM(eq.tenant_id) = :tid"]
    params = {"tid": tid, "lim": per_page, "off": offset}

    if status:
        filters.append("eq.routing_status = :status")
        params["status"] = status
    if search:
        filters.append("(eq.subject ILIKE :q OR eq.from_email ILIKE :q OR eq.from_display ILIKE :q OR eq.body_preview ILIKE :q)")
        params["q"] = f"%{search}%"

    where = " AND ".join(filters)

    async with AsyncSessionLocal() as session:
        total = (await session.execute(text(
            f"SELECT COUNT(*) FROM email_routing_queue eq WHERE {where}"
        ), params)).scalar() or 0

        rows = (await session.execute(text(f"""
            SELECT eq.id::text, eq.subject, eq.from_email, eq.from_display,
                   eq.to_emails, eq.received_at, eq.routing_status, eq.has_attachments,
                   eq.attachment_count, eq.match_confidence,
                   eq.matched_matter_id::text, eq.match_signals,
                   eq.filed_to_dms, eq.filing_status, eq.filed_matter_id::text,
                   m.matter_name, c.client_name
            FROM email_routing_queue eq
            LEFT JOIN matters m ON m.id = eq.matched_matter_id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE {where}
            ORDER BY eq.received_at DESC
            LIMIT :lim OFFSET :off
        """), params)).mappings().all()

    def ser(v):
        if v is None: return None
        if hasattr(v, 'hex'): return str(v)
        if hasattr(v, 'isoformat'): return v.isoformat()
        if isinstance(v, (int, float, bool, str)): return v
        from decimal import Decimal as _D
        if isinstance(v, _D): return float(v)
        return str(v)

    return JSONResponse({
        "emails": [{k: ser(v) for k, v in dict(r).items()} for r in rows],
        "total": total,
        "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })


# ─────────────────────────────────────────────────────────────────────────────
# SYNC
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sync")
async def trigger_sync(request: Request, user=Depends(get_current_user)):
    """Trigger Exchange sync inline."""
    tid = _tid(request)
    body = await request.json()
    lookback_days = body.get("lookback_days", 1)

    try:
        import psycopg2, psycopg2.extras
        db_url = os.environ.get("DATABASE_URL", "")
        idx = db_url.rfind("@")
        before = db_url[:idx]
        after = db_url[idx + 1:]
        scheme_end = before.index("://") + 3
        creds = before[scheme_end:]
        colon = creds.index(":")
        user_db, password = creds[:colon], creds[colon + 1:]
        host_db = after
        host_port, dbname = (host_db.rsplit("/", 1) if "/" in host_db else (host_db, "praesidium_hjmm"))
        host = host_port.split(":")[0] if ":" in host_port else host_port
        port = int(host_port.split(":")[1]) if ":" in host_port else 5432
        conn = psycopg2.connect(host=host, port=port, user=user_db, password=password, dbname=dbname)
        cur = conn.cursor()
        cur.execute("""
            UPDATE tenant_connectors
            SET config = jsonb_set(
                COALESCE(config, '{}'::jsonb),
                '{email_lookback_days}',
                to_jsonb(%s::text)
            )
            WHERE trim(tenant_id) = trim(%s) AND connector = 'exchange'
        """, (str(lookback_days), tid))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("Config update failed (non-fatal): %s", e)

    try:
        import asyncio
        from jobs.exchange_sync import run as exchange_run
        await asyncio.to_thread(exchange_run, tid.strip())

        async with AsyncSessionLocal() as session:
            r = (await session.execute(text(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE routing_status = :ps) AS pending "
                "FROM email_routing_queue eq WHERE TRIM(eq.tenant_id) = :tid"
            ), {"tid": tid, "ps": "pending"})).mappings().fetchone()

        return JSONResponse({
            "ok": True,
            "total": r["total"] if r else 0,
            "pending": r["pending"] if r else 0,
        })
    except Exception as e:
        logger.exception("Exchange sync failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/auto-route")
async def auto_route_emails(request: Request, user=Depends(get_current_user)):
    """Auto-route pending emails using multi-pass rule-based matching.

    Pass order (first match wins):
        1. eFiling cause number (Case: XX-YY-ZZZZ in subject)
        2. email_routing_rules table (cause_number, sender_email, subject_contains)
        3. Contact email → matter_contacts lookup
        4. Client email match (single-matter clients only)
        5. Subject ↔ matter name keyword matching (3+ char keywords)
    """
    tid = _tid(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    client_filter = body.get("client_id")  # optional: scope to one client
    results = {"efiling": 0, "rules": 0, "contact": 0, "client_email": 0, "subject_match": 0, "unmatched": 0}

    async with AsyncSessionLocal() as session:
        # ── Load pending emails ──
        eq_filters = "TRIM(eq.tenant_id) = :tid AND eq.routing_status = 'pending'"
        eq_params = {"tid": tid}
        if client_filter:
            eq_filters += " AND EXISTS (SELECT 1 FROM matters m2 JOIN clients c2 ON c2.id = m2.client_id WHERE m2.id = eq.matched_matter_id AND c2.id = CAST(:cid AS uuid))"
            eq_params["cid"] = client_filter

        pending = (await session.execute(text(f"""
            SELECT eq.id, eq.subject, eq.from_email, eq.from_display,
                   eq.to_emails, eq.body_preview, eq.received_at,
                   eq.conversation_topic
            FROM email_routing_queue eq
            WHERE {eq_filters}
            ORDER BY received_at DESC
        """), eq_params)).mappings().all()

        if not pending:
            return JSONResponse({"ok": True, "results": results, "message": "No pending emails"})

        # ── Load cause number map ──
        cause_map = {}
        cause_rows = (await session.execute(text("""
            SELECT id, cause_number FROM matters
            WHERE TRIM(tenant_id) = :tid AND cause_number IS NOT NULL
              AND TRIM(CAST(cause_number AS text)) != ''
        """), {"tid": tid})).fetchall()
        for r in cause_rows:
            cause_map[r[1].strip().upper()] = r[0]

        # ── Load email_routing_rules ──
        rules = (await session.execute(text("""
            SELECT rule_type, LOWER(rule_value) as rule_value, matter_id
            FROM email_routing_rules
            WHERE TRIM(tenant_id) = :tid AND is_active = true
            ORDER BY priority ASC NULLS LAST
        """), {"tid": tid})).fetchall()

        rules_cause = {}
        rules_sender = {}
        rules_subject = []
        for r in rules:
            if r[0] == 'cause_number':
                rules_cause[r[1].strip().upper()] = r[2]
            elif r[0] == 'sender_email':
                rules_sender[r[1].strip()] = r[2]
            elif r[0] == 'subject_contains':
                rules_subject.append((r[1].strip(), r[2]))

        # ── Load contact email map ──
        contact_email_map = {}
        ct_rows = (await session.execute(text("""
            SELECT LOWER(co.email) as email, mc.matter_id
            FROM contacts co
            JOIN matter_contacts mc ON mc.contact_id = co.id AND TRIM(mc.tenant_id) = :tid
            WHERE TRIM(co.tenant_id) = :tid AND co.email IS NOT NULL
              AND TRIM(CAST(co.email AS text)) != ''
        """), {"tid": tid})).fetchall()
        for r in ct_rows:
            contact_email_map.setdefault(r[0].strip(), []).append(r[1])

        # ── Load client email map ──
        client_email_map = {}
        ce_rows = (await session.execute(text("""
            SELECT LOWER(c.email) as email, m.id AS matter_id
            FROM clients c JOIN matters m ON m.client_id = c.id AND TRIM(m.tenant_id) = :tid
            WHERE TRIM(c.tenant_id) = :tid AND c.email IS NOT NULL
              AND TRIM(CAST(c.email AS text)) != ''
              AND (m.status IS NULL OR m.status = 'active')
        """), {"tid": tid})).fetchall()
        for r in ce_rows:
            client_email_map.setdefault(r[0].strip(), []).append(r[1])

        # ── Load matter names for subject matching ──
        # Only matters with distinctive names (3+ chars, not pure numbers)
        matter_keywords = []  # list of (keyword_lower, matter_id, matter_name)
        mkw_rows = (await session.execute(text("""
            SELECT m.id, m.matter_name FROM matters m
            WHERE TRIM(m.tenant_id) = :tid AND m.matter_name IS NOT NULL
              AND (m.status IS NULL OR m.status = 'active')
              AND LENGTH(TRIM(m.matter_name)) >= 3
        """), {"tid": tid})).fetchall()

        import re as _re
        # Build keyword index from matter names
        # Skip generic words that would false-positive
        STOP_WORDS = {'the','and','for','llc','inc','corp','land','sale','purchase',
                       'contract','agreement','road','street','ave','blvd','highway',
                       'general','new','old','north','south','east','west','city',
                       'county','state','texas','dallas','partners','properties'}
        for r in mkw_rows:
            name = r[1].strip()
            # Use the full matter name as a keyword if it's 4+ chars
            if len(name) >= 4:
                matter_keywords.append((name.lower(), r[0], name))
            # Also extract individual words that are 4+ chars and not stop words
            words = _re.findall(r'[A-Za-z]{4,}', name)
            for w in words:
                wl = w.lower()
                if wl not in STOP_WORDS and len(wl) >= 4:
                    matter_keywords.append((wl, r[0], name))

        # Sort by keyword length descending (prefer longer/more specific matches)
        matter_keywords.sort(key=lambda x: -len(x[0]))

        # ── Process each email ──
        for email in pending:
            eid = email["id"]
            subj = (email["subject"] or "").strip()
            subj_lower = subj.lower()
            from_addr = (email["from_email"] or "").strip().lower()
            conv_topic = (email.get("conversation_topic") or "").strip().lower()
            matched_matter_id = None
            match_method = None
            confidence = 0.0
            signals = {}

            # ── Pass 1: eFiling cause number ──
            efiling_match = _re.search(r'Case:\s*([A-Z]{2,4}-\d{2,4}-\d{4,8})', subj, _re.IGNORECASE)
            if efiling_match:
                cause_num = efiling_match.group(1).upper()
                if cause_num in cause_map:
                    matched_matter_id = cause_map[cause_num]
                    match_method = "efiling_cause_number"
                    confidence = 1.0
                    signals = {"cause_number": cause_num, "source": "tyler_efiling"}
                    results["efiling"] += 1

            # ── Pass 2: email_routing_rules ──
            if not matched_matter_id:
                # 2a: cause_number rules (check subject for cause patterns)
                for pattern, mid in rules_cause.items():
                    if pattern.lower() in subj_lower:
                        matched_matter_id = mid
                        match_method = "routing_rule_cause"
                        confidence = 0.95
                        signals = {"rule_type": "cause_number", "rule_value": pattern}
                        results["rules"] += 1
                        break

                # 2b: sender_email rules
                if not matched_matter_id and from_addr in rules_sender:
                    matched_matter_id = rules_sender[from_addr]
                    match_method = "routing_rule_sender"
                    confidence = 0.90
                    signals = {"rule_type": "sender_email", "rule_value": from_addr}
                    results["rules"] += 1

                # 2c: subject_contains rules
                if not matched_matter_id:
                    for rule_val, mid in rules_subject:
                        if rule_val in subj_lower or (conv_topic and rule_val in conv_topic):
                            matched_matter_id = mid
                            match_method = "routing_rule_subject"
                            confidence = 0.90
                            signals = {"rule_type": "subject_contains", "rule_value": rule_val}
                            results["rules"] += 1
                            break

            # ── Pass 3: Contact email match ──
            if not matched_matter_id and from_addr in contact_email_map:
                matter_ids = contact_email_map[from_addr]
                if len(matter_ids) == 1:
                    matched_matter_id = matter_ids[0]
                    match_method = "contact_email"
                    confidence = 0.85
                    signals = {"from_email": from_addr, "contact_matters": 1}
                    results["contact"] += 1
                else:
                    signals = {"from_email": from_addr, "contact_matters": len(matter_ids), "ambiguous": True}

            # ── Pass 4: Client email match ──
            if not matched_matter_id and from_addr in client_email_map:
                matter_ids = client_email_map[from_addr]
                if len(matter_ids) == 1:
                    matched_matter_id = matter_ids[0]
                    match_method = "client_email"
                    confidence = 0.75
                    signals = {"from_email": from_addr, "client_matters": 1}
                    results["client_email"] += 1

            # ── Pass 5: Subject ↔ matter name keyword matching ──
            if not matched_matter_id:
                search_text = subj_lower + " " + conv_topic
                best_match = None
                best_len = 0
                for kw, mid, mname in matter_keywords:
                    if len(kw) > best_len and kw in search_text:
                        best_match = (mid, mname, kw)
                        best_len = len(kw)
                if best_match and best_len >= 4:
                    matched_matter_id = best_match[0]
                    match_method = "subject_matter_name"
                    confidence = 0.70 if best_len >= 6 else 0.60
                    signals = {"matched_keyword": best_match[2], "matter_name": best_match[1], "keyword_len": best_len}
                    results["subject_match"] += 1

            if not matched_matter_id:
                results["unmatched"] += 1

            new_status = "matched" if matched_matter_id else "pending"
            await session.execute(text("""
                UPDATE email_routing_queue SET
                    routing_status = :status,
                    matched_matter_id = CAST(:mid AS uuid),
                    match_confidence = :conf,
                    match_signals = CAST(:signals AS jsonb),
                    routed_by = :method,
                    routed_at = CASE WHEN :mid IS NOT NULL THEN NOW() ELSE NULL END
                WHERE id = :eid AND TRIM(tenant_id) = :tid
            """), {
                "status": new_status,
                "mid": str(matched_matter_id) if matched_matter_id else None,
                "conf": confidence,
                "signals": json.dumps(signals),
                "method": match_method,
                "eid": eid,
                "tid": tid,
            })

        await session.commit()

    total_matched = sum(v for k, v in results.items() if k != "unmatched")
    return JSONResponse({
        "ok": True, "results": results,
        "message": f"{total_matched} matched, {results['unmatched']} unmatched",
    })


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE EMAIL (manual match / skip / file)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/route-email")
async def route_single_email(request: Request, user=Depends(get_current_user)):
    """Manually route a single email to a matter.

    Actions:
        match — assign to matter (DB only, no disk filing)
        skip  — mark as skipped
        file  — match AND export to matter's Email folder on disk
    """
    tid = _tid(request)
    body = await request.json()
    email_id = body.get("email_id")
    matter_id = body.get("matter_id")
    action = body.get("action", "match")

    if not email_id:
        return JSONResponse({"error": "email_id required"}, status_code=400)

    if action == "file":
        # Delegate to the file-email endpoint
        return await file_email_to_matter(request, user, override_body=body)

    async with AsyncSessionLocal() as session:
        if action == "skip":
            await session.execute(text("""
                UPDATE email_routing_queue SET
                    routing_status = 'skipped', routed_by = 'manual', routed_at = NOW()
                WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"eid": email_id, "tid": tid})
        elif matter_id:
            await session.execute(text("""
                UPDATE email_routing_queue SET
                    routing_status = 'matched',
                    matched_matter_id = CAST(:mid AS uuid),
                    match_confidence = 1.0,
                    routed_by = 'manual',
                    routed_at = NOW()
                WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"eid": email_id, "mid": matter_id, "tid": tid})
        await session.commit()

    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# FILE EMAIL TO MATTER'S EMAIL FOLDER
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/file-email")
async def file_email_to_matter(request: Request, user=Depends(get_current_user),
                                override_body: dict = None):
    """
    Export email from Exchange to the matter's Email folder on disk.

    Body:
        email_id:   UUID of email_routing_queue row
        matter_id:  UUID of target matter (uses matched_matter_id if omitted)

    What it does:
        1. Resolves the matter's Email folder on disk (15-Email / 09-Email)
        2. Exports full MIME from Exchange via EWS
        3. Writes .eml with human-readable name (YYYY-MM-DD - Sender - Subject.eml)
        4. Creates dms_documents + documents rows
        5. Updates email_routing_queue filing columns
        6. Auto-populates matter_contacts for the sender
    """
    tid = _tid(request)
    if override_body:
        body = override_body
    else:
        body = await request.json()
    email_id = body.get("email_id")
    matter_id = body.get("matter_id")

    if not email_id:
        return JSONResponse({"error": "email_id required"}, status_code=400)

    async with AsyncSessionLocal() as session:
        # 1. Get email record
        eq = (await session.execute(text("""
            SELECT id, message_id, subject, from_email, from_display,
                   to_emails, cc_emails, received_at, body_preview, body_text,
                   has_attachments, attachment_names, matched_matter_id::text,
                   routing_status, filing_status, attorney_user_id
            FROM email_routing_queue
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})).mappings().fetchone()

        if not eq:
            return JSONResponse({"error": "Email not found"}, status_code=404)
        if eq["filing_status"] == "filed":
            return JSONResponse({"error": "Already filed"}, status_code=409)

        target_matter_id = matter_id or eq["matched_matter_id"]
        if not target_matter_id:
            return JSONResponse({"error": "No matter_id — match the email first or provide matter_id"}, status_code=400)

        # 2. Resolve email folder
        email_folder = await _resolve_email_folder(session, tid, target_matter_id)
        if not email_folder:
            return JSONResponse({
                "error": "No Praesidium folder for this matter. Sync or create folders first."
            }, status_code=400)

        # 3. Export from Exchange
        mime_bytes = await _export_email_from_exchange(
            session, tid, eq["message_id"], eq.get("attorney_user_id")
        )

        if mime_bytes is None:
            # Fallback: build .eml from DB metadata
            logger.warning("EWS export failed for %s — creating .eml from DB metadata", email_id)
            import email as email_lib
            from email.mime.text import MIMEText
            body_content = eq["body_text"] or eq["body_preview"] or ""
            fallback_msg = MIMEText(body_content, "plain", "utf-8")
            fallback_msg["Subject"] = eq["subject"] or "(No Subject)"
            fallback_msg["From"] = f"{eq['from_display'] or ''} <{eq['from_email'] or ''}>"
            to_list = eq["to_emails"]
            if isinstance(to_list, str):
                to_list = json.loads(to_list)
            fallback_msg["To"] = ", ".join(to_list) if to_list else ""
            if eq["received_at"]:
                fallback_msg["Date"] = str(eq["received_at"])
            mime_bytes = fallback_msg.as_bytes()

        # 4. Build filename
        recv_date = eq["received_at"]
        date_str = recv_date.strftime("%Y-%m-%d") if recv_date else "unknown-date"
        sender_name = _safe_filename(eq["from_display"] or eq["from_email"] or "unknown", 40)
        subject_clean = _safe_filename(eq["subject"] or "No Subject", 60)
        eml_filename = f"{date_str} - {sender_name} - {subject_clean}.eml"

        # Deduplicate
        eml_path = os.path.join(email_folder, eml_filename)
        counter = 1
        while os.path.exists(eml_path):
            eml_path = os.path.join(email_folder,
                f"{date_str} - {sender_name} - {subject_clean} ({counter}).eml")
            counter += 1

        # 5. Write to disk
        try:
            with open(eml_path, "wb") as f:
                f.write(mime_bytes)
        except Exception as exc:
            logger.exception("Failed to write email to %s: %s", eml_path, exc)
            return JSONResponse({"error": f"Disk write failed: {exc}"}, status_code=500)

        file_size = len(mime_bytes)
        file_hash = hashlib.sha256(mime_bytes).hexdigest()
        body_text = eq["body_text"] or eq["body_preview"] or ""

        # 6. Create dms_documents row
        await session.execute(text("""
            INSERT INTO dms_documents (
                tenant_id, file_path, folder_root, file_hash,
                file_size_bytes, modified_at, content_text,
                source, extraction_status, indexed_at
            ) VALUES (
                :tid, :fp, :fr, :fh, :fs, NOW(), :ct,
                'email_filing', 'complete', NOW()
            )
            ON CONFLICT (file_path) DO UPDATE SET
                file_hash = EXCLUDED.file_hash,
                file_size_bytes = EXCLUDED.file_size_bytes,
                content_text = EXCLUDED.content_text,
                updated_at = NOW()
        """), {
            "tid": tid, "fp": eml_path, "fr": email_folder,
            "fh": file_hash, "fs": file_size, "ct": body_text[:50000],
        })

        # 7. Create documents row
        await session.execute(text("""
            INSERT INTO documents (
                tenant_id, matter_id, filename, original_filename,
                mime_type, file_size, storage_path, document_type,
                status, extracted_text, title, doc_type,
                file_name, created_at, updated_at
            ) VALUES (
                :tid, CAST(:mid AS uuid), :fn, :fn,
                'message/rfc822', :fs, :sp, 'email',
                'filed', :et, :title, 'email',
                :fn, NOW(), NOW()
            )
        """), {
            "tid": tid, "mid": target_matter_id,
            "fn": os.path.basename(eml_path), "fs": file_size,
            "sp": eml_path, "et": body_text[:50000],
            "title": eq["subject"] or "(No Subject)",
        })

        # 8. Update email_routing_queue
        current_user_id = getattr(user, 'id', None) or getattr(user, 'user_id', None)
        await session.execute(text("""
            UPDATE email_routing_queue SET
                filing_status = 'filed',
                filed_to_dms = true,
                filed_matter_id = CAST(:mid AS uuid),
                filed_at = NOW(),
                filed_by = :uid,
                staging_path = :sp,
                routing_status = CASE
                    WHEN routing_status = 'pending' THEN 'matched'
                    ELSE routing_status
                END,
                matched_matter_id = COALESCE(matched_matter_id, CAST(:mid AS uuid)),
                match_confidence = CASE
                    WHEN matched_matter_id IS NULL THEN 1.0
                    ELSE match_confidence
                END,
                routed_by = CASE
                    WHEN routed_by IS NULL THEN 'manual_file'
                    ELSE routed_by
                END,
                routed_at = COALESCE(routed_at, NOW())
            WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
        """), {
            "mid": target_matter_id, "uid": current_user_id,
            "sp": eml_path, "eid": email_id, "tid": tid,
        })

        # 9. Auto-populate matter_contacts
        from_email_addr = (eq["from_email"] or "").strip().lower()
        if from_email_addr and not from_email_addr.endswith("@hjmmlegal.com"):
            existing = (await session.execute(text("""
                SELECT 1 FROM contacts co
                JOIN matter_contacts mc ON mc.contact_id = co.id
                WHERE TRIM(co.tenant_id) = :tid
                  AND LOWER(co.email) = :email
                  AND mc.matter_id = CAST(:mid AS uuid)
                LIMIT 1
            """), {"tid": tid, "email": from_email_addr, "mid": target_matter_id})).fetchone()

            if not existing:
                contact = (await session.execute(text("""
                    SELECT id FROM contacts
                    WHERE TRIM(tenant_id) = :tid AND LOWER(email) = :email LIMIT 1
                """), {"tid": tid, "email": from_email_addr})).fetchone()

                if not contact:
                    display = eq["from_display"] or from_email_addr.split("@")[0]
                    await session.execute(text("""
                        INSERT INTO contacts (tenant_id, full_name, email, source, created_at)
                        VALUES (:tid, :name, :email, 'email_filing', NOW())
                    """), {"tid": tid, "name": display, "email": from_email_addr})
                    contact = (await session.execute(text("""
                        SELECT id FROM contacts
                        WHERE TRIM(tenant_id) = :tid AND LOWER(email) = :email
                        ORDER BY created_at DESC LIMIT 1
                    """), {"tid": tid, "email": from_email_addr})).fetchone()

                if contact:
                    await session.execute(text("""
                        INSERT INTO matter_contacts (tenant_id, matter_id, contact_id, role, created_at)
                        VALUES (:tid, CAST(:mid AS uuid), :cid, 'correspondent', NOW())
                        ON CONFLICT DO NOTHING
                    """), {"tid": tid, "mid": target_matter_id, "cid": contact[0]})

        # 10. Learning loop: auto-create routing rules from this filing
        if from_email_addr and not from_email_addr.endswith("@hjmmlegal.com"):
            # Create sender_email rule if sender is external and not already a rule
            existing_rule = (await session.execute(text("""
                SELECT 1 FROM email_routing_rules
                WHERE TRIM(tenant_id) = :tid AND rule_type = 'sender_email'
                  AND LOWER(rule_value) = :val AND matter_id = CAST(:mid AS uuid)
                LIMIT 1
            """), {"tid": tid, "val": from_email_addr, "mid": target_matter_id})).fetchone()
            if not existing_rule:
                await session.execute(text("""
                    INSERT INTO email_routing_rules (tenant_id, rule_type, rule_value, matter_id, priority, is_active, created_by, created_at)
                    VALUES (:tid, 'sender_email', :val, CAST(:mid AS uuid), 50, true, :uid, NOW())
                """), {"tid": tid, "val": from_email_addr, "mid": target_matter_id, "uid": current_user_id})

        await session.commit()

    return JSONResponse({
        "ok": True,
        "filed_path": eml_path,
        "file_size": file_size,
        "matter_id": target_matter_id,
    })


# ─────────────────────────────────────────────────────────────────────────────
# BATCH FILE — file all matched emails to their matters
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/file-matched")
async def file_all_matched(request: Request, user=Depends(get_current_user)):
    """
    Batch-file all matched (but not yet filed) emails to their matters.
    Iterates through matched emails and calls the filing logic for each.
    """
    tid = _tid(request)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id::text, matched_matter_id::text
            FROM email_routing_queue
            WHERE TRIM(tenant_id) = :tid
              AND routing_status = 'matched'
              AND (filing_status IS NULL OR filing_status = 'pending')
              AND matched_matter_id IS NOT NULL
            ORDER BY received_at
            LIMIT 200
        """), {"tid": tid})).fetchall()

    filed = 0
    errors = 0
    for row in rows:
        try:
            # Simulate request body for the file endpoint
            class FakeRequest:
                state = request.state
                async def json(self):
                    return {"email_id": row[0], "matter_id": row[1]}
            resp = await file_email_to_matter(FakeRequest(), user)
            data = json.loads(resp.body)
            if data.get("ok"):
                filed += 1
            else:
                errors += 1
                logger.warning("Batch file error for %s: %s", row[0], data.get("error"))
        except Exception as exc:
            errors += 1
            logger.warning("Batch file exception for %s: %s", row[0], exc)

    return JSONResponse({
        "ok": True,
        "filed": filed,
        "errors": errors,
        "total": len(rows),
    })


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL DETAIL
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/detail/{email_id}")
async def email_detail(request: Request, email_id: str, user=Depends(get_current_user)):
    """Full email detail for popout viewer."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = (await session.execute(text("""
            SELECT eq.id::text, eq.subject, eq.from_email, eq.from_display,
                   eq.to_emails, eq.cc_emails, eq.bcc_emails,
                   eq.received_at, eq.body_preview, eq.body_text,
                   eq.has_attachments, eq.attachment_names, eq.attachment_count,
                   eq.routing_status, eq.matched_matter_id::text,
                   eq.match_confidence, eq.match_signals, eq.routed_by, eq.routed_at,
                   eq.importance, eq.sensitivity, eq.categories, eq.is_read,
                   eq.conversation_topic, eq.in_reply_to, eq.internet_message_id,
                   eq.filed_matter_id::text, eq.filed_at, eq.filing_status,
                   eq.filed_to_dms, eq.staging_path,
                   m.matter_name, c.client_name, c.id::text AS client_id
            FROM email_routing_queue eq
            LEFT JOIN matters m ON m.id = eq.matched_matter_id
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE eq.id = CAST(:eid AS uuid) AND TRIM(eq.tenant_id) = :tid
        """), {"eid": email_id, "tid": tid})).mappings().fetchone()
    if not r:
        return JSONResponse({"error": "Not found"}, status_code=404)
    from decimal import Decimal as _D
    def ser(v):
        if isinstance(v, _D): return float(v)
        if hasattr(v, 'hex') and not isinstance(v, (str, bytes)): return str(v)
        if hasattr(v, 'isoformat'): return v.isoformat()
        return v
    return JSONResponse({k: ser(v) for k, v in dict(r).items()})


# ─────────────────────────────────────────────────────────────────────────────
# AI MATCH
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ai-match")
async def ai_match_emails(request: Request, user=Depends(get_current_user)):
    """AI-powered email-to-matter matching using Claude."""
    tid = _tid(request)
    body = await request.json()
    batch_size = min(body.get("batch_size", 50), 50)

    # Get API key
    api_key = None
    async with AsyncSessionLocal() as session:
        key_row = (await session.execute(text(
            "SELECT encrypted_key FROM credentials_vault "
            "WHERE TRIM(tenant_id) = :tid AND provider = :prov AND key_type = :kt"
        ), {"tid": tid, "prov": "anthropic", "kt": "api_key"})).fetchone()
    if not key_row:
        return JSONResponse({"ok": False, "error": "No Anthropic API key configured"}, status_code=400)

    import base64
    from cryptography.fernet import Fernet
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)
    api_key = f.decrypt(key_row[0].encode()).decode()

    async with AsyncSessionLocal() as session:
        pending = (await session.execute(text("""
            SELECT eq.id::text, eq.subject, eq.from_email, eq.from_display,
                   eq.to_emails, eq.cc_emails, eq.body_preview, eq.received_at,
                   eq.has_attachments, eq.attachment_names
            FROM email_routing_queue eq
            WHERE TRIM(eq.tenant_id) = :tid AND eq.routing_status = :ps
            ORDER BY eq.received_at DESC LIMIT :lim
        """), {"tid": tid, "ps": "pending", "lim": batch_size})).mappings().all()

        if not pending:
            return JSONResponse({"ok": True, "matched": 0, "message": "No pending emails"})

        matters = (await session.execute(text("""
            SELECT m.id::text AS matter_id, m.matter_name, m.matter_number,
                   m.matter_type, m.cause_number, c.client_name, c.email AS client_email
            FROM matters m JOIN clients c ON c.id = m.client_id
            WHERE TRIM(m.tenant_id) = :tid AND (m.status IS NULL OR m.status = :st)
            ORDER BY c.client_name, m.matter_name
        """), {"tid": tid, "st": "active"})).mappings().all()

        contacts = (await session.execute(text("""
            SELECT co.full_name, co.email, co.company, co.firm_name, mc.matter_id::text
            FROM contacts co
            JOIN matter_contacts mc ON mc.contact_id = co.id AND TRIM(mc.tenant_id) = :tid
            WHERE TRIM(co.tenant_id) = :tid AND co.email IS NOT NULL AND TRIM(CAST(co.email AS text)) != ''
        """), {"tid": tid})).mappings().all()

    matter_lines = []
    for i, m in enumerate(matters):
        line = str(i+1) + ". " + (m["client_name"] or "?") + " / " + (m["matter_name"] or "?")
        if m.get("matter_number"): line += " [#" + m["matter_number"] + "]"
        if m.get("cause_number"): line += " (Cause: " + m["cause_number"] + ")"
        if m.get("client_email"): line += " <" + m["client_email"] + ">"
        matter_lines.append(line)

    contact_lines = [
        co["email"] + " -> " + co["full_name"] + " -> matter " + co["matter_id"]
        for co in contacts[:200]
    ]

    email_lines = []
    for i, em in enumerate(pending):
        to_list = em["to_emails"]
        if isinstance(to_list, str): to_list = json.loads(to_list)
        cc_list = em["cc_emails"]
        if isinstance(cc_list, str): cc_list = json.loads(cc_list)
        att_list = em["attachment_names"]
        if isinstance(att_list, str): att_list = json.loads(att_list)
        line = f"EMAIL {i+1}:\n  ID: {em['id']}\n"
        line += f"  Date: {str(em['received_at'])[:16] if em['received_at'] else '?'}\n"
        line += f"  From: {em['from_display'] or ''} <{em['from_email'] or ''}>\n"
        line += f"  To: {', '.join(to_list)}\n"
        if cc_list: line += f"  CC: {', '.join(cc_list)}\n"
        line += f"  Subject: {em['subject'] or '(none)'}\n"
        if em["body_preview"] and len(em["body_preview"].strip()) > 5:
            line += f"  Preview: {em['body_preview'][:200].strip()}\n"
        if att_list: line += f"  Attachments: {', '.join(att_list[:5])}\n"
        email_lines.append(line)

    prompt = """You are matching incoming emails to legal matters for a law firm (HJMM Legal).

IMPORTANT RULES:
- SKIP all emails from @hjmmlegal.com and @thelomm.com addresses — these are internal firm emails.
- SKIP all marketing, newsletters, personal subscriptions.
- Cross-reference time entries for lower-confidence matches.

ACTIVE MATTERS:
""" + "\n".join(matter_lines[:300]) + """

KNOWN CONTACTS:
""" + "\n".join(contact_lines[:100]) + """

EMAILS TO MATCH:
""" + "\n".join(email_lines) + """

For each email, determine which matter it belongs to. Use cause numbers, client/matter names, sender emails, attachment names, body previews.

Mark as "skip" if marketing/newsletters/personal/internal.

Respond with ONLY a JSON array:
[{"email_id": "uuid", "matter_index": N or null, "confidence": 0.0-1.0, "reasoning": "brief", "skip": true/false}]
JSON array:"""

    try:
        import httpx
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            return JSONResponse({"ok": False, "error": f"Claude API error: {resp.status_code}"}, status_code=500)
        response_text = resp.json()["content"][0]["text"].strip()
        if "```" in response_text:
            response_text = response_text.split("```")[1].replace("json", "").strip()
        classifications = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("AI match JSON parse error: %s", e)
        return JSONResponse({"ok": False, "error": "AI response parsing failed"}, status_code=500)
    except Exception as e:
        logger.exception("AI match error: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    matched = 0
    skipped = 0
    async with AsyncSessionLocal() as session:
        for cls in classifications:
            eid = cls.get("email_id", "")
            matter_idx = cls.get("matter_index")
            confidence = cls.get("confidence", 0)
            reasoning = cls.get("reasoning", "")
            should_skip = cls.get("skip", False)

            if should_skip:
                await session.execute(text("""
                    UPDATE email_routing_queue SET
                        routing_status = :st, routed_by = :rb, routed_at = NOW(),
                        match_confidence = :conf,
                        match_signals = CAST(:sig AS jsonb)
                    WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
                """), {
                    "st": "skipped", "rb": "ai_match", "conf": confidence,
                    "sig": json.dumps({"reasoning": reasoning, "ai_skip": True}),
                    "eid": eid, "tid": tid,
                })
                skipped += 1
            elif matter_idx and 1 <= matter_idx <= len(matters):
                matter = matters[matter_idx - 1]
                await session.execute(text("""
                    UPDATE email_routing_queue SET
                        routing_status = :st,
                        matched_matter_id = CAST(:mid AS uuid),
                        match_confidence = :conf,
                        match_signals = CAST(:sig AS jsonb),
                        routed_by = :rb, routed_at = NOW()
                    WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid
                """), {
                    "st": "matched", "mid": matter["matter_id"], "conf": confidence,
                    "sig": json.dumps({"reasoning": reasoning, "ai_matter_index": matter_idx,
                                       "matched_matter_name": matter["matter_name"]}),
                    "rb": "ai_match", "eid": eid, "tid": tid,
                })
                matched += 1
        await session.commit()

    return JSONResponse({
        "ok": True, "matched": matched, "skipped": skipped,
        "total_processed": len(classifications),
        "message": f"{matched} matched, {skipped} skipped, {len(pending) - matched - skipped} unresolved",
    })


# ─────────────────────────────────────────────────────────────────────────────
# MATTER SEARCH (for manual routing picker)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/search-matters")
async def search_matters_for_email(request: Request, q: str = "", user=Depends(get_current_user)):
    """Matter search for manual email routing."""
    tid = _tid(request)
    if not q or len(q) < 2:
        return JSONResponse([])
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT m.id::text, m.matter_name, m.matter_number, c.client_name
            FROM matters m LEFT JOIN clients c ON c.id = m.client_id
            WHERE TRIM(m.tenant_id) = :tid
              AND (m.matter_name ILIKE :q OR c.client_name ILIKE :q OR m.matter_number ILIKE :q)
            ORDER BY c.client_name, m.matter_name LIMIT 15
        """), {"tid": tid, "q": "%" + q + "%"})).mappings().all()
    def ser(v):
        if hasattr(v, 'hex'): return str(v)
        return v
    return JSONResponse([{k: ser(v) for k, v in dict(r).items()} for r in rows])
