"""
jobs/exchange_sync.py

Exchange EWS sync job.
Runs on PROC-01 via RQ. Reads credentials from credentials_vault.
Reads mailbox mappings from connector_entity_map.

Per mapped mailbox:
  - sync_email=true    → pulls messages → email_routing_queue
  - sync_calendar=true → pulls calendar events → exchange_calendar_events

Shared mailboxes (entity_type='shared_mailbox'):
  - email only, no user attribution

Calendar resources (entity_type='calendar'):
  - calendar events only, tagged as resource calendar

Lookback/lookahead from tenant_connectors.config.
Uses service account impersonation — one account accesses all mailboxes.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_db_conn():
    db_url = os.environ.get("DATABASE_URL", "")
    idx = db_url.rfind("@")
    before = db_url[:idx]
    after  = db_url[idx + 1:]
    scheme_end = before.index("://") + 3
    creds = before[scheme_end:]
    colon = creds.index(":")
    user, password = creds[:colon], creds[colon + 1:]
    host_db = after
    host_port, dbname = (host_db.rsplit("/", 1) if "/" in host_db
                         else (host_db, "praesidium_hjmm"))
    if ":" in host_port:
        host, port = host_port.split(":", 1)
        port = int(port)
    else:
        host, port = host_port, 5432
    return psycopg2.connect(
        host=host, port=port, user=user, password=password,
        dbname=dbname, cursor_factory=psycopg2.extras.RealDictCursor
    )


def _get_credentials(conn, tenant_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("""
        SELECT key_type, encrypted_key FROM credentials_vault
        WHERE trim(tenant_id) = trim(%s) AND provider = 'exchange'
    """, (tenant_id,))
    result = {r['key_type']: r['encrypted_key'] for r in cur.fetchall()}
    cur.close()
    return result


def _get_config(conn, tenant_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("""
        SELECT config FROM tenant_connectors
        WHERE trim(tenant_id) = trim(%s) AND connector = 'exchange'
        LIMIT 1
    """, (tenant_id,))
    row = cur.fetchone()
    cur.close()
    return row['config'] if row else {}


def _get_mapped_entities(conn, tenant_id: str) -> list:
    """Return all active mapped entities for this tenant."""
    cur = conn.cursor()
    cur.execute("""
        SELECT entity_id, entity_display, entity_email,
               entity_type, mapped_user_id
        FROM connector_entity_map
        WHERE trim(tenant_id) = trim(%s)
          AND connector_type = 'exchange'
          AND is_active = true
        ORDER BY entity_type, entity_display
    """, (tenant_id,))
    result = cur.fetchall()
    cur.close()
    return [dict(r) for r in result]


def _update_connector_sync(conn, tenant_id: str, count: int, error: str = None):
    cur = conn.cursor()
    cur.execute("""
        UPDATE connector_sources
        SET last_sync_at    = NOW(),
            last_sync_count = %s,
            last_error      = %s,
            updated_at      = NOW()
        WHERE trim(tenant_id) = trim(%s) AND connector_type = 'exchange'
    """, (count, error, tenant_id))
    conn.commit()
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# Email sync
# ─────────────────────────────────────────────────────────────────────────────

def _sync_mailbox_email(conn, tenant_id: str, account, entity: dict,
                         since: datetime) -> int:
    """
    Pull emails from one mailbox since `since`.
    Upserts into email_routing_queue.
    Returns count of new/updated rows.
    """
    from exchangelib import Q

    count = 0
    cur = conn.cursor()

    try:
        inbox = account.inbox
        messages = inbox.filter(
            datetime_received__gte=since
        ).only(
            'message_id', 'internet_message_id', 'subject',
            'sender', 'to_recipients', 'cc_recipients',
            'datetime_received', 'text_body', 'has_attachments',
            'attachments'
        ).order_by('-datetime_received')[:200]

        for msg in messages:
            try:
                from_email = (msg.sender.email_address
                               if msg.sender else None)
                from_display = (msg.sender.name
                                 if msg.sender else None)
                to_emails = []
                if msg.to_recipients:
                    to_emails = [r.email_address for r in msg.to_recipients
                                  if r.email_address]
                cc_emails = []
                if msg.cc_recipients:
                    cc_emails = [r.email_address for r in msg.cc_recipients
                                  if r.email_address]
                body_preview = (msg.text_body or '')[:500] if msg.text_body else ''
                attachment_names = []
                if msg.has_attachments and msg.attachments:
                    attachment_names = [
                        a.name for a in msg.attachments
                        if hasattr(a, 'name') and a.name
                    ]

                import json
                cur.execute("""
                    INSERT INTO email_routing_queue (
                        tenant_id, connector_type, message_id,
                        internet_message_id, subject, from_email,
                        from_display, to_emails, cc_emails,
                        received_at, body_preview, has_attachments,
                        attachment_names, routing_status, attorney_user_id
                    ) VALUES (
                        %s, 'exchange', %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb,
                        'pending', %s
                    )
                    ON CONFLICT (message_id) DO UPDATE SET
                        body_preview   = EXCLUDED.body_preview,
                        has_attachments = EXCLUDED.has_attachments,
                        attorney_user_id = COALESCE(
                            email_routing_queue.attorney_user_id,
                            EXCLUDED.attorney_user_id
                        )
                """, (
                    tenant_id,
                    str(msg.message_id or ''),
                    str(msg.internet_message_id or ''),
                    msg.subject or '(No Subject)',
                    from_email,
                    from_display,
                    json.dumps(to_emails),
                    json.dumps(cc_emails),
                    msg.datetime_received,
                    body_preview,
                    bool(msg.has_attachments),
                    json.dumps(attachment_names),
                    entity.get('mapped_user_id'),
                ))
                conn.commit()
                count += 1
            except Exception as msg_exc:
                logger.warning("[exchange_sync] email msg error: %s", msg_exc)
                conn.rollback()

    except Exception as exc:
        logger.error("[exchange_sync] mailbox email error [%s]: %s",
                     entity.get('entity_email'), exc)
    finally:
        cur.close()

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Calendar sync
# ─────────────────────────────────────────────────────────────────────────────

def _sync_mailbox_calendar(conn, tenant_id: str, account, entity: dict,
                            start: datetime, end: datetime) -> int:
    """
    Pull calendar events from one mailbox between start and end.
    Upserts into exchange_calendar_events.
    Returns count of new/updated rows.
    """
    import json
    from exchangelib.ewsdatetime import EWSDateTime, EWSTimeZone

    count = 0
    cur = conn.cursor()

    try:
        tz = EWSTimeZone.localzone()
        ews_start = EWSDateTime.from_datetime(start.replace(tzinfo=None)).replace(tzinfo=tz)
        ews_end   = EWSDateTime.from_datetime(end.replace(tzinfo=None)).replace(tzinfo=tz)

        calendar_items = account.calendar.view(
            start=ews_start, end=ews_end
        )

        for item in calendar_items:
            try:
                attendees = []
                if item.required_attendees:
                    attendees = [
                        {'name': a.mailbox.name, 'email': a.mailbox.email_address}
                        for a in item.required_attendees
                        if a.mailbox
                    ]

                source_cal = 'resource' if entity.get('entity_type') == 'calendar' else 'personal'

                cur.execute("""
                    INSERT INTO exchange_calendar_events (
                        tenant_id, mailbox, ews_item_id, change_key,
                        subject, start_at, end_at, is_all_day,
                        location, organizer_email, organizer_name,
                        attendees, body_preview, is_recurring,
                        source_calendar, attorney_user_id,
                        routing_status, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb, %s, %s,
                        %s, %s, 'pending', NOW(), NOW()
                    )
                    ON CONFLICT (ews_item_id) DO UPDATE SET
                        change_key   = EXCLUDED.change_key,
                        subject      = EXCLUDED.subject,
                        start_at     = EXCLUDED.start_at,
                        end_at       = EXCLUDED.end_at,
                        location     = EXCLUDED.location,
                        attendees    = EXCLUDED.attendees,
                        body_preview = EXCLUDED.body_preview,
                        updated_at   = NOW()
                """, (
                    tenant_id,
                    entity.get('entity_email', ''),
                    str(item.id),
                    str(item.changekey or ''),
                    item.subject or '(No Subject)',
                    item.start.ewsformat() if item.start else None,
                    item.end.ewsformat() if item.end else None,
                    bool(getattr(item, 'is_all_day', False)),
                    getattr(item, 'location', None),
                    item.organizer.email_address if item.organizer else None,
                    item.organizer.name if item.organizer else None,
                    json.dumps(attendees),
                    (item.text_body or '')[:300] if item.text_body else '',
                    bool(getattr(item, 'is_recurring', False)),
                    source_cal,
                    entity.get('mapped_user_id'),
                ))
                conn.commit()
                count += 1
            except Exception as item_exc:
                logger.warning("[exchange_sync] calendar item error: %s", item_exc)
                conn.rollback()

    except Exception as exc:
        logger.error("[exchange_sync] calendar error [%s]: %s",
                     entity.get('entity_email'), exc)
    finally:
        cur.close()

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Main sync entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(tenant_id: str, triggered_by: int = None):
    """
    Main Exchange sync job.
    Enqueued by manual trigger or scheduler.
    """
    logger.info("[exchange_sync] starting tenant=%s", tenant_id)

    conn = None
    total_synced = 0

    try:
        conn = _get_db_conn()
        creds  = _get_credentials(conn, tenant_id)
        config = _get_config(conn, tenant_id)

        if not creds.get('username') or not creds.get('password'):
            raise ValueError("Exchange credentials missing from credentials_vault")

        ews_url  = config.get('ews_url', '')
        username = creds['username']
        password = creds['password']
        domain   = creds.get('domain', '')

        sync_email    = str(config.get('sync_email', 'true')).lower() == 'true'
        sync_calendar = str(config.get('sync_calendar', 'true')).lower() == 'true'
        email_lookback    = int(config.get('email_lookback_days', 60))
        cal_lookback      = int(config.get('calendar_lookback_days', 90))
        cal_lookahead     = int(config.get('calendar_lookahead_days', 180))

        now = datetime.now(timezone.utc)
        email_since   = now - timedelta(days=email_lookback)
        cal_start     = now - timedelta(days=cal_lookback)
        cal_end       = now + timedelta(days=cal_lookahead)

        # Build EWS connection
        from exchangelib import Credentials, Configuration, Account, IMPERSONATION
        from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
        import urllib3
        urllib3.disable_warnings()
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

        full_username = f"{domain}\\{username}" if domain else username
        ews_creds = Credentials(username=full_username, password=password)
        ews_config = Configuration(
            service_endpoint=ews_url,
            credentials=ews_creds,
            auth_type='NTLM',
        )

        entities = _get_mapped_entities(conn, tenant_id)
        logger.info("[exchange_sync] %d entities to sync", len(entities))

        for entity in entities:
            email = entity.get('entity_email')
            etype = entity.get('entity_type', 'mailbox')

            if not email:
                continue

            logger.info("[exchange_sync] syncing %s (%s)", email, etype)

            try:
                account = Account(
                    primary_smtp_address=email,
                    config=ews_config,
                    autodiscover=False,
                    access_type=IMPERSONATION,
                )

                # Email sync — mailbox and shared_mailbox only
                if sync_email and etype in ('mailbox', 'shared_mailbox'):
                    count = _sync_mailbox_email(
                        conn, tenant_id, account, entity, email_since
                    )
                    total_synced += count
                    logger.info("[exchange_sync] %s email: %d messages", email, count)

                # Calendar sync — mailbox and calendar resources
                if sync_calendar and etype in ('mailbox', 'calendar'):
                    count = _sync_mailbox_calendar(
                        conn, tenant_id, account, entity, cal_start, cal_end
                    )
                    total_synced += count
                    logger.info("[exchange_sync] %s calendar: %d events", email, count)

            except Exception as entity_exc:
                logger.error("[exchange_sync] entity %s failed: %s",
                             email, entity_exc)
                continue

        _update_connector_sync(conn, tenant_id, total_synced)
        logger.info("[exchange_sync] complete tenant=%s total=%d",
                    tenant_id, total_synced)

    except Exception as exc:
        logger.exception("[exchange_sync] job failed: %s", exc)
        if conn:
            try:
                _update_connector_sync(conn, tenant_id, 0, str(exc))
            except Exception:
                pass
    finally:
        if conn:
            conn.close()
