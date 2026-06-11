#!/usr/bin/env python3
"""
jobs/discover_exchange_mailboxes.py (v3 — LDAP-based)

Exchange entity discovery job. Uses LDAP to query Active Directory for
all objects with msExchMailboxGuid (definitive list of Exchange mailboxes),
filters out system/health mailboxes, classifies by msExchRecipientTypeDetails,
writes to connector_entity_map, and auto-matches to Praesidium users.

Why LDAP instead of EWS ResolveNames:
  - ResolveNames fails with ErrorNonExistentMailbox on on-prem Exchange
    when called via impersonation context
  - LDAP query against AD is reliable, fast, and returns the full GAL
    including mailbox type classification

Called by: entities_router POST /tenant-admin/connectors/exchange/entities/discover
RQ queue: default
"""
import logging
import os
import base64
import ssl

import psycopg2

logger = logging.getLogger(__name__)

RECIPIENT_TYPE_MAP = {
    1:               "mailbox",
    4:               "shared_mailbox",
    16:              "calendar",
    32:              "calendar",
    8388608:         "system",
    536870912:       "system",
    549755813888:    "system",
    4398046511104:   "system",
}

SKIP_TYPES = {"system"}

SKIP_PATTERNS = [
    "healthmailbox", "systemmailbox", "discoverysearchmailbox",
    "federatedemail", "migration.", "sm_", "e4e encryption",
]


def _get_db_conn():
    db_url = os.environ.get("DATABASE_URL", "")
    return psycopg2.connect(db_url.replace("postgresql+asyncpg://", "postgresql://"))


def _decrypt_vault_value(val):
    if not val or not val.startswith("gAAAAA"):
        return val
    try:
        from cryptography.fernet import Fernet
        secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
        key_bytes = (secret[:32]).encode().ljust(32, b"0")
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        return Fernet(fernet_key).decrypt(val.encode()).decode()
    except Exception as e:
        logger.warning("Decrypt failed: %s", e)
        return val


def _get_ldap_credentials(conn, tid):
    cur = conn.cursor()
    cur.execute("SELECT key_type, encrypted_key FROM credentials_vault WHERE trim(tenant_id)=trim(%s) AND provider='auth_ldap'", (tid,))
    rows = cur.fetchall(); cur.close()
    return {r[0]: _decrypt_vault_value(r[1]) for r in rows}


def _get_ldap_config(conn, tid):
    cur = conn.cursor()
    cur.execute("SELECT config FROM tenant_connectors WHERE trim(tenant_id)=trim(%s) AND connector='auth_ldap' LIMIT 1", (tid,))
    row = cur.fetchone(); cur.close()
    return row[0] if row else {}


def _classify(rtype, name, email):
    if rtype is not None:
        try:
            c = RECIPIENT_TYPE_MAP.get(int(rtype))
            if c: return c
        except (ValueError, TypeError): pass
    nl = (name or "").lower(); el = (email or "").lower()
    for p in SKIP_PATTERNS:
        if p in nl or p in el: return "system"
    for kw in ["conference","room","resource","board","training"]:
        if kw in nl: return "calendar"
    for kw in ["info@","support@","billing@","office@"]:
        if kw in el: return "shared_mailbox"
    return "mailbox"


def _should_skip(name, email, sam):
    combined = ((name or "")+(email or "")+(sam or "")).lower()
    return any(p in combined for p in SKIP_PATTERNS)


def _log_sync(conn, tid, discovered, new, auto_matched, error=None):
    cur = conn.cursor()
    cur.execute("INSERT INTO connector_entity_sync_log (tenant_id,connector_type,started_at,completed_at,discovered_count,new_count,auto_matched_count,error) VALUES (%s,'exchange',NOW(),NOW(),%s,%s,%s,%s)", (tid, discovered, new, auto_matched, error))
    conn.commit(); cur.close()


def run(tenant_id, triggered_by=None):
    logger.info("[discover_exchange] starting tenant=%s (LDAP-based)", tenant_id)
    conn = None
    try:
        conn = _get_db_conn()
        ldap_creds = _get_ldap_credentials(conn, tenant_id)
        ldap_config = _get_ldap_config(conn, tenant_id)

        if not ldap_creds.get("bind_dn") or not ldap_creds.get("bind_password"):
            raise ValueError("LDAP credentials incomplete")

        ldap_url = ldap_config.get("ldap_url", "ldaps://10.10.10.1:636")
        base_dn = ldap_config.get("base_dn", "DC=hjmmlegal,DC=com")

        import ldap3
        tls = ldap3.Tls(validate=ssl.CERT_NONE)
        server = ldap3.Server(ldap_url, use_ssl=True, tls=tls, get_info=ldap3.ALL)
        lc = ldap3.Connection(server, user=ldap_creds["bind_dn"], password=ldap_creds["bind_password"], auto_bind=True, raise_exceptions=True)

        lc.search(
            search_base=base_dn,
            search_filter="(&(objectClass=user)(msExchMailboxGuid=*))",
            search_scope=ldap3.SUBTREE,
            attributes=["sAMAccountName","mail","displayName","msExchRecipientTypeDetails","userAccountControl"],
        )
        all_entries = lc.entries
        logger.info("[discover_exchange] LDAP returned %d Exchange objects", len(all_entries))
        lc.unbind()

        mailboxes = []
        for entry in all_entries:
            name = entry.displayName.value or "" if hasattr(entry,"displayName") else ""
            email = entry.mail.value or "" if hasattr(entry,"mail") else ""
            sam = entry.sAMAccountName.value or "" if hasattr(entry,"sAMAccountName") else ""
            rtype = entry.msExchRecipientTypeDetails.value if hasattr(entry,"msExchRecipientTypeDetails") else None
            if _should_skip(name, email, sam): continue
            mb_type = _classify(rtype, name, email)
            if mb_type in SKIP_TYPES: continue
            if not email: continue
            uac = int(entry.userAccountControl.value) if hasattr(entry,"userAccountControl") and entry.userAccountControl.value else 0
            mailboxes.append({"display_name":name,"email":email,"sam":sam,"mb_type":mb_type,"is_disabled":bool(uac&0x0002)})

        logger.info("[discover_exchange] %d real mailboxes after filtering", len(mailboxes))

        # Load users for auto-match
        cur = conn.cursor()
        cur.execute("SELECT id, email, full_name FROM users WHERE trim(tenant_id)=trim(%s) AND is_active=true", (tenant_id,))
        prs_users = {r[1].lower(): (r[0], r[2]) for r in cur.fetchall() if r[1]}
        cur.close()

        new_count = 0; auto_matched = 0
        cur = conn.cursor()
        for mb in mailboxes:
            eid = mb["email"].lower()
            cur.execute("SELECT id FROM connector_entity_map WHERE trim(tenant_id)=trim(%s) AND connector_type='exchange' AND entity_id=%s", (tenant_id, eid))
            existing = cur.fetchone()
            uid = prs_users.get(eid, (None,))[0]

            if existing:
                cur.execute("""UPDATE connector_entity_map SET entity_display=%s, entity_email=%s, entity_type=%s, is_active=%s,
                    mapped_user_id=COALESCE(mapped_user_id,%s), auto_matched=CASE WHEN mapped_user_id IS NULL AND %s IS NOT NULL THEN true ELSE auto_matched END,
                    mapped_at=CASE WHEN mapped_user_id IS NULL AND %s IS NOT NULL THEN NOW() ELSE mapped_at END, discovered_at=NOW()
                    WHERE trim(tenant_id)=trim(%s) AND connector_type='exchange' AND entity_id=%s""",
                    (mb["display_name"], mb["email"], mb["mb_type"], not mb["is_disabled"], uid, uid, uid, tenant_id, eid))
            else:
                cur.execute("""INSERT INTO connector_entity_map (tenant_id,connector_type,entity_id,entity_display,entity_email,entity_type,
                    mapped_user_id,auto_matched,is_active,discovered_at,mapped_at)
                    VALUES (%s,'exchange',%s,%s,%s,%s,%s,%s,%s,NOW(),CASE WHEN %s IS NOT NULL THEN NOW() ELSE NULL END)""",
                    (tenant_id, eid, mb["display_name"], mb["email"], mb["mb_type"], uid, uid is not None, not mb["is_disabled"], uid))
                new_count += 1
            if uid: auto_matched += 1

        conn.commit(); cur.close()
        _log_sync(conn, tenant_id, len(mailboxes), new_count, auto_matched)
        logger.info("[discover_exchange] done — %d discovered, %d new, %d auto-matched", len(mailboxes), new_count, auto_matched)

    except Exception as exc:
        logger.exception("[discover_exchange] failed: %s", exc)
        if conn:
            try: _log_sync(conn, tenant_id, 0, 0, 0, str(exc)[:500])
            except: pass
    finally:
        if conn: conn.close()
