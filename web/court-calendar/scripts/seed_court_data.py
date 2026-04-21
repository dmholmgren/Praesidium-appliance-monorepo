#!/usr/bin/env python3
"""
Seed script: Load court rules, holidays, and AI compliance rules.

Usage:
    python -m scripts.seed_court_data --tenant-id <UUID>

Loads:
  - FRCP master rules + TX/CA/CO delta files (COMP 3)
  - Federal + state holidays for current + next 2 years (COMP 3)
  - E.D. Tex. + Denton County AI certification rules (COMP 9)

All DB writes via write_audit().
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def seed_rules(tenant_id: str, db) -> int:
    """Seed court rules into the database."""
    from core.audit import write_audit
    from modules.court.models import CourtRule
    from modules.court.seed_data.rules_seed import get_all_rules

    rules = get_all_rules()
    count = 0

    for rule_data in rules:
        # Check for existing rule (avoid duplicates)
        existing = db.query_first(
            CourtRule,
            filters={
                "rule_number": rule_data["rule_number"],
                "jurisdiction": rule_data["jurisdiction"],
            },
        )
        if existing:
            logger.debug(f"Rule already exists: {rule_data['rule_number']}")
            continue

        rule = CourtRule(
            tenant_id=tenant_id,
            jurisdiction=rule_data["jurisdiction"],
            rule_set=rule_data["rule_set"],
            rule_number=rule_data["rule_number"],
            rule_title=rule_data["rule_title"],
            triggering_event=rule_data["triggering_event"],
            deadline_description=rule_data["deadline_description"],
            duration_days=rule_data["duration_days"],
            duration_type=rule_data["duration_type"],
            direction=rule_data["direction"],
            service_method_adjustments=rule_data.get("service_method_adjustments"),
            triggers_rules=rule_data.get("triggers_rules"),
            is_delta=rule_data.get("is_delta", False),
            is_active=True,
        )
        db.add(rule)
        count += 1

    db.flush()
    logger.info(f"Seeded {count} court rules")
    return count


def seed_holidays(tenant_id: str, db) -> int:
    """Seed court holidays for current and next 2 years."""
    from core.audit import write_audit
    from modules.court.models import CourtHoliday
    from modules.court.seed_data.rules_seed import get_all_holidays

    current_year = datetime.now().year
    years = [current_year, current_year + 1, current_year + 2]
    holidays = get_all_holidays(years)
    count = 0

    for h_data in holidays:
        existing = db.query_first(
            CourtHoliday,
            filters={
                "jurisdiction": h_data["jurisdiction"],
                "holiday_date": h_data["holiday_date"],
            },
        )
        if existing:
            continue

        holiday = CourtHoliday(
            tenant_id=tenant_id,
            jurisdiction=h_data["jurisdiction"],
            holiday_date=h_data["holiday_date"],
            holiday_name=h_data["holiday_name"],
            year=h_data["year"],
            is_active=True,
        )
        db.add(holiday)
        count += 1

    db.flush()
    logger.info(f"Seeded {count} court holidays for {years}")
    return count


def seed_ai_rules(tenant_id: str, db) -> int:
    """Seed court AI certification rules (E.D. Tex. + Denton County)."""
    from core.audit import write_audit
    from modules.court.models import CourtAIRule
    from modules.court.services.ai_certification import get_seed_ai_rules

    rules = get_seed_ai_rules()
    count = 0

    for rule_data in rules:
        existing = db.query_first(
            CourtAIRule,
            filters={
                "court_name": rule_data["court_name"],
                "order_date": rule_data["order_date"],
            },
        )
        if existing:
            logger.debug(f"AI rule already exists: {rule_data['court_name']}")
            continue

        rule = CourtAIRule(
            tenant_id=tenant_id,
            **rule_data,
            is_active=True,
        )
        db.add(rule)
        db.flush()

        write_audit(
            tenant_id=tenant_id,
            table_name="court_ai_rules",
            record_id=rule.id,
            action="seed",
            details={"court_name": rule_data["court_name"], "order_date": str(rule_data["order_date"])},
        )
        count += 1

    db.flush()
    logger.info(f"Seeded {count} court AI rules")
    return count


def main():
    parser = argparse.ArgumentParser(description="Seed court & calendar data")
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
    args = parser.parse_args()

    from core.db.tenant_session import get_tenant_session

    db = get_tenant_session(args.tenant_id)

    try:
        rules_count = seed_rules(args.tenant_id, db)
        holidays_count = seed_holidays(args.tenant_id, db)
        ai_rules_count = seed_ai_rules(args.tenant_id, db)
        db.commit()

        logger.info(
            f"Seed complete: {rules_count} rules, "
            f"{holidays_count} holidays, {ai_rules_count} AI rules"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Seed failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
