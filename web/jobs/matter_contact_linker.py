"""
matter_contact_linker.py
========================

Phase B1 of the Contact Workstream. Tenant-agnostic.

Mines three classes of signal to propose matter-contact relationships:

  1. ts_clients (legacy Timeslips data)
     - ts_clients.ts_raw->>'nickname2' joins to matters.matter_number
     - For each ts_client row, we have:
         * ts_name      -> propose contact match against contacts.full_name
         * ts_email     -> propose contact match against contacts.email
         * ts_phone     -> propose contact match against contacts.phone
         * opp_counsel  -> propose contact match for OC role
         * paralegal    -> propose contact match for paralegal role
         * referred_by  -> propose contact match for referrer role
     The matched contact is linked to the resolved matter (via nickname2).
     This is the strongest signal: 1.0 confidence on email exact, 0.95
     on phone exact, 0.85 on name+role triangulation.

  2. email_routing_queue (active routing data)
     - For each row with matched_matter_id IS NOT NULL, the from_email
       (and optionally to/cc) implies that contact-email belongs on that
       matter. AUTO-CREATES the contact if missing (contact_type='auto_email').
       Confidence depends on volume: 1+ routed email -> 0.85,
       3+ -> 0.92, 5+ -> 0.95.
     - Note: As of v11.8 deployment, email_routing_queue.matched_matter_id
       may be entirely null; this signal contributes nothing in that
       state but the code is in place for when routing fires.

  3. (Future) keyword-in-name signal — left as enhancement; lower
     precedence and not implemented in this pass.

WRITES
------
All proposals land in matter_contact_proposals. Per architectural
decision (May 5 2026), even 1.0-confidence proposals require human
approval — but a separate auto-promotion step (NOT YET IMPLEMENTED)
can promote 1.0-confidence proposals to matter_contacts directly,
creating a 'auto_approved' audit row. This module only writes
proposals; the promotion step lives in Phase C UI.

CONTACT AUTO-CREATION
---------------------
Per decision: yes, auto-create contacts for email senders with no
existing match. Sets contact_type='auto_email'. Names initialized
from from_display when present, falling back to local-part of the
email address. NEVER auto-creates from ts_clients (those should map
to existing contacts via the dedupe heuristics already in place).

IDEMPOTENCY
-----------
The unique partial index uq_mcp_pending_pair ensures we don't
double-propose the same (contact, matter) pair while pending. Re-runs
of the linker UPDATE the existing pending row (incrementing
signal_count, taking the max confidence, appending to notes) instead
of inserting a duplicate.

USAGE
-----
    from jobs.matter_contact_linker import run_linker
    stats = await run_linker(tenant_id="...", dry_run=False)

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.jobs.matter_contact_linker")


# ---------------------------------------------------------------------------
# Phone normalization (mirrors contact_hygiene)
# ---------------------------------------------------------------------------
def _normalize_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def _normalize_email(email: Optional[str]) -> str:
    if not email:
        return ""
    return email.strip().lower()


def _normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z]+", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Proposal upsert
# ---------------------------------------------------------------------------
async def _upsert_proposal(
    db,
    tenant_id: str,
    contact_id: int,
    matter_id: str,
    proposed_role: Optional[str],
    proposed_is_primary: str,
    signal_type: str,
    confidence: float,
    notes: str,
    run_id: str,
) -> str:
    """Insert a proposal, or update existing PENDING proposal for the same
    pair (incrementing signal_count, taking max confidence, appending notes).

    Returns 'inserted' | 'updated' | 'skipped' (matter already linked).
    """
    # First: check if matter_contacts already has this pair (don't propose
    # what's already done)
    r = await db.execute(
        text("""
            SELECT 1 FROM matter_contacts
            WHERE TRIM(tenant_id) = :tid
              AND contact_id = :cid
              AND matter_id = CAST(:mid AS uuid)
        """),
        {"tid": tenant_id.strip(), "cid": contact_id, "mid": matter_id},
    )
    if r.fetchone():
        return "skipped"

    # Second: check if a pending proposal already exists for this pair
    r = await db.execute(
        text("""
            SELECT id, confidence, signal_count, notes
            FROM matter_contact_proposals
            WHERE TRIM(tenant_id) = :tid
              AND contact_id = :cid
              AND matter_id = CAST(:mid AS uuid)
              AND review_status = 'pending'
        """),
        {"tid": tenant_id.strip(), "cid": contact_id, "mid": matter_id},
    )
    existing = r.fetchone()

    if existing:
        # Update: bump signal_count, take max confidence, append notes
        existing_id = existing[0]
        existing_conf = float(existing[1] or 0)
        existing_count = int(existing[2] or 1)
        existing_notes = existing[3] or ""
        new_conf = max(existing_conf, confidence)
        merged_notes = (existing_notes + "\n[+]" + notes)[:4000]
        await db.execute(
            text("""
                UPDATE matter_contact_proposals
                   SET confidence = :conf,
                       signal_count = :count,
                       notes = :notes,
                       linker_run_id = :run
                 WHERE id = :id
            """),
            {
                "conf": new_conf,
                "count": existing_count + 1,
                "notes": merged_notes,
                "run": run_id,
                "id": existing_id,
            },
        )
        return "updated"

    # Third: insert new proposal
    await db.execute(
        text("""
            INSERT INTO matter_contact_proposals
                (id, tenant_id, contact_id, matter_id,
                 proposed_role, proposed_is_primary,
                 signal_type, confidence, signal_count, notes,
                 linker_run_id)
            VALUES
                (:id, :tid, :cid, CAST(:mid AS uuid),
                 :role, :primary,
                 :sig, :conf, 1, :notes,
                 :run)
        """),
        {
            "id": str(uuid.uuid4()),
            "tid": tenant_id.strip(),
            "cid": contact_id,
            "mid": matter_id,
            "role": proposed_role,
            "primary": proposed_is_primary,
            "sig": signal_type,
            "conf": confidence,
            "notes": notes[:4000],
            "run": run_id,
        },
    )
    return "inserted"


# ---------------------------------------------------------------------------
# Contact lookup helpers (used by both signal sources)
# ---------------------------------------------------------------------------
async def _index_contacts(tenant_id: str) -> Dict[str, Any]:
    """Build in-memory indexes of contacts for this tenant. Cheap on small
    tenants; revisit if any tenant has 10k+ contacts.

    Returns:
        {
            "by_email": {email -> [contact_id, ...]},
            "by_phone": {digits -> [contact_id, ...]},
            "by_name":  {normalized_name -> [contact_id, ...]},
            "all":      [{id, full_name, email, phone, ...}, ...],
        }
    """
    by_email: Dict[str, List[int]] = defaultdict(list)
    by_phone: Dict[str, List[int]] = defaultdict(list)
    by_name: Dict[str, List[int]] = defaultdict(list)
    all_contacts: List[Dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, full_name, email, phone, company, contact_type
                FROM contacts
                WHERE TRIM(tenant_id) = :tid
            """),
            {"tid": tenant_id.strip()},
        )
        for row in r.fetchall():
            cid = int(row[0])
            full_name = row[1] or ""
            email = _normalize_email(row[2])
            phone = _normalize_phone(row[3])
            n_name = _normalize_name(full_name)
            all_contacts.append({
                "id": cid, "full_name": full_name, "email": email,
                "phone": phone, "company": row[4], "type": row[5],
            })
            if email:
                by_email[email].append(cid)
            if phone:
                by_phone[phone].append(cid)
            if n_name:
                by_name[n_name].append(cid)

    return {
        "by_email": dict(by_email),
        "by_phone": dict(by_phone),
        "by_name": dict(by_name),
        "all": all_contacts,
    }


async def _create_auto_contact(
    db,
    tenant_id: str,
    email: str,
    display_name: Optional[str],
) -> Optional[int]:
    """Insert a new contact for an email sender with no existing match.

    Returns the new contact_id, or None on failure.
    """
    if not email or "@" not in email:
        return None

    # Build a sensible full_name
    name = (display_name or "").strip()
    if not name:
        local = email.split("@", 1)[0]
        # Title-case local part heuristically: 'jdoe' -> 'Jdoe',
        # 'john.doe' -> 'John Doe', 'john_doe' -> 'John Doe'
        parts = re.split(r"[._-]+", local)
        name = " ".join(p.capitalize() for p in parts if p) or local

    try:
        r = await db.execute(
            text("""
                INSERT INTO contacts
                    (tenant_id, full_name, contact_type, email)
                VALUES
                    (:tid, :name, 'auto_email', :email)
                RETURNING id
            """),
            {
                "tid": tenant_id.strip(),
                "name": name[:500],
                "email": email[:255],
            },
        )
        new_id = r.scalar()
        return int(new_id) if new_id else None
    except Exception as exc:
        log.warning("Auto-create contact failed for %s: %s", email, exc)
        return None


# ---------------------------------------------------------------------------
# Signal source 1: ts_clients
# ---------------------------------------------------------------------------
async def mine_ts_clients(
    tenant_id: str,
    contacts_idx: Dict[str, Any],
    run_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """For each ts_clients row that resolves to a matter via nickname2,
    propose contact links based on email/phone/name fields.

    Field-to-role mapping:
      ts_email / email / ts_name / phone1  -> 'client'  (the client themselves)
      opp_counsel                           -> 'opposing_counsel'
      paralegal                             -> 'paralegal'
      referred_by                           -> 'referred_by'

    Confidence:
      Exact email match in contacts.email                   -> 1.00
      Exact phone match in contacts.phone (10-digit norm)   -> 0.95
      Exact normalized-name match (single hit)              -> 0.85
      Normalized-name match with multiple hits              -> 0.70 (ambiguous)
      Free-text name parsed from opp_counsel/paralegal/etc  -> 0.75

    Notes are built including the ts_client_id and the field that matched
    so the audit trail makes sense in the review UI.
    """
    stats = {
        "ts_clients_scanned": 0,
        "ts_clients_with_matter": 0,
        "proposals": defaultdict(int),  # signal_type -> count
        "ops": defaultdict(int),        # inserted/updated/skipped
    }

    by_email = contacts_idx["by_email"]
    by_phone = contacts_idx["by_phone"]
    by_name  = contacts_idx["by_name"]

    async with AsyncSessionLocal() as db:
        # Pull ts_clients with their resolved matter_id in one query
        r = await db.execute(
            text("""
                SELECT
                    tc.id            AS ts_client_pk,
                    tc.ts_client_id,
                    tc.ts_name,
                    tc.ts_email,
                    tc.email,
                    tc.ts_phone,
                    tc.phone1,
                    tc.opp_counsel,
                    tc.paralegal,
                    tc.referred_by,
                    tc.associate,
                    tc.ts_raw->>'nickname2' AS nickname2,
                    m.id            AS matter_id,
                    m.matter_name,
                    m.matter_number
                FROM ts_clients tc
                LEFT JOIN matters m
                  ON m.matter_number = tc.ts_raw->>'nickname2'
                 AND TRIM(m.tenant_id) = :tid
                WHERE TRIM(tc.tenant_id) = :tid
                  AND tc.ts_raw->>'nickname2' IS NOT NULL
                  AND tc.ts_raw->>'nickname2' != ''
            """),
            {"tid": tenant_id.strip()},
        )
        rows = r.mappings().fetchall()
        stats["ts_clients_scanned"] = len(rows)

        for row in rows:
            if not row["matter_id"]:
                continue
            stats["ts_clients_with_matter"] += 1
            matter_id = str(row["matter_id"])
            ts_client_id = row["ts_client_id"]
            matter_label = (
                f"{row['matter_number']} {row['matter_name'] or ''}"
            ).strip()

            # Build a list of (role, field_name, value) triples to test
            # The 'client' fields prefer the union: email > phone > name
            client_email = (
                _normalize_email(row["ts_email"])
                or _normalize_email(row["email"])
            )
            client_phone = (
                _normalize_phone(row["ts_phone"])
                or _normalize_phone(row["phone1"])
            )
            client_name = row["ts_name"] or ""

            # ---- Client role: try email first, then phone, then name ----
            client_proposals: List[Tuple[int, float, str, str]] = []
            # (contact_id, confidence, signal_type, note)

            if client_email and client_email in by_email:
                for cid in by_email[client_email]:
                    client_proposals.append((
                        cid, 1.00, "ts_clients_email",
                        f"ts_client {ts_client_id}: ts_email/email = "
                        f"{client_email!r} matches contact.email exactly",
                    ))

            if not client_proposals and client_phone and client_phone in by_phone:
                hits = by_phone[client_phone]
                conf = 0.95 if len(hits) == 1 else 0.80
                for cid in hits:
                    client_proposals.append((
                        cid, conf, "ts_clients_phone",
                        f"ts_client {ts_client_id}: phone {client_phone} "
                        f"matches contact.phone "
                        f"({'unique' if len(hits) == 1 else f'{len(hits)} candidates'})",
                    ))

            if not client_proposals and client_name:
                n_name = _normalize_name(client_name)
                if n_name in by_name:
                    hits = by_name[n_name]
                    conf = 0.85 if len(hits) == 1 else 0.70
                    for cid in hits:
                        client_proposals.append((
                            cid, conf, "ts_clients_name",
                            f"ts_client {ts_client_id}: ts_name "
                            f"{client_name!r} matches contact.full_name "
                            f"normalized "
                            f"({'unique' if len(hits) == 1 else f'{len(hits)} candidates'})",
                        ))

            for cid, conf, sig, note in client_proposals:
                if dry_run:
                    op = "inserted"
                else:
                    op = await _upsert_proposal(
                        db, tenant_id, cid, matter_id,
                        "client", "Y",
                        sig, conf,
                        f"[matter: {matter_label}] {note}",
                        run_id,
                    )
                stats["proposals"][sig] += 1
                stats["ops"][op] += 1

            # ---- Other-role fields (opp_counsel, paralegal, referred_by) ----
            for role_field, role_label in (
                ("opp_counsel",  "opposing_counsel"),
                ("paralegal",    "paralegal"),
                ("referred_by",  "referred_by"),
                ("associate",    "co_counsel"),
            ):
                raw = (row[role_field] or "").strip()
                if not raw:
                    continue
                # These fields are free-text; we only attempt name match
                # and only if the parsed name is reasonably long
                n_name = _normalize_name(raw)
                if not n_name or len(n_name) < 4:
                    continue
                hits = by_name.get(n_name, [])
                if not hits:
                    continue
                conf = 0.75 if len(hits) == 1 else 0.55
                sig = f"ts_clients_{role_field}"
                for cid in hits:
                    note = (
                        f"ts_client {ts_client_id}: {role_field} field "
                        f"{raw!r} matches contact.full_name normalized "
                        f"({'unique' if len(hits) == 1 else f'{len(hits)} candidates'})"
                    )
                    if dry_run:
                        op = "inserted"
                    else:
                        op = await _upsert_proposal(
                            db, tenant_id, cid, matter_id,
                            role_label, "N",
                            sig, conf,
                            f"[matter: {matter_label}] {note}",
                            run_id,
                        )
                    stats["proposals"][sig] += 1
                    stats["ops"][op] += 1

        if not dry_run:
            await db.commit()

    return {
        "ts_clients_scanned": stats["ts_clients_scanned"],
        "ts_clients_with_matter": stats["ts_clients_with_matter"],
        "proposals_by_signal": dict(stats["proposals"]),
        "ops": dict(stats["ops"]),
    }


# ---------------------------------------------------------------------------
# Signal source 2: email_routing_queue
# ---------------------------------------------------------------------------
async def mine_email_routing(
    tenant_id: str,
    contacts_idx: Dict[str, Any],
    run_id: str,
    dry_run: bool = False,
    include_recipients: bool = False,
) -> Dict[str, Any]:
    """For each row in email_routing_queue with matched_matter_id IS NOT
    NULL, propose contact-matter links from the from_email (and optionally
    to/cc).

    Auto-creates a contact when from_email has no match (per design
    decision).

    Confidence by volume of routed emails per (sender, matter):
        1   email  -> 0.85
        3+  emails -> 0.92
        5+  emails -> 0.95
    """
    stats = {
        "rows_scanned": 0,
        "rows_routed": 0,
        "auto_created_contacts": 0,
        "proposals": defaultdict(int),
        "ops": defaultdict(int),
    }

    by_email = contacts_idx["by_email"]

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT
                    matched_matter_id,
                    LOWER(from_email) AS from_email,
                    from_display,
                    to_emails,
                    cc_emails,
                    COUNT(*) AS n_emails
                FROM email_routing_queue
                WHERE TRIM(tenant_id) = :tid
                  AND matched_matter_id IS NOT NULL
                  AND from_email IS NOT NULL
                GROUP BY matched_matter_id, LOWER(from_email), from_display,
                         to_emails, cc_emails
            """),
            {"tid": tenant_id.strip()},
        )
        # NOTE: GROUP BY on jsonb columns to_emails/cc_emails is fragile;
        # a real implementation would explode them into a separate query
        # path. For now, we collapse only on the (matter, from_email) pair
        # and re-process recipients per row when --include-recipients.
        rows = r.mappings().fetchall()
        stats["rows_scanned"] = len(rows)

        # Aggregate by (matter, from_email)
        sender_volumes: Dict[Tuple[str, str], int] = defaultdict(int)
        sender_displays: Dict[str, str] = {}
        rows_by_sender: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            mid = str(row["matched_matter_id"])
            from_email = row["from_email"]
            if not from_email or "@" not in from_email:
                continue
            sender_volumes[(mid, from_email)] += int(row["n_emails"] or 1)
            if row.get("from_display"):
                sender_displays[from_email] = row["from_display"]
            rows_by_sender[(mid, from_email)].append(row)
            stats["rows_routed"] += int(row["n_emails"] or 1)

        for (matter_id, from_email), vol in sender_volumes.items():
            # Confidence by volume
            if vol >= 5:
                conf = 0.95
            elif vol >= 3:
                conf = 0.92
            else:
                conf = 0.85

            # Find contact (or auto-create)
            contact_ids = by_email.get(from_email, [])
            if not contact_ids:
                if dry_run:
                    # Skip auto-create on dry-run (it's a write)
                    continue
                new_id = await _create_auto_contact(
                    db, tenant_id, from_email,
                    sender_displays.get(from_email),
                )
                if new_id is None:
                    continue
                contact_ids = [new_id]
                # Mirror into the index so subsequent iterations within
                # this run see it
                by_email.setdefault(from_email, []).append(new_id)
                stats["auto_created_contacts"] += 1

            note = (
                f"email_routing_queue: {vol} email(s) from {from_email!r} "
                f"routed to this matter"
            )
            for cid in contact_ids:
                if dry_run:
                    op = "inserted"
                else:
                    op = await _upsert_proposal(
                        db, tenant_id, cid, matter_id,
                        proposed_role=None, proposed_is_primary="N",
                        signal_type="email_from",
                        confidence=conf,
                        notes=note,
                        run_id=run_id,
                    )
                stats["proposals"]["email_from"] += 1
                stats["ops"][op] += 1

            # Recipients (to/cc) only if explicitly requested
            if include_recipients:
                recip_emails: set[str] = set()
                for row in rows_by_sender[(matter_id, from_email)]:
                    for fld in ("to_emails", "cc_emails"):
                        recipients = row.get(fld) or []
                        if isinstance(recipients, str):
                            try:
                                recipients = json.loads(recipients)
                            except (ValueError, TypeError):
                                recipients = []
                        for rec in recipients:
                            if isinstance(rec, dict):
                                e = _normalize_email(rec.get("email"))
                            elif isinstance(rec, str):
                                e = _normalize_email(rec)
                            else:
                                e = ""
                            if e and "@" in e:
                                recip_emails.add(e)
                for e in recip_emails:
                    cids = by_email.get(e, [])
                    if not cids:
                        continue  # don't auto-create for recipients
                    sig = "email_to_cc"
                    rec_conf = max(0.55, conf - 0.20)
                    note_r = (
                        f"email_routing_queue: {e!r} appeared as recipient "
                        f"on email(s) routed to this matter"
                    )
                    for cid in cids:
                        if dry_run:
                            op = "inserted"
                        else:
                            op = await _upsert_proposal(
                                db, tenant_id, cid, matter_id,
                                proposed_role=None, proposed_is_primary="N",
                                signal_type=sig,
                                confidence=rec_conf,
                                notes=note_r,
                                run_id=run_id,
                            )
                        stats["proposals"][sig] += 1
                        stats["ops"][op] += 1

        if not dry_run:
            await db.commit()

    return {
        "rows_scanned": stats["rows_scanned"],
        "rows_routed": stats["rows_routed"],
        "auto_created_contacts": stats["auto_created_contacts"],
        "proposals_by_signal": dict(stats["proposals"]),
        "ops": dict(stats["ops"]),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_linker(
    tenant_id: str,
    dry_run: bool = True,
    skip_ts_clients: bool = False,
    skip_email_routing: bool = False,
    include_recipients: bool = False,
) -> Dict[str, Any]:
    """Run all enabled signal-mining passes."""
    run_id = f"linker-{uuid.uuid4().hex[:8]}"
    log.info(
        "Matter-contact linker starting (tenant=%s, dry_run=%s, run_id=%s)",
        tenant_id, dry_run, run_id,
    )

    contacts_idx = await _index_contacts(tenant_id)
    n_contacts = len(contacts_idx["all"])
    log.info("Indexed %d contacts (by_email=%d, by_phone=%d, by_name=%d)",
             n_contacts,
             len(contacts_idx["by_email"]),
             len(contacts_idx["by_phone"]),
             len(contacts_idx["by_name"]))

    out: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "dry_run": dry_run,
        "indexed_contacts": n_contacts,
        "ts_clients_signal": None,
        "email_routing_signal": None,
    }

    if not skip_ts_clients:
        try:
            out["ts_clients_signal"] = await mine_ts_clients(
                tenant_id, contacts_idx, run_id, dry_run=dry_run,
            )
        except Exception as exc:
            log.exception("ts_clients mining failed")
            out["ts_clients_signal"] = {"error": f"{type(exc).__name__}: {exc}"}

    if not skip_email_routing:
        try:
            out["email_routing_signal"] = await mine_email_routing(
                tenant_id, contacts_idx, run_id,
                dry_run=dry_run,
                include_recipients=include_recipients,
            )
        except Exception as exc:
            log.exception("email_routing mining failed")
            out["email_routing_signal"] = {"error": f"{type(exc).__name__}: {exc}"}

    log.info("Matter-contact linker complete: %s", out)
    return out
