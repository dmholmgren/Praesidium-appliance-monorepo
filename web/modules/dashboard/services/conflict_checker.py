"""COMP 5 — Conflict of Interest Checker.

Runs automatically on new matter creation.  Checks adverse parties
against all existing clients, matters, and contacts using fuzzy matching.
Results: clear | potential | hard_conflict.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from rapidfuzz import fuzz

from modules.dashboard.services._audit_safe import safe_audit
from core.db.session import TenantSession
from modules.dashboard.models import ConflictCheck, ConflictWaiver

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85  # percent — potential conflict if score >= this
EXACT_THRESHOLD = 95  # percent — hard conflict if score >= this


async def run_conflict_check(
    ts: TenantSession,
    parties: list[dict[str, str]],
    *,
    matter_id: int | None = None,
    checked_by: int | None = None,
) -> ConflictCheck:
    """Run conflict check against all clients, matter contacts, and contacts.

    Args:
        ts: tenant-scoped session
        parties: list of dicts with keys 'name' and optionally 'type' (individual|entity)
        matter_id: optional matter being created
        checked_by: user ID who initiated the check

    Returns:
        ConflictCheck record with overall_result set
    """
    # Gather all existing names to check against
    existing = await _gather_existing_names(ts)
    results: list[dict[str, Any]] = []
    overall = "clear"

    for party in parties:
        party_name = party.get("name", "").strip()
        if not party_name:
            continue

        party_results = _check_party_against_existing(party_name, existing)
        results.append({
            "party_name": party_name,
            "matches": party_results,
        })

        for match in party_results:
            if match["level"] == "hard_conflict":
                overall = "hard_conflict"
            elif match["level"] == "potential" and overall != "hard_conflict":
                overall = "potential"

    check = ConflictCheck(
        tenant_id=ts.tenant_id,
        matter_id=matter_id,
        checked_by=checked_by,
        check_type="new_matter",
        parties_checked=[p.get("name", "") for p in parties],
        results=results,
        overall_result=overall,
        created_at=datetime.now(timezone.utc),
    )
    ts.session.add(check)
    await ts.session.flush()

    await safe_audit(
        ts, "CREATE", "conflict_checks", check.id,
        new_values={"overall_result": overall, "party_count": len(parties)},
        user_id=checked_by,
    )
    return check


async def create_waiver(
    ts: TenantSession,
    conflict_check_id: int,
    waived_by: int,
    documented_reason: str,
) -> ConflictWaiver:
    """Create a documented waiver for a hard conflict — requires super_admin."""
    waiver = ConflictWaiver(
        tenant_id=ts.tenant_id,
        conflict_check_id=conflict_check_id,
        waived_by=waived_by,
        documented_reason=documented_reason,
        created_at=datetime.now(timezone.utc),
    )
    ts.session.add(waiver)
    await ts.session.flush()

    await safe_audit(
        ts, "CREATE", "conflict_waivers", waiver.id,
        new_values={"conflict_check_id": conflict_check_id, "reason": documented_reason},
        user_id=waived_by,
    )
    return waiver


async def _gather_existing_names(ts: TenantSession) -> list[dict[str, Any]]:
    """Gather all names from clients, matter_contacts, and contacts tables."""
    names: list[dict[str, Any]] = []

    # Clients
    rows = await ts.execute_query(
        "SELECT id, client_name, client_type FROM clients WHERE tenant_id = :tid",
        {"tid": ts.tenant_id},
    )
    for r in rows:
        names.append({"name": r["client_name"], "source": "client", "id": r["id"], "type": r.get("client_type")})

    # Contacts
    rows = await ts.execute_query(
        "SELECT id, CONCAT(COALESCE(first_name,''), ' ', COALESCE(last_name,'')) as full_name, "
        "company FROM contacts WHERE tenant_id = :tid",
        {"tid": ts.tenant_id},
    )
    for r in rows:
        if r["full_name"].strip():
            names.append({"name": r["full_name"].strip(), "source": "contact", "id": r["id"]})
        if r.get("company"):
            names.append({"name": r["company"], "source": "contact_company", "id": r["id"]})

    # Matter contacts (adverse parties)
    rows = await ts.execute_query(
        "SELECT mc.id, mc.matter_id, c.client_name as name, mc.role "
        "FROM matter_contacts mc JOIN clients c ON mc.contact_id = c.id "
        "WHERE mc.tenant_id = :tid",
        {"tid": ts.tenant_id},
    )
    for r in rows:
        names.append({
            "name": r["name"], "source": "matter_contact",
            "id": r["id"], "matter_id": r["matter_id"], "role": r.get("role"),
        })

    return names


def _check_party_against_existing(
    party_name: str,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check one party name against all existing names using fuzzy matching."""
    matches: list[dict[str, Any]] = []
    for ex in existing:
        score = fuzz.token_sort_ratio(party_name.lower(), ex["name"].lower())
        if score >= FUZZY_THRESHOLD:
            level = "hard_conflict" if score >= EXACT_THRESHOLD else "potential"
            matches.append({
                "existing_name": ex["name"],
                "source": ex["source"],
                "source_id": ex["id"],
                "score": score,
                "level": level,
            })
    return sorted(matches, key=lambda m: m["score"], reverse=True)
