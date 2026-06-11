"""
contact_hygiene.py
==================

Phase A1 of the Contact Workstream. Tenant-agnostic.

A1 — Phone extraction (deterministic, idempotent):
    For contacts where `phone IS NULL` and `full_name` ends with a phone
    pattern like '(NNN) NNN-NNNN', '(NNN)NNN-NNNN', or 'NNN-NNN-NNNN',
    extract the digits into the `phone` column and strip the phone
    fragment from `full_name`.

A2 — Duplicate cluster detection (proposes only, never auto-merges):
    Groups contacts within a tenant by three signal types and writes
    each cluster (>= 2 contacts) as a row in contact_dedup_candidates:
        - 'exact_phone'              : same normalized digits in phone OR
                                       extracted from name (post-A1)
        - 'exact_email'              : same lowercase email
        - 'fuzzy_name+exact_phone'   : Levenshtein <= 2 on full_name AND
                                       same phone
    Skips clusters already represented by a pending row.

USAGE
-----
    from jobs.contact_hygiene import run_hygiene
    stats = await run_hygiene(tenant_id="...", dry_run=False)

    # In a script:
    asyncio.run(run_hygiene(tenant_id="...", dry_run=True))  # preview
    asyncio.run(run_hygiene(tenant_id="...", dry_run=False)) # apply

DESIGN NOTES
------------
- TRIM(tenant_id) on every query (varchar(36) padding-prone).
- AsyncSessionLocal used directly (per appliance convention).
- All DB writes wrapped in a single transaction per phase, with
  explicit commits at phase boundaries so a partial failure doesn't
  half-clean the contacts table.
- Pure-data; no UI, no cron — meant to be invoked by a script (A3) or
  later from a CRUD UI's "Run Hygiene" button (Phase C).

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.jobs.contact_hygiene")


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
# Trailing phone patterns we know how to extract from full_name.
# Order matters: most specific first.
_PHONE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # "Bhadresh Trivedi(214) 208-5078"
    # "Barbara (325) 660-3260"
    ("parens",
     re.compile(r"\s*\((\d{3})\)\s*(\d{3})-?(\d{4})\s*$")),
    # "Person Name 214-208-5078"
    ("dashes",
     re.compile(r"\s+(\d{3})-(\d{3})-(\d{4})\s*$")),
    # "Person Name 2142085078"
    ("plain",
     re.compile(r"\s+(\d{10})\s*$")),
]


def _extract_trailing_phone(full_name: str) -> Tuple[str, Optional[str]]:
    """Strip a trailing phone fragment from full_name.

    Returns (cleaned_name, digits_or_None). If no pattern matches,
    returns (full_name, None) unchanged.

    The returned digits are always 10 characters. Country code stripping
    is handled here for the 'plain' pattern only (parens/dashes patterns
    are by-construction 10 digits).
    """
    if not full_name:
        return "", None
    for kind, pat in _PHONE_PATTERNS:
        m = pat.search(full_name)
        if not m:
            continue
        if kind == "plain":
            digits = m.group(1)
        else:
            digits = m.group(1) + m.group(2) + m.group(3)
        if len(digits) != 10:
            continue
        cleaned = full_name[: m.start()].rstrip()
        # Don't strip down to empty; if cleaning removes everything
        # (full_name was just the phone), return original to preserve
        # whatever identifying info exists.
        if not cleaned:
            return full_name, None
        return cleaned, digits
    return full_name, None


def _normalize_phone(phone: Optional[str]) -> str:
    """Reduce phone to last-10-digits form for comparison."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def _normalize_name(name: Optional[str]) -> str:
    """Lowercase + collapse whitespace + strip non-letters for fuzzy match.
    Good enough for 'Trivedi' vs 'Trevidi' and 'Mark Self' vs 'ark Self'.
    """
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"[^a-z]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance. Adequate for short names; we cap
    candidate pairs by other signals first so this is never called on
    the full O(n^2) cross-product."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 4:
        # Early exit — too different to matter for our threshold of 2
        return abs(len(a) - len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + cost, # substitution
            )
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Phase A1: phone extraction
# ---------------------------------------------------------------------------
async def extract_trapped_phones(
    tenant_id: str, dry_run: bool = False
) -> Dict[str, Any]:
    """Find contacts where full_name contains a trailing phone fragment
    and the phone column is NULL. Extract the phone, write to the phone
    column, strip the fragment from full_name.

    Returns:
        {
            "scanned": int,
            "would_update": int,        # dry_run only
            "updated": int,             # actual run
            "skipped_no_pattern": int,
            "samples": [first 5 (id, before, after, phone), ...],
        }
    """
    stats: Dict[str, Any] = {
        "scanned": 0,
        "would_update": 0,
        "updated": 0,
        "skipped_no_pattern": 0,
        "samples": [],
    }

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, full_name
                FROM contacts
                WHERE TRIM(tenant_id) = :tid
                  AND (phone IS NULL OR phone = '')
                  AND full_name ~ '[0-9]'
                ORDER BY id
            """),
            {"tid": tenant_id.strip()},
        )
        rows = r.fetchall()
        stats["scanned"] = len(rows)

        updates: List[Tuple[int, str, str, str]] = []
        # (id, original_name, cleaned_name, digits)
        for row in rows:
            cid, full_name = row[0], row[1]
            cleaned, digits = _extract_trailing_phone(full_name or "")
            if digits is None:
                stats["skipped_no_pattern"] += 1
                continue
            updates.append((cid, full_name, cleaned, digits))

        if dry_run:
            stats["would_update"] = len(updates)
            stats["samples"] = [
                {"id": u[0], "before": u[1], "after": u[2], "phone": u[3]}
                for u in updates[:5]
            ]
            log.info(
                "[dry_run] phone extraction: scanned=%d would_update=%d "
                "skipped_no_pattern=%d",
                stats["scanned"], stats["would_update"],
                stats["skipped_no_pattern"],
            )
            return stats

        # Apply updates
        for cid, original, cleaned, digits in updates:
            # Format digits as canonical XXX-XXX-XXXX (matches how clean
            # contacts already look for emerging consistency)
            formatted = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
            try:
                await db.execute(
                    text("""
                        UPDATE contacts
                           SET full_name = :name,
                               phone = :phone,
                               updated_at = NOW()
                         WHERE id = :id
                           AND TRIM(tenant_id) = :tid
                    """),
                    {
                        "name": cleaned,
                        "phone": formatted,
                        "id": cid,
                        "tid": tenant_id.strip(),
                    },
                )
                stats["updated"] += 1
                if len(stats["samples"]) < 5:
                    stats["samples"].append({
                        "id": cid, "before": original,
                        "after": cleaned, "phone": formatted,
                    })
            except Exception as exc:
                log.warning("Phone-extraction update failed for id=%s: %s",
                            cid, exc)
        await db.commit()

    log.info(
        "Phone extraction: scanned=%d updated=%d skipped_no_pattern=%d",
        stats["scanned"], stats["updated"], stats["skipped_no_pattern"],
    )
    return stats


# ---------------------------------------------------------------------------
# Phase A2: duplicate cluster detection
# ---------------------------------------------------------------------------
async def detect_duplicates(
    tenant_id: str,
    run_id: Optional[str] = None,
    dry_run: bool = False,
    fuzzy_name_distance: int = 2,
) -> Dict[str, Any]:
    """Detect duplicate-contact clusters within a tenant. Writes one row
    per cluster to contact_dedup_candidates (skipping clusters already
    represented by a pending row).

    Signal types produced:
      - 'exact_phone'            : same normalized phone digits
      - 'exact_email'            : same lowercase email
      - 'fuzzy_name+exact_phone' : same phone AND name distance <= N

    Returns stats dict with counts per signal.
    """
    if run_id is None:
        run_id = f"hygiene-{uuid.uuid4().hex[:8]}"

    stats: Dict[str, Any] = {
        "run_id": run_id,
        "scanned_contacts": 0,
        "candidates_proposed": {
            "exact_phone": 0,
            "exact_email": 0,
            "fuzzy_name+exact_phone": 0,
        },
        "skipped_already_pending": 0,
        "samples": [],
    }

    async with AsyncSessionLocal() as db:
        # Pull every contact for this tenant (single read)
        r = await db.execute(
            text("""
                SELECT id, full_name, email, phone, company
                FROM contacts
                WHERE TRIM(tenant_id) = :tid
                ORDER BY id
            """),
            {"tid": tenant_id.strip()},
        )
        rows = r.fetchall()
        stats["scanned_contacts"] = len(rows)

        contacts = [
            {
                "id": row[0],
                "full_name": row[1] or "",
                "email": (row[2] or "").lower().strip() or None,
                "phone": _normalize_phone(row[3]) or None,
                "company": row[4],
            }
            for row in rows
        ]

        # ----- Build buckets -----
        by_phone: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        by_email: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for c in contacts:
            if c["phone"]:
                by_phone[c["phone"]].append(c)
            if c["email"]:
                by_email[c["email"]].append(c)

        # ----- Existing pending candidates: don't double-propose -----
        # We hash each pending candidate's contact_ids set so we can skip
        # exact re-proposals.
        r = await db.execute(
            text("""
                SELECT contact_ids
                FROM contact_dedup_candidates
                WHERE TRIM(tenant_id) = :tid
                  AND review_outcome IS NULL
            """),
            {"tid": tenant_id.strip()},
        )
        pending_clusters = set()
        for row in r.fetchall():
            ids = row[0]
            if isinstance(ids, list):
                pending_clusters.add(tuple(sorted(int(i) for i in ids)))

        proposals: List[Dict[str, Any]] = []

        # --- exact_phone clusters ---
        for phone, members in by_phone.items():
            if len(members) < 2:
                continue
            cluster_ids = tuple(sorted(m["id"] for m in members))
            if cluster_ids in pending_clusters:
                stats["skipped_already_pending"] += 1
                continue
            proposals.append({
                "ids": list(cluster_ids),
                "signal_type": "exact_phone",
                "confidence": 0.95,
                "notes": (
                    f"{len(members)} contacts share normalized phone {phone}. "
                    f"Names: " + "; ".join(
                        f"id={m['id']} '{m['full_name']}'" for m in members
                    )
                ),
            })

        # --- exact_email clusters ---
        for email, members in by_email.items():
            if len(members) < 2:
                continue
            cluster_ids = tuple(sorted(m["id"] for m in members))
            if cluster_ids in pending_clusters:
                stats["skipped_already_pending"] += 1
                continue
            proposals.append({
                "ids": list(cluster_ids),
                "signal_type": "exact_email",
                "confidence": 0.97,
                "notes": (
                    f"{len(members)} contacts share email {email}. "
                    f"Names: " + "; ".join(
                        f"id={m['id']} '{m['full_name']}'" for m in members
                    )
                ),
            })

        # --- fuzzy_name+exact_phone (high-precision pattern) ---
        # Only check pairs WITHIN the exact_phone buckets we already
        # gathered. This bounds the work to reasonable size.
        for phone, members in by_phone.items():
            if len(members) < 2:
                continue
            # Within the bucket, find sub-clusters whose normalized names
            # are within fuzzy_name_distance of each other.
            for i, m1 in enumerate(members):
                n1 = _normalize_name(m1["full_name"])
                if not n1:
                    continue
                near = [m1]
                for m2 in members[i + 1:]:
                    n2 = _normalize_name(m2["full_name"])
                    if not n2:
                        continue
                    if _levenshtein(n1, n2) <= fuzzy_name_distance:
                        near.append(m2)
                if len(near) >= 2:
                    cluster_ids = tuple(sorted(m["id"] for m in near))
                    if cluster_ids in pending_clusters:
                        continue
                    # Skip if same as the exact_phone cluster we already
                    # captured (would duplicate the proposal — exact_phone
                    # already covers it)
                    full_phone_cluster = tuple(sorted(m["id"] for m in members))
                    if cluster_ids == full_phone_cluster:
                        continue
                    proposals.append({
                        "ids": list(cluster_ids),
                        "signal_type": "fuzzy_name+exact_phone",
                        "confidence": 0.90,
                        "notes": (
                            f"Fuzzy name match on phone {phone}. "
                            f"Names: " + "; ".join(
                                f"id={m['id']} '{m['full_name']}'" for m in near
                            )
                        ),
                    })

        # ----- Persist proposals (or sample for dry-run) -----
        for p in proposals:
            stats["candidates_proposed"][p["signal_type"]] += 1
            if len(stats["samples"]) < 8:
                stats["samples"].append({
                    "ids": p["ids"],
                    "signal": p["signal_type"],
                    "notes": p["notes"][:200],
                })

        if dry_run:
            log.info(
                "[dry_run] dedup detection: scanned=%d "
                "proposals_by_signal=%s skipped_already_pending=%d",
                stats["scanned_contacts"],
                stats["candidates_proposed"],
                stats["skipped_already_pending"],
            )
            return stats

        for p in proposals:
            try:
                import json as _json
                await db.execute(
                    text("""
                        INSERT INTO contact_dedup_candidates
                            (id, tenant_id, contact_ids, signal_type,
                             confidence, notes, hygiene_run_id)
                        VALUES
                            (:id, :tid, CAST(:ids AS jsonb), :sig,
                             :conf, :notes, :run)
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": tenant_id.strip(),
                        "ids": _json.dumps(p["ids"]),
                        "sig": p["signal_type"],
                        "conf": p["confidence"],
                        "notes": p["notes"],
                        "run": run_id,
                    },
                )
            except Exception as exc:
                log.warning("Dedup candidate insert failed: %s", exc)
        await db.commit()

    log.info(
        "Dedup detection: scanned=%d proposals=%s skipped_already_pending=%d "
        "run_id=%s",
        stats["scanned_contacts"], stats["candidates_proposed"],
        stats["skipped_already_pending"], run_id,
    )
    return stats


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_hygiene(
    tenant_id: str,
    dry_run: bool = True,
    skip_phone_extraction: bool = False,
    skip_dedup_detection: bool = False,
) -> Dict[str, Any]:
    """Run both hygiene phases in order.

    Args:
        tenant_id:               Tenant scope.
        dry_run:                 If True, no writes; counts only.
                                 Default True (safe by default).
        skip_phone_extraction:   Skip Phase A1.
        skip_dedup_detection:    Skip Phase A2.

    Returns combined stats dict.
    """
    run_id = f"hygiene-{uuid.uuid4().hex[:8]}"
    log.info(
        "Contact hygiene starting (tenant=%s, dry_run=%s, run_id=%s)",
        tenant_id, dry_run, run_id,
    )
    out: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "dry_run": dry_run,
        "phase_a1_phone_extraction": None,
        "phase_a2_dedup_detection": None,
    }

    if not skip_phone_extraction:
        out["phase_a1_phone_extraction"] = await extract_trapped_phones(
            tenant_id, dry_run=dry_run,
        )

    if not skip_dedup_detection:
        out["phase_a2_dedup_detection"] = await detect_duplicates(
            tenant_id, run_id=run_id, dry_run=dry_run,
        )

    log.info("Contact hygiene complete: %s", out)
    return out
