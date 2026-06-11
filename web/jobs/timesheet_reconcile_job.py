"""
jobs/timesheet_reconcile_job.py
Timesheet Reconciliation Engine — Praesidium Series 2.0

Pulls from four data sources, runs deterministic rule ladder to match
events to matters, writes timesheet_drafts for attorney review.

AI escalation is OPT-IN — triggered separately from the review UI,
never automatically.  No audit trail on drafts.  Audit fires only
when approved entries push to time_entries.

Sources:
  exchange_calendar  — exchange_calendar_events table (already synced)
  exchange_email     — email_routing_queue table (already synced)
  manictime          — ManicTime Server API (NTLM auth, on-prem)
  phone_csv          — uploaded carrier CSV (any carrier format)
  imazing_csv        — uploaded iMazing text message export CSV
  timeslips          — ts_slips table (historical cross-reference)
  ai_api_calls       — AI usage logs (token → time estimate)

Rule Ladder (deterministic, no AI):
  1. Pre-matched matter from source connector (confidence from source)
  2. Contact phone → matter_contacts → matter_id (confidence 0.90)
  3. Contact email exact → matter_contacts (confidence 0.88)
  4. Email domain → matter_contacts (confidence 0.70)
  5. Matter number keyword in text (confidence 0.80)
  6. Matter name keyword overlap (confidence 0.40–0.65)
  7. No match → confidence 0.0, status 'unmatched'

Filters:
  - Phone calls under 2 minutes dropped at parse time
  - SMS threads grouped by counterparty+date, minimum 5 min per thread

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

log = logging.getLogger("praesidium.jobs.timesheet_reconcile")


# ── Shared types ──────────────────────────────────────────────────────────────

class CandidateEvent:
    """Normalized event from any source, pre-match."""
    __slots__ = (
        "source", "event_date", "start_time", "end_time",
        "raw_minutes", "counterparty_name", "counterparty_phone",
        "counterparty_email", "subject", "body_preview", "app_name",
        "doc_title", "source_id", "source_detail", "raw_data",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    @property
    def searchable_text(self) -> str:
        parts = []
        for attr in ("subject", "body_preview", "app_name", "doc_title",
                      "counterparty_name"):
            val = getattr(self, attr)
            if val:
                parts.append(str(val))
        return " ".join(parts).lower()


# ── Phone number normalization ────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


# ── Source Loaders ────────────────────────────────────────────────────────────

async def load_exchange_calendar(tenant_id, user_id, date_from, date_to, db):
    from sqlalchemy import text
    rows = await db.execute(
        text("""
            SELECT id, subject, start_at, end_at, location,
                   organizer_email, organizer_name, attendees,
                   body_preview, matter_id, match_confidence
            FROM exchange_calendar_events
            WHERE trim(tenant_id) = trim(:tid)
              AND start_at::date BETWEEN :df AND :dt
            ORDER BY start_at
        """),
        {"tid": tenant_id, "df": date_from, "dt": date_to},
    )
    events = []
    for r in rows.mappings().fetchall():
        start = r["start_at"]
        end = r["end_at"]
        raw_min = max(0, (end - start).total_seconds() / 60) if start and end else 0
        attendees = r["attendees"] or []
        cp_emails, cp_names = [], []
        if isinstance(attendees, list):
            for a in attendees:
                if isinstance(a, dict):
                    cp_emails.append(a.get("email", ""))
                    cp_names.append(a.get("name", ""))
        events.append(CandidateEvent(
            source="exchange_calendar", event_date=start.date() if start else date_from,
            start_time=start, end_time=end, raw_minutes=raw_min,
            counterparty_name="; ".join(cp_names[:5]),
            counterparty_email="; ".join(cp_emails[:5]),
            counterparty_phone=None, subject=r["subject"],
            body_preview=r["body_preview"], app_name=None, doc_title=None,
            source_id=str(r["id"]),
            source_detail=json.dumps({
                "location": r["location"], "organizer": r["organizer_name"],
                "pre_matched_matter": str(r["matter_id"]) if r["matter_id"] else None,
                "pre_match_confidence": float(r["match_confidence"]) if r["match_confidence"] else None,
            }),
            raw_data=None,
        ))
    return events


async def load_exchange_email(tenant_id, user_id, date_from, date_to, db):
    from sqlalchemy import text
    rows = await db.execute(
        text("""
            SELECT id, subject, from_email, from_display,
                   to_emails, cc_emails, received_at, body_preview,
                   matched_matter_id, match_confidence
            FROM email_routing_queue
            WHERE trim(tenant_id) = trim(:tid)
              AND received_at::date BETWEEN :df AND :dt
            ORDER BY received_at
        """),
        {"tid": tenant_id, "df": date_from, "dt": date_to},
    )
    events = []
    for r in rows.mappings().fetchall():
        all_recips = []
        for lst in (r["to_emails"] or [], r["cc_emails"] or []):
            if isinstance(lst, list):
                for item in lst:
                    if isinstance(item, dict):
                        all_recips.append(item.get("email", ""))
                    elif isinstance(item, str):
                        all_recips.append(item)
        events.append(CandidateEvent(
            source="exchange_email",
            event_date=r["received_at"].date() if r["received_at"] else date_from,
            start_time=r["received_at"], end_time=None, raw_minutes=0,
            counterparty_name=r["from_display"],
            counterparty_email=r["from_email"] or "; ".join(all_recips[:5]),
            counterparty_phone=None, subject=r["subject"],
            body_preview=r["body_preview"], app_name=None, doc_title=None,
            source_id=str(r["id"]),
            source_detail=json.dumps({
                "from": r["from_email"], "to": all_recips[:10],
                "pre_matched_matter": str(r["matched_matter_id"]) if r["matched_matter_id"] else None,
                "pre_match_confidence": float(r["match_confidence"]) if r["match_confidence"] else None,
            }),
            raw_data=None,
        ))
    return events


async def load_manictime(tenant_id, user_id, date_from, date_to, db):
    """
    Fetch ManicTime activity data via NTLM-authenticated Server API.
    Reads entities (not activities) from the v2023+ API response format.
    Uses service account impersonation — one account accesses all timelines.
    """
    from sqlalchemy import text as _text
    import os, base64, json, uuid
    from datetime import datetime, timedelta

    row = await db.execute(
        _text("""
            SELECT tc.config, cv.encrypted_key
            FROM tenant_connectors tc
            LEFT JOIN credentials_vault cv
                ON trim(cv.tenant_id) = trim(tc.tenant_id)
                AND cv.provider = 'manictime'
                AND cv.key_type = 'api_key'
            WHERE trim(tc.tenant_id) = trim(:tid)
              AND tc.connector = 'manictime'
              AND tc.is_active = true
            LIMIT 1
        """),
        {"tid": tenant_id},
    )
    config_row = row.mappings().fetchone()
    if not config_row:
        log.warning("ManicTime connector not configured for tenant %s", tenant_id)
        return []

    config = config_row["config"] or {}
    if isinstance(config, str):
        config = json.loads(config)
    server_url = (config.get("server_url") or config.get("base_url", "")).rstrip("/")
    username = config.get("username", "")

    password = ""
    encrypted = config_row["encrypted_key"]
    if encrypted:
        try:
            from cryptography.fernet import Fernet
            secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
            key_bytes = (secret[:32]).encode().ljust(32, b"0")
            fernet_key = base64.urlsafe_b64encode(key_bytes)
            f = Fernet(fernet_key)
            password = f.decrypt(encrypted.encode()).decode()
        except Exception as exc:
            log.error("ManicTime decrypt failed: %s", exc)
            return []

    if not server_url or not username:
        log.warning("ManicTime missing server_url or username")
        return []

    events = []
    try:
        import requests as _requests
        import urllib3
        urllib3.disable_warnings()
        from requests_ntlm import HttpNtlmAuth

        domain = config.get("domain", "HJMMLEGAL")
        auth = HttpNtlmAuth(f"{domain}\\{username}", password)

        tl_resp = _requests.get(
            f"{server_url}/api/timelines",
            auth=auth, verify=False, timeout=30,
        )
        tl_resp.raise_for_status()
        timelines = tl_resp.json().get("timelines", [])
        log.info("ManicTime: %d timelines found", len(timelines))

        for tl in timelines:
            tl_key = tl.get("timelineKey")
            schema_name = (tl.get("schema") or {}).get("name", "")

            if not any(s in schema_name for s in ("Applications", "Documents")):
                continue

            act_resp = _requests.get(
                f"{server_url}/api/timelines/{tl_key}/activities",
                params={
                    "fromTime": date_from.isoformat(),
                    "toTime": (date_to + timedelta(days=1)).isoformat(),
                },
                auth=auth, verify=False, timeout=30,
            )
            if act_resp.status_code != 200:
                log.warning("ManicTime timeline %s returned %d", tl_key, act_resp.status_code)
                continue

            data = act_resp.json()
            all_entities = data.get("entities", [])

            groups = {}
            for ent in all_entities:
                if ent.get("entityType") == "group":
                    gid = ent.get("entityId")
                    gname = (ent.get("values") or {}).get("name", "")
                    groups[gid] = gname

            activities = [e for e in all_entities if e.get("entityType") == "activity"]

            for act in activities:
                values = act.get("values") or {}
                display_name = values.get("name", "")
                group_id = values.get("groupId")
                group_name = groups.get(group_id, "")
                time_interval = values.get("timeInterval") or {}
                start_str = time_interval.get("start", "")
                duration_secs = time_interval.get("duration", 0)

                combined = (display_name + group_name).lower()
                if any(s in combined for s in ("idle", "away", "locked", "sleep", "session lock")):
                    continue

                if duration_secs < 30:
                    continue

                try:
                    start_dt = datetime.fromisoformat(start_str)
                except (ValueError, TypeError):
                    continue

                end_dt = start_dt + timedelta(seconds=duration_secs)
                raw_min = max(0, duration_secs / 60)

                if "Applications" in schema_name:
                    app_name = group_name or display_name
                    doc_title = display_name if group_name else None
                else:
                    app_name = group_name
                    doc_title = display_name

                events.append(CandidateEvent(
                    source="manictime",
                    event_date=start_dt.date(),
                    start_time=start_dt,
                    end_time=end_dt,
                    raw_minutes=raw_min,
                    counterparty_name=None,
                    counterparty_email=None,
                    counterparty_phone=None,
                    subject=None,
                    body_preview=None,
                    app_name=app_name,
                    doc_title=doc_title,
                    source_id=str(act.get("entityId", uuid.uuid4())),
                    source_detail=json.dumps({
                        "timeline_type": schema_name,
                        "timeline_key": tl_key,
                        "group": group_name,
                        "duration_seconds": duration_secs,
                        "device": tl.get("deviceDisplayName"),
                        "owner": (tl.get("owner") or {}).get("username"),
                    }),
                    raw_data=json.dumps(values, default=str),
                ))

            log.info("ManicTime: %s - %d activities from %d entities",
                     schema_name, len(activities), len(all_entities))

    except Exception as exc:
        log.error("ManicTime API error: %s", exc, exc_info=True)

    log.info("ManicTime: %d total events for %s-%s", len(events), date_from, date_to)
    return events


def parse_phone_csv(content: str) -> list[CandidateEvent]:
    """Parse carrier CSV. Supports T-Mobile Usage Detail Report and generic formats.
    DROP CALLS UNDER 2 MINUTES."""
    events = []

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # ── T-Mobile Usage Detail Report detection ──
    # T-Mobile has metadata rows before the CSV header.
    # Header row contains "Subscriber Number" and "Duration".
    header_idx = None
    for i, line in enumerate(lines):
        if "Subscriber Number" in line and "Duration" in line:
            header_idx = i
            break

    if header_idx is not None:
        # T-Mobile format
        import csv
        reader = csv.DictReader(lines[header_idx:])
        for row in reader:
            try:
                date_str = (row.get("Date") or "").strip()
                call_type = (row.get("Type") or "").strip()
                party = (row.get("Party") or "").strip()
                duration_str = (row.get("Duration") or "0").strip()
                direction = (row.get("Direction") or "").strip()
                subscriber = (row.get("Line Identifier1 Value") or "").strip()

                if not date_str:
                    continue

                # Only process Talk (calls), skip Text and Data
                if call_type != "Talk":
                    continue

                # Parse duration (minutes)
                try:
                    minutes = float(duration_str)
                except (ValueError, TypeError):
                    minutes = 0

                # DROP CALLS UNDER 2 MINUTES
                if minutes < 2.0:
                    continue

                # Parse date
                parsed_date = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        parsed_date = datetime.strptime(date_str[:19], fmt)
                        break
                    except ValueError:
                        continue
                if not parsed_date:
                    continue

                # Normalize phone number
                phone = _normalize_phone(party)
                dir_label = "Incoming" if direction == "I" else "Outgoing"
                call_type_detail = (row.get("Call Type 1") or "").strip()
                desc = f"Phone call ({dir_label}): {party}"
                if call_type_detail:
                    desc += f" [{call_type_detail}]"

                events.append(CandidateEvent(
                    source="phone_csv",
                    event_date=parsed_date.date(),
                    start_time=parsed_date,
                    end_time=parsed_date + timedelta(minutes=minutes) if minutes else None,
                    raw_minutes=minutes,
                    counterparty_name=None,
                    counterparty_phone=phone,
                    counterparty_email=None,
                    subject=desc,
                    body_preview=f"{dir_label} call, {int(minutes)} min, subscriber: {subscriber}",
                    app_name=None, doc_title=None,
                    source_id=f"tmobile_{parsed_date.isoformat()}_{party}",
                    source_detail=json.dumps({
                        "raw_number": party, "raw_duration": duration_str,
                        "direction": direction, "call_type": call_type_detail,
                        "subscriber": subscriber, "carrier": "tmobile",
                    }),
                    raw_data=json.dumps(dict(row)),
                ))
            except Exception:
                continue

        log.info("T-Mobile CSV: %d call events (after 2-min filter)", len(events))
        return events

    # ── Generic carrier CSV fallback (original logic) ──
    import csv as _csv
    reader = _csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return events
    date_col = next((h for h in reader.fieldnames
                     if any(k in h.lower() for k in ("date", "time", "when"))), None)
    dur_col = next((h for h in reader.fieldnames
                    if any(k in h.lower() for k in ("duration", "min", "seconds", "length"))), None)
    num_col = next((h for h in reader.fieldnames
                    if any(k in h.lower() for k in ("number", "phone", "to", "from", "contact"))), None)
    if not date_col:
        return events

    for row in reader:
        try:
            raw_date = (row.get(date_col) or "").strip()
            if not raw_date:
                continue
            parsed_date = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y",
                        "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed_date = datetime.strptime(raw_date[:19], fmt)
                    break
                except ValueError:
                    continue
            if not parsed_date:
                continue
            raw_dur = (row.get(dur_col) or "0").strip() if dur_col else "0"
            minutes = 0.0
            if ":" in raw_dur:
                parts = raw_dur.split(":")
                try:
                    if len(parts) == 3:
                        minutes = int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 60
                    elif len(parts) == 2:
                        minutes = int(parts[0]) + int(parts[1]) / 60
                except (ValueError, IndexError):
                    pass
            else:
                try:
                    val = float(raw_dur.replace(",", ""))
                    minutes = val / 60 if val > 300 else val
                except ValueError:
                    pass
            if minutes < 2.0:
                continue
            number = (row.get(num_col) or "").strip() if num_col else ""
            events.append(CandidateEvent(
                source="phone_csv", event_date=parsed_date.date(),
                start_time=parsed_date,
                end_time=parsed_date + timedelta(minutes=minutes) if minutes else None,
                raw_minutes=minutes, counterparty_name=None,
                counterparty_phone=_normalize_phone(number),
                counterparty_email=None,
                subject=f"Phone call: {number}",
                body_preview=None, app_name=None, doc_title=None,
                source_id=f"phone_{parsed_date.isoformat()}_{number}",
                source_detail=json.dumps({"raw_number": number, "raw_duration": raw_dur}),
                raw_data=json.dumps(row),
            ))
        except Exception:
            continue
    log.info("Phone CSV: %d events (after 2-min filter)", len(events))
    return events


def parse_imazing_csv(content: str) -> list[CandidateEvent]:
    """Parse iMazing CSV export. Supports Chat Session format.
    Group by counterparty+date into threads. Minimum 5 min per thread."""
    events = []
    import csv as _csv

    reader = _csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return events

    hmap = {h.strip(): h for h in reader.fieldnames}

    # Detect iMazing Chat Session format
    is_chat_format = "Chat Session" in hmap and "Message Date" in hmap

    if is_chat_format:
        threads: dict[str, dict] = {}
        for row in reader:
            try:
                session = (row.get("Chat Session") or "").strip()
                msg_date = (row.get("Message Date") or "").strip()
                msg_type = (row.get("Type") or "").strip()
                text = (row.get("Text") or "").strip()
                sender = (row.get("Sender Name") or "").strip()

                if not session or not msg_date:
                    continue

                # Skip short-code / marketing messages
                if session.isdigit() and len(session) <= 6:
                    continue

                parsed_ts = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                            "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
                    try:
                        parsed_ts = datetime.strptime(msg_date[:19], fmt)
                        break
                    except ValueError:
                        continue
                if not parsed_ts:
                    continue

                # Extract phone from session name if present
                cp_phone = ""
                cp_name = session
                phone_match = re.search(r"\+?1?(\d{10})", session.replace("-","").replace(" ",""))
                if phone_match:
                    cp_phone = phone_match.group(1)
                    if session.startswith("+") or session.replace("-","").replace("+","").isdigit():
                        cp_name = ""

                tkey = f"{session}|{parsed_ts.date().isoformat()}"
                if tkey not in threads:
                    threads[tkey] = {
                        "cp_phone": cp_phone, "cp_name": cp_name,
                        "event_date": parsed_ts.date(),
                        "first_ts": parsed_ts, "last_ts": parsed_ts,
                        "msg_count": 0, "messages": [],
                    }
                th = threads[tkey]
                th["msg_count"] += 1
                th["first_ts"] = min(th["first_ts"], parsed_ts)
                th["last_ts"] = max(th["last_ts"], parsed_ts)
                if text and len(th["messages"]) < 5:
                    th["messages"].append(text[:200])
            except Exception:
                continue

        for key, t in threads.items():
            span_min = max(5, (t["last_ts"] - t["first_ts"]).total_seconds() / 60)
            cp_display = t["cp_name"] or t["cp_phone"] or "Unknown"
            events.append(CandidateEvent(
                source="imazing_csv", event_date=t["event_date"],
                start_time=t["first_ts"], end_time=t["last_ts"],
                raw_minutes=span_min, counterparty_name=t["cp_name"],
                counterparty_phone=t["cp_phone"], counterparty_email=None,
                subject=f"Text thread: {cp_display} ({t['msg_count']} msgs)",
                body_preview="; ".join(t["messages"][:3])[:300],
                app_name=None, doc_title=None,
                source_id=f"imazing_{key}",
                source_detail=json.dumps({"message_count": t["msg_count"],
                                          "span_minutes": round(span_min, 1)}),
                raw_data=None,
            ))
        log.info("iMazing Chat CSV: %d thread events from %d threads", len(events), len(threads))
        return events

    # ── Legacy iMazing format fallback ──
    hmap_lower = {h.lower().strip(): h for h in reader.fieldnames}
    ts_col = hmap_lower.get("timestamp") or hmap_lower.get("date")
    from_col = hmap_lower.get("from") or hmap_lower.get("from name")
    to_col = hmap_lower.get("to") or hmap_lower.get("to name")
    msg_col = hmap_lower.get("message") or hmap_lower.get("text")
    date_col = hmap_lower.get("date")
    time_col = hmap_lower.get("time")

    threads2: dict[str, dict] = {}
    for row in reader:
        try:
            ts_raw = ""
            if ts_col and row.get(ts_col):
                ts_raw = row[ts_col].strip()
            elif date_col and row.get(date_col):
                d = row[date_col].strip()
                t = (row.get(time_col) or "00:00").strip() if time_col else "00:00"
                ts_raw = f"{d} {t}"
            if not ts_raw:
                continue
            parsed_ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                        "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M"):
                try:
                    parsed_ts = datetime.strptime(ts_raw[:19], fmt)
                    break
                except ValueError:
                    continue
            if not parsed_ts:
                continue
            from_val = (row.get(from_col) or "").strip() if from_col else ""
            to_val = (row.get(to_col) or "").strip() if to_col else ""
            message = (row.get(msg_col) or "").strip() if msg_col else ""
            if from_val.lower() in ("me", ""):
                cp_phone = _normalize_phone(to_val)
                cp_name = to_val
            else:
                cp_phone = _normalize_phone(from_val)
                cp_name = from_val
            tkey = f"{cp_phone or cp_name}|{parsed_ts.date().isoformat()}"
            if tkey not in threads2:
                threads2[tkey] = {
                    "cp_phone": cp_phone, "cp_name": cp_name,
                    "event_date": parsed_ts.date(),
                    "first_ts": parsed_ts, "last_ts": parsed_ts,
                    "msg_count": 0, "messages": [],
                }
            th = threads2[tkey]
            th["msg_count"] += 1
            th["first_ts"] = min(th["first_ts"], parsed_ts)
            th["last_ts"] = max(th["last_ts"], parsed_ts)
            if message and len(th["messages"]) < 5:
                th["messages"].append(message[:200])
        except Exception:
            continue

    for key, t in threads2.items():
        span_min = max(5, (t["last_ts"] - t["first_ts"]).total_seconds() / 60)
        events.append(CandidateEvent(
            source="imazing_csv", event_date=t["event_date"],
            start_time=t["first_ts"], end_time=t["last_ts"],
            raw_minutes=span_min, counterparty_name=t["cp_name"],
            counterparty_phone=t["cp_phone"], counterparty_email=None,
            subject=f"Text thread: {t['cp_name']} ({t['msg_count']} msgs)",
            body_preview="; ".join(t["messages"][:3]),
            app_name=None, doc_title=None,
            source_id=f"imazing_{key}",
            source_detail=json.dumps({"message_count": t["msg_count"], "span_minutes": span_min}),
            raw_data=None,
        ))
    log.info("iMazing CSV: %d thread events", len(events))
    return events


async def load_ai_api_calls(tenant_id, user_id, date_from, date_to, db):
    from sqlalchemy import text
    rows = await db.execute(
        text("""
            SELECT DATE(created_at) AS entry_date,
                   SUM(total_tokens) AS total_tokens,
                   COUNT(*) AS call_count,
                   module, purpose, allocated_to_matter_id
            FROM ai_api_calls
            WHERE trim(tenant_id) = trim(:tid)
              AND user_id = :uid
              AND DATE(created_at) BETWEEN :df AND :dt
              AND status = 'ok'
            GROUP BY DATE(created_at), module, purpose, allocated_to_matter_id
            ORDER BY entry_date, module
        """),
        {"tid": tenant_id, "uid": user_id, "df": date_from, "dt": date_to},
    )
    events = []
    for r in rows.mappings().fetchall():
        tokens = r["total_tokens"] or 0
        raw_min = max(6, (tokens / 1000) * 6)
        desc = f"AI-assisted: {r['module'] or 'Platform'}"
        if r["purpose"]:
            desc += f" — {r['purpose']}"
        events.append(CandidateEvent(
            source="ai_api_calls", event_date=r["entry_date"],
            start_time=None, end_time=None, raw_minutes=raw_min,
            counterparty_name=None, counterparty_email=None,
            counterparty_phone=None,
            subject=desc, body_preview=f"{r['call_count']} calls, {tokens:,} tokens",
            app_name="Praesidium AI", doc_title=None,
            source_id=f"ai_{r['entry_date']}_{r['module']}_{r['purpose']}",
            source_detail=json.dumps({
                "tokens": tokens, "call_count": r["call_count"],
                "module": r["module"], "purpose": r["purpose"],
                "pre_allocated_matter": str(r["allocated_to_matter_id"]) if r["allocated_to_matter_id"] else None,
            }),
            raw_data=None,
        ))
    return events


async def load_timeslips_reference(tenant_id, user_id, date_from, date_to, db):
    from sqlalchemy import text
    rows = await db.execute(
        text("""
            SELECT s.id, s.slip_date, s.hours, s.narrative,
                   s.source_client_id, s.source_tk_id,
                   tc.ts_name AS client_name,
                   tc.ts_raw->>'nickname2' AS matter_number
            FROM ts_slips s
            LEFT JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                AND trim(tc.tenant_id) = trim(:tid)
            WHERE trim(s.tenant_id) = trim(:tid)
              AND s.slip_date BETWEEN :df AND :dt
            ORDER BY s.slip_date
        """),
        {"tid": tenant_id, "df": date_from, "dt": date_to},
    )
    events = []
    for r in rows.mappings().fetchall():
        hours = float(r["hours"] or 0)
        events.append(CandidateEvent(
            source="timeslips", event_date=r["slip_date"],
            start_time=None, end_time=None, raw_minutes=hours * 60,
            counterparty_name=r["client_name"], counterparty_email=None,
            counterparty_phone=None, subject=r["narrative"],
            body_preview=None, app_name=None, doc_title=None,
            source_id=str(r["id"]),
            source_detail=json.dumps({
                "matter_number": r["matter_number"],
                "source_client_id": r["source_client_id"], "ts_hours": hours,
            }),
            raw_data=None,
        ))
    return events


# ── Rule Ladder ───────────────────────────────────────────────────────────────

class RuleLadder:
    """Deterministic matter matching — no AI, no tokens."""

    def __init__(self):
        self.phone_index: dict[str, list[tuple]] = {}
        self.email_index: dict[str, list[tuple]] = {}
        self.domain_index: dict[str, list[tuple]] = {}
        self.matter_names: dict[str, str] = {}
        self.matter_numbers: dict[str, str] = {}
        self.keyword_index: dict[str, str] = {}
        self.ts_matter_map: dict[str, str] = {}
        self.folder_path_index: dict[str, str] = {}  # folder_name_lower -> matter_id
        self.client_rates: dict[str, float] = {}  # client_id -> most recent rate

    # Skip generic email domains that match everything
    _SKIP_DOMAINS = frozenset({
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "aol.com", "icloud.com", "me.com", "live.com",
        "hjmmlegal.com",  # own firm domain
    })

    async def load(self, tenant_id: str, db):
        from sqlalchemy import text

        # Active matters
        result = await db.execute(
            text("""
                SELECT id, matter_name, matter_number, client_id
                FROM matters
                WHERE trim(tenant_id) = trim(:tid)
                  AND LOWER(status) IN ('active', 'open')
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            mid = str(r["id"])
            mname = r["matter_name"] or ""
            mnum = r["matter_number"] or ""
            self.matter_names[mid] = mname
            self.matter_numbers[mid] = mnum
            for word in re.split(r"[\s/\\,\-–]+", mname.lower()):
                if len(word) >= 3:
                    self.keyword_index[word] = mid
            if mnum:
                self.keyword_index[mnum.lower()] = mid

        # Contacts → matters
        result = await db.execute(
            text("""
                SELECT c.id AS cid, c.email, c.phone, c.full_name, mc.matter_id
                FROM contacts c
                JOIN matter_contacts mc ON mc.contact_id = c.id
                    AND trim(mc.tenant_id) = trim(:tid)
                WHERE trim(c.tenant_id) = trim(:tid)
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            mid = str(r["matter_id"])
            mname = self.matter_names.get(mid, "")
            entry = (r["cid"], mid, mname)
            phone = _normalize_phone(r["phone"] or "")
            if phone:
                self.phone_index.setdefault(phone, []).append(entry)
            email = (r["email"] or "").lower().strip()
            if email:
                self.email_index.setdefault(email, []).append(entry)
                domain = email.split("@")[-1] if "@" in email else ""
                if domain and domain not in self._SKIP_DOMAINS:
                    self.domain_index.setdefault(domain, []).append(entry)

        # ts_clients nickname2 → matter
        result = await db.execute(
            text("""
                SELECT tc.ts_raw->>'nickname2' AS nn2, m.id AS matter_id
                FROM ts_clients tc
                JOIN matters m ON m.matter_number = tc.ts_raw->>'nickname2'
                    AND trim(m.tenant_id) = trim(:tid)
                WHERE trim(tc.tenant_id) = trim(:tid)
                  AND tc.ts_raw->>'nickname2' IS NOT NULL
                  AND tc.ts_raw->>'nickname2' != ''
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            nn2 = (r["nn2"] or "").strip()
            if nn2:
                self.ts_matter_map[nn2.lower()] = str(r["matter_id"])

        # Folder paths for ManicTime file-path matching
        result = await db.execute(
            text("""
                SELECT m.id, m.folder_path, m.client_id, c.client_name
                FROM matters m
                LEFT JOIN clients c ON c.id = m.client_id
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.folder_path IS NOT NULL AND m.folder_path != ''
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            fp = r["folder_path"] or ""
            # Index by last path segment (the folder name)
            parts = [p for p in fp.replace("\\", "/").replace("\\", "/").split("/") if p.strip()]
            if parts:
                folder_name = parts[-1].strip().lower()
                self.folder_path_index[folder_name] = str(r["id"])
                # Also index client\folder pattern
                if len(parts) >= 2:
                    client_folder = (parts[-2].strip() + "/" + parts[-1].strip()).lower()
                    self.folder_path_index[client_folder] = str(r["id"])

        # Also index dms_folder_matches
        result = await db.execute(
            text("""
                SELECT matter_id, folder_path, best_disk_path
                FROM dms_folder_matches
                WHERE trim(tenant_id) = trim(:tid) AND status = 'accepted'
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            for p in [r["folder_path"], r["best_disk_path"]]:
                if not p:
                    continue
                parts = [s for s in p.replace("\\", "/").replace("\\", "/").split("/") if s.strip()]
                if parts:
                    self.folder_path_index[parts[-1].strip().lower()] = str(r["matter_id"])

        # Client rates (most recent rate per client)
        result = await db.execute(
            text("""
                SELECT DISTINCT ON (m.client_id) m.client_id, te.rate
                FROM time_entries te
                JOIN matters m ON m.id = te.matter_id
                WHERE trim(te.tenant_id) = trim(:tid)
                  AND te.rate > 0 AND te.rate IS NOT NULL
                ORDER BY m.client_id, te.date DESC
            """),
            {"tid": tenant_id},
        )
        for r in result.mappings().fetchall():
            if r["client_id"]:
                self.client_rates[str(r["client_id"])] = float(r["rate"])

        log.info("Rule ladder: %d phones, %d emails, %d domains, "
                 "%d matters, %d keywords, %d ts_maps, %d folder_paths, %d client_rates",
                 len(self.phone_index), len(self.email_index),
                 len(self.domain_index), len(self.matter_names),
                 len(self.keyword_index), len(self.ts_matter_map),
                 len(self.folder_path_index), len(self.client_rates))

    def match(self, event: CandidateEvent) -> tuple[Optional[str], float, str]:
        detail = {}
        try:
            detail = json.loads(event.source_detail) if event.source_detail else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # Pre-matched from connector
        pre = detail.get("pre_matched_matter") or detail.get("pre_allocated_matter")
        pre_conf = detail.get("pre_match_confidence")
        if pre and pre != "None" and pre in self.matter_names:
            return pre, float(pre_conf) if pre_conf else 0.85, f"pre_matched:{event.source}"

        # Timeslips: nickname2
        if event.source == "timeslips":
            mn = detail.get("matter_number", "")
            if mn and mn.lower() in self.ts_matter_map:
                return self.ts_matter_map[mn.lower()], 0.95, "ts_nickname2"

        # Phone
        if event.counterparty_phone:
            phone = _normalize_phone(event.counterparty_phone)
            if phone in self.phone_index:
                m = self.phone_index[phone]
                if len(m) == 1:
                    return m[0][1], 0.90, f"phone:{phone}"
                return m[0][1], 0.75, f"phone_multi:{phone}:{len(m)}"

        # Email exact
        if event.counterparty_email:
            for es in event.counterparty_email.split(";"):
                e = es.strip().lower()
                if e in self.email_index:
                    m = self.email_index[e]
                    if len(m) == 1:
                        return m[0][1], 0.88, f"email:{e}"
                    return m[0][1], 0.72, f"email_multi:{e}:{len(m)}"

        # Email domain
        if event.counterparty_email:
            for es in event.counterparty_email.split(";"):
                e = es.strip().lower()
                d = e.split("@")[-1] if "@" in e else ""
                if d in self.domain_index:
                    m = self.domain_index[d]
                    if len(m) == 1:
                        return m[0][1], 0.70, f"domain:{d}"
                    return m[0][1], 0.55, f"domain_multi:{d}:{len(m)}"

        # ManicTime file path matching (V:\Client\Folder\file.docx)
        # This is HIGH confidence — the file lives in the matter's folder
        doc_title = event.doc_title or ""
        app_name = event.app_name or ""
        combined_path = doc_title + " " + app_name
        path_match = re.search(r'[A-Z]:\\([^\\]+)\\([^\\]+)', combined_path)
        if not path_match:
            path_match = re.search(r'[A-Z]:\\([^\\]+)\\([^\\]+)',
                                   event.subject or "")
        if path_match:
            client_seg = path_match.group(1).strip().lower()
            folder_seg = path_match.group(2).strip().lower()
            # Try exact folder name
            if folder_seg in self.folder_path_index:
                mid = self.folder_path_index[folder_seg]
                return mid, 0.92, f"file_path_folder:{folder_seg}"
            # Try client/folder combo
            combo = f"{client_seg}/{folder_seg}"
            if combo in self.folder_path_index:
                mid = self.folder_path_index[combo]
                return mid, 0.92, f"file_path_combo:{combo}"
            # Fuzzy: check if folder_seg is substring of any indexed folder
            for idx_folder, mid in self.folder_path_index.items():
                if len(folder_seg) >= 4 and folder_seg in idx_folder:
                    return mid, 0.85, f"file_path_fuzzy:{folder_seg}~{idx_folder}"
                if len(idx_folder) >= 4 and idx_folder in folder_seg:
                    return mid, 0.85, f"file_path_fuzzy:{idx_folder}~{folder_seg}"

        # Keywords — matter number must be 3+ chars to avoid false positives
        text_lower = event.searchable_text
        if text_lower:
            for mnum_lower, mid in self.ts_matter_map.items():
                if len(mnum_lower) >= 3 and mnum_lower in text_lower:
                    return mid, 0.80, f"keyword_matter_num:{mnum_lower}"
            best_mid, best_score = None, 0
            for mid, mname in self.matter_names.items():
                words = [w for w in re.split(r"[\s/\\,\-–]+", mname.lower()) if len(w) >= 3]
                if not words:
                    continue
                hits = sum(1 for w in words if w in text_lower)
                score = hits / len(words)
                if hits >= 2 and score > best_score:
                    best_score = score
                    best_mid = mid
            if best_mid and best_score >= 0.5:
                return best_mid, min(0.65, 0.40 + best_score * 0.30), \
                    f"keyword_name:{self.matter_names[best_mid]}:{best_score:.2f}"

        return None, 0.0, "unmatched"


# ── Aggregation ───────────────────────────────────────────────────────────────

_SOURCE_MAP = {
    "exchange_calendar": "exchange_calendar", "exchange_email": "exchange_email",
    "manictime": "manictime", "phone_csv": "phone_csv",
    "imazing_csv": "phone_csv", "ai_api_calls": "ai_api_calls",
    "timeslips": "timeslips", "excel": "excel",
}


def aggregate_events(events, matches, billing_increment=0.25):
    """Aggregate events into draft time entries.

    AGGREGATION RULES:
    - manictime: aggregate by (date, matter, source) — many small app events
    - phone_csv, imazing_csv: aggregate by (date, matter, source) — call/text threads
    - exchange_email: NO aggregation — each email is its own draft
    - exchange_calendar: NO aggregation — each meeting is its own draft
    - ai_api_calls: aggregate by (date, matter, source) — token batches
    - timeslips: skip (reference only)
    """
    # Sources that should NOT be aggregated — each event = one draft
    NO_AGGREGATE = frozenset({"exchange_email", "exchange_calendar"})

    buckets: dict[tuple, list] = {}
    solo_items = []  # (event, matter_id, confidence, reason) for non-aggregated sources

    for event, (matter_id, confidence, reason) in zip(events, matches):
        if event.source == "timeslips":
            continue  # reference only

        if event.source in NO_AGGREGATE:
            solo_items.append((event, matter_id, confidence, reason))
        else:
            key = (event.event_date, matter_id or "__unmatched__", event.source)
            buckets.setdefault(key, []).append((event, matter_id, confidence, reason))

    drafts = []

    # Solo items — one draft per event
    for ev, matter_id, confidence, reason in solo_items:
        raw_min = ev.raw_minutes or 0
        # Calendar events get actual duration; emails get 0.25h minimum
        if ev.source == "exchange_calendar" and raw_min > 0:
            inc_min = billing_increment * 60
            total_min = math.ceil(raw_min / inc_min) * inc_min
        elif ev.source == "exchange_email":
            total_min = billing_increment * 60  # 0.25h per email
        else:
            total_min = max(billing_increment * 60, raw_min)
        hours = Decimal(str(round(total_min / 60, 4)))

        desc = ev.subject or ""
        if not desc and ev.app_name:
            desc = ev.app_name
            if ev.doc_title:
                desc += f": {ev.doc_title[:80]}"
        if not desc:
            desc = f"{ev.source} activity"

        # Clean email narratives: "Correspondence re: {subject}"
        if ev.source == "exchange_email" and desc:
            # Strip RE:/FW:/FWD: prefixes
            clean_subj = re.sub(r'^(RE|FW|FWD|Fw|Re|Fwd):\s*(\[EXTERNAL\]\s*)?', '', desc).strip()
            clean_subj = re.sub(r'^(RE|FW|FWD|Fw|Re|Fwd):\s*', '', clean_subj).strip()
            if clean_subj:
                desc = f"Correspondence re: {clean_subj[:80]}"
            else:
                desc = "Correspondence"

        drafts.append({
            "entry_date": ev.event_date, "matter_id": matter_id,
            "matter_name": None, "hours": hours,
            "description": desc[:500],
            "source": _SOURCE_MAP.get(ev.source, "manual"),
            "ai_confidence": confidence,
            "source_detail": json.dumps({
                "event_count": 1,
                "total_raw_minutes": raw_min,
                "match_reasons": [reason] if reason != "unmatched" else [],
                "source_ids": [ev.source_id] if ev.source_id else [],
            }),
            "status": "pending",
        })

    # Aggregated buckets — same as before
    for (edate, mid_key, source), items in buckets.items():
        matter_id = None if mid_key == "__unmatched__" else mid_key
        total_min = sum(ev.raw_minutes or 0 for ev, _, _, _ in items)
        inc_min = billing_increment * 60
        if total_min > 0:
            total_min = math.ceil(total_min / inc_min) * inc_min
        hours = Decimal(str(round(total_min / 60, 4)))
        best_conf = max(c for _, _, c, _ in items)
        reasons = list(set(r for _, _, _, r in items if r != "unmatched"))

        descs = []
        for ev, _, _, _ in items[:10]:
            if ev.subject:
                descs.append(ev.subject[:120])
            elif ev.app_name:
                d = ev.app_name
                if ev.doc_title:
                    d += f": {ev.doc_title[:80]}"
                descs.append(d)
        description = "; ".join(descs[:5])
        if len(items) > 5:
            description += f" (+{len(items) - 5} more)"

        drafts.append({
            "entry_date": edate, "matter_id": matter_id,
            "matter_name": None, "hours": hours,
            "description": description,
            "source": _SOURCE_MAP.get(source, "manual"),
            "ai_confidence": best_conf,
            "source_detail": json.dumps({
                "event_count": len(items),
                "total_raw_minutes": sum(ev.raw_minutes or 0 for ev, _, _, _ in items),
                "match_reasons": reasons[:5],
                "source_ids": [ev.source_id for ev, _, _, _ in items[:20]],
            }),
            "status": "pending",
        })

    drafts.sort(key=lambda d: (d["entry_date"], -(d["ai_confidence"] or 0)))
    return drafts


# ── Main Job ──────────────────────────────────────────────────────────────────

def run(session_id, tenant_id, user_id, user_name,
        date_from_str, date_to_str, file_data):
    """RQ job entry point."""
    import asyncio
    try:
        asyncio.run(_run_async(session_id, tenant_id, int(user_id),
                               user_name, date_from_str, date_to_str,
                               file_data or []))
    except Exception as exc:
        log.error("Timesheet reconcile failed: %s", exc, exc_info=True)
        _mark_session_failed(session_id, str(exc)[:500])


async def _run_async(session_id, tenant_id, user_id, user_name,
                     date_from_str, date_to_str, file_data):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    df = date.fromisoformat(date_from_str)
    dt = date.fromisoformat(date_to_str)
    log.info("Reconcile: %s %s–%s session=%s", tenant_id, df, dt, session_id)

    all_events = []
    sources_used = []

    # DB-backed sources
    async with AsyncSessionLocal() as db:
        for name, loader in [
            ("exchange_calendar", load_exchange_calendar),
            ("exchange_email", load_exchange_email),
            ("manictime", load_manictime),
            ("ai_api_calls", load_ai_api_calls),
            ("timeslips", load_timeslips_reference),
        ]:
            try:
                evts = await loader(tenant_id, user_id, df, dt, db)
                all_events.extend(evts)
                sources_used.append(name)
                log.info("  %s: %d events", name, len(evts))
            except Exception as exc:
                log.warning("  %s failed: %s", name, exc)

    # Uploaded files
    for fdata in file_data:
        filename = fdata.get("filename", "").lower()
        try:
            content_bytes = bytes.fromhex(fdata.get("content_b64", ""))
            content_str = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        if "imazing" in filename or "imessage" in filename or "text" in filename:
            parsed = parse_imazing_csv(content_str)
            all_events.extend(parsed)
            sources_used.append("imazing_csv")
        elif filename.endswith(".csv"):
            parsed = parse_phone_csv(content_str)
            all_events.extend(parsed)
            sources_used.append("phone_csv")
        elif filename.endswith((".xlsx", ".xls")):
            try:
                from modules.admin.timesheet_api import _parse_excel_timesheet
                for entry in _parse_excel_timesheet(content_bytes):
                    all_events.append(CandidateEvent(
                        source="excel", event_date=entry.get("entry_date"),
                        start_time=None, end_time=None,
                        raw_minutes=float(entry.get("hours", 0)) * 60,
                        counterparty_name=None, counterparty_email=None,
                        counterparty_phone=None,
                        subject=entry.get("description"), body_preview=None,
                        app_name=None, doc_title=None,
                        source_id=f"excel_{entry.get('entry_date')}",
                        source_detail=json.dumps(entry.get("source_detail", {})),
                        raw_data=None,
                    ))
                sources_used.append("excel")
            except Exception as exc:
                log.warning("Excel parse failed: %s", exc)

    log.info("Total events: %d from %s", len(all_events), set(sources_used))

    # Rule ladder
    ladder = RuleLadder()
    async with AsyncSessionLocal() as db:
        await ladder.load(tenant_id, db)

    matches = [ladder.match(ev) for ev in all_events]
    matched = sum(1 for _, c, _ in matches if c > 0)
    log.info("Rule ladder: %d matched, %d unmatched (%.1f%%)",
             matched, len(matches) - matched,
             (matched / max(1, len(matches))) * 100)

    # Aggregate
    drafts = aggregate_events(all_events, matches)
    for d in drafts:
        if d["matter_id"] and d["matter_id"] in ladder.matter_names:
            d["matter_name"] = ladder.matter_names[d["matter_id"]]

    # Dedup drafts — ManicTime generates duplicate source_ids
    seen_keys = set()
    deduped_drafts = []
    for d in drafts:
        detail = json.loads(d.get("source_detail", "{}"))
        source_ids = tuple(sorted(detail.get("source_ids", [])))
        key = (d["entry_date"], d["matter_id"], d["source"], source_ids)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped_drafts.append(d)
    if len(deduped_drafts) < len(drafts):
        log.info("Dedup: %d -> %d drafts (removed %d duplicates)",
                 len(drafts), len(deduped_drafts), len(drafts) - len(deduped_drafts))
    drafts = deduped_drafts

    # Write drafts — NO AUDIT TRAIL
    async with AsyncSessionLocal() as db:
        draft_count = 0
        for d in drafts:
            try:
                await db.execute(
                    text("""
                        INSERT INTO timesheet_drafts
                            (id, session_id, tenant_id, user_id,
                             entry_date, matter_id, matter_name,
                             hours, description, source, source_detail,
                             ai_confidence, status)
                        VALUES
                            (:id, :sid, :tid, :uid,
                             :edate, CAST(:mid AS uuid), :mname,
                             :hours, :desc, :source, CAST(:detail AS jsonb),
                             :conf, :status)
                    """),
                    {
                        "id": str(uuid.uuid4()), "sid": session_id,
                        "tid": tenant_id, "uid": user_id,
                        "edate": d["entry_date"], "mid": d["matter_id"],
                        "mname": d["matter_name"],
                        "hours": float(d["hours"]),
                        "desc": d["description"],
                        "source": d["source"],
                        "detail": d["source_detail"],
                        "conf": d["ai_confidence"],
                        "status": d["status"],
                    },
                )
                draft_count += 1
            except Exception as exc:
                log.warning("Draft write failed: %s", exc)

        # === M9 AI Reconciliation integration (Component 5) ===
        # Pass 1 (classifier) + Pass 2 (matcher). Non-fatal: any failure
        # leaves rule-ladder drafts intact and the session still completes.
        ai_stats = {"ai_passes_enabled": False}
        try:
            from jobs.ai_reconcile_integration import run_ai_passes
            ai_stats = await run_ai_passes(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                ladder_matter_names=ladder.matter_names,
            )
            log.info("AI passes complete: %s", ai_stats)
        except Exception as _ai_exc:
            log.exception("AI integration failed (non-fatal): %s", _ai_exc)
            ai_stats = {"ai_passes_enabled": False,
                        "error": f"{type(_ai_exc).__name__}: {_ai_exc}"}

        await db.execute(
            text("""
                UPDATE timesheet_sessions
                SET status = 'complete', draft_count = :dc,
                    sources_used = CAST(:src AS jsonb),
                    completed_at = NOW()
                WHERE id = :sid
            """),
            {"dc": draft_count,
             "src": json.dumps({
                 "sources": list(set(sources_used)),
                 "ai_stats": ai_stats,
             }),
             "sid": session_id},
        )
        await db.commit()
    log.info("Reconcile done: %d drafts for session %s", draft_count, session_id)


def _mark_session_failed(session_id, error_msg):
    import asyncio
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text

    async def _update():
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""UPDATE timesheet_sessions
                        SET status='failed', error_message=:err, completed_at=NOW()
                        WHERE id=:sid"""),
                {"err": error_msg, "sid": session_id},
            )
            await db.commit()
    try:
        asyncio.run(_update())
    except Exception:
        log.error("Failed to mark session %s as failed", session_id)
