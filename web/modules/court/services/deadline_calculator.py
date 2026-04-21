"""
COMP 4 — Deadline Calculator Engine.

Computes derivative deadline chains from confirmed anchor dates:
  1. Query court_rules for matching triggering_event + jurisdiction
  2. Compute deadline date (calendar or business days)
  3. Adjust for weekends and holidays
  4. Apply method-of-service adjustments
  5. Recursively compute derivative deadlines from each computed date
  6. Return complete chain with rule bases and derivation paths

Runs as RQ job on PROC-01. Never blocks HTTP handlers.
All DB access via TenantSession. All writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from core.audit import write_audit
from core.db.base import TenantSession
from modules.court.models import CourtHoliday, CourtRule, Deadline

logger = logging.getLogger(__name__)

# Maximum recursion depth for derivative chains (safety limit)
MAX_CHAIN_DEPTH = 10


class DeadlineCalculator:
    """
    Deadline calculation engine.

    Computes complete deadline chains given an anchor date, jurisdiction,
    and triggering event. Handles calendar/business day counting, weekend
    and holiday adjustment, service-method extensions, and recursive
    derivative chain computation.
    """

    def __init__(self, tenant_id: str, db: TenantSession):
        self.tenant_id = tenant_id
        self.db = db
        self._holiday_cache: dict[str, set[date]] = {}
        self._rules_cache: dict[str, list[CourtRule]] = {}

    def _load_holidays(self, jurisdiction: str) -> set[date]:
        """Load and cache holidays for a jurisdiction."""
        cache_key = jurisdiction
        if cache_key not in self._holiday_cache:
            holidays = self.db.query_all(
                CourtHoliday,
                filters={"jurisdiction": jurisdiction, "is_active": True},
            )
            self._holiday_cache[cache_key] = {h.holiday_date for h in holidays}

            # Always include federal holidays as a base for state courts
            if jurisdiction != "federal":
                fed_holidays = self.db.query_all(
                    CourtHoliday,
                    filters={"jurisdiction": "federal", "is_active": True},
                )
                self._holiday_cache[cache_key].update(h.holiday_date for h in fed_holidays)

        return self._holiday_cache[cache_key]

    def _load_rules(self, triggering_event: str, jurisdiction: str) -> list[CourtRule]:
        """
        Load applicable rules for a triggering event.

        Resolution order:
        1. Local rules (court_district-specific) override everything
        2. State delta rules override federal
        3. Federal FRCP is the base layer
        """
        cache_key = f"{triggering_event}:{jurisdiction}"
        if cache_key in self._rules_cache:
            return self._rules_cache[cache_key]

        # Get federal rules
        federal_rules = self.db.query_all(
            CourtRule,
            filters={
                "triggering_event": triggering_event,
                "jurisdiction": "federal",
                "is_active": True,
            },
        )

        # Get state delta rules
        state_rules = []
        if jurisdiction != "federal":
            state_rules = self.db.query_all(
                CourtRule,
                filters={
                    "triggering_event": triggering_event,
                    "jurisdiction": jurisdiction,
                    "is_active": True,
                },
            )

        # Delta resolution: state rules override federal where is_delta=True
        result = list(federal_rules)
        for state_rule in state_rules:
            if state_rule.is_delta and state_rule.overrides_rule_id:
                result = [r for r in result if r.id != state_rule.overrides_rule_id]
            result.append(state_rule)

        self._rules_cache[cache_key] = result
        return result

    def is_court_day(self, d: date, jurisdiction: str) -> bool:
        """Check if a date is a valid court day (not weekend or holiday)."""
        if d.weekday() >= 5:  # Saturday or Sunday
            return False
        holidays = self._load_holidays(jurisdiction)
        return d not in holidays

    def next_court_day(self, d: date, jurisdiction: str) -> date:
        """Advance to the next valid court day if needed."""
        while not self.is_court_day(d, jurisdiction):
            d += timedelta(days=1)
        return d

    def compute_deadline_date(
        self,
        anchor: date,
        duration_days: int,
        duration_type: str,
        direction: str,
        jurisdiction: str,
        service_method: Optional[str] = None,
        service_adjustments: Optional[dict] = None,
    ) -> tuple[date, int]:
        """
        Compute a single deadline date from an anchor.

        Returns (deadline_date, service_adjustment_days).
        """
        # Determine direction multiplier
        step = 1 if direction == "after" else -1

        if duration_type == "business":
            # Count only business days
            current = anchor
            days_counted = 0
            while days_counted < duration_days:
                current += timedelta(days=step)
                if self.is_court_day(current, jurisdiction):
                    days_counted += 1
            computed = current
        else:
            # Calendar days
            computed = anchor + timedelta(days=duration_days * step)

        # Adjust to next court day if computed falls on non-court day
        computed = self.next_court_day(computed, jurisdiction)

        # Apply method-of-service adjustment
        service_adj_days = 0
        if service_method and service_adjustments:
            service_adj_days = service_adjustments.get(service_method, 0)
            if service_adj_days > 0:
                computed += timedelta(days=service_adj_days)
                computed = self.next_court_day(computed, jurisdiction)

        return computed, service_adj_days

    def calculate_chain(
        self,
        matter_id: int,
        anchor_date: date,
        anchor_description: str,
        triggering_event: str,
        jurisdiction: str,
        service_method: Optional[str] = None,
        scheduling_order_id: Optional[int] = None,
        scheduling_order_date_id: Optional[int] = None,
        parent_deadline_id: Optional[int] = None,
        depth: int = 0,
    ) -> list[Deadline]:
        """
        Compute a complete derivative deadline chain.

        Recursively follows triggered rules until no more derivatives
        are found or MAX_CHAIN_DEPTH is reached.

        Returns list of Deadline model instances (not yet committed).
        """
        if depth >= MAX_CHAIN_DEPTH:
            logger.warning(f"Chain depth limit reached for matter {matter_id}, event {triggering_event}")
            return []

        rules = self._load_rules(triggering_event, jurisdiction)
        if not rules:
            return []

        all_deadlines = []

        for rule in rules:
            computed_date, service_adj = self.compute_deadline_date(
                anchor=anchor_date,
                duration_days=rule.duration_days,
                duration_type=rule.duration_type,
                direction=rule.direction,
                jurisdiction=jurisdiction,
                service_method=service_method,
                service_adjustments=rule.service_method_adjustments,
            )

            derivation = {
                "anchor_date": anchor_date.isoformat(),
                "anchor_description": anchor_description,
                "rule_id": rule.id,
                "rule_number": rule.rule_number,
                "rule_title": rule.rule_title,
                "duration_days": rule.duration_days,
                "duration_type": rule.duration_type,
                "direction": rule.direction,
                "service_method": service_method,
                "service_adjustment_days": service_adj,
                "depth": depth,
            }

            deadline = Deadline(
                tenant_id=self.tenant_id,
                matter_id=matter_id,
                rule_id=rule.id,
                anchor_date=anchor_date,
                anchor_description=anchor_description,
                deadline_date=computed_date,
                deadline_description=rule.deadline_description,
                derivation_path=derivation,
                priority="high" if depth == 0 else "normal",
                status="active",
                scheduling_order_id=scheduling_order_id,
                scheduling_order_date_id=scheduling_order_date_id,
                parent_deadline_id=parent_deadline_id,
                service_method=service_method,
                service_adjustment_days=service_adj,
            )

            self.db.add(deadline)
            self.db.flush()  # Get the ID for derivative chain parent tracking

            write_audit(
                tenant_id=self.tenant_id,
                table_name="deadlines",
                record_id=deadline.id,
                action="create",
                details={
                    "rule_number": rule.rule_number,
                    "anchor_date": anchor_date.isoformat(),
                    "computed_date": computed_date.isoformat(),
                    "chain_depth": depth,
                },
            )

            all_deadlines.append(deadline)

            # Recursively compute derivative deadlines
            if rule.triggers_rules:
                for triggered_event in rule.triggers_rules:
                    derivatives = self.calculate_chain(
                        matter_id=matter_id,
                        anchor_date=computed_date,
                        anchor_description=f"Derivative of: {rule.deadline_description}",
                        triggering_event=triggered_event,
                        jurisdiction=jurisdiction,
                        service_method=service_method,
                        scheduling_order_id=scheduling_order_id,
                        scheduling_order_date_id=scheduling_order_date_id,
                        parent_deadline_id=deadline.id,
                        depth=depth + 1,
                    )
                    all_deadlines.extend(derivatives)

        logger.info(
            f"Calculated {len(all_deadlines)} deadlines for matter {matter_id}, "
            f"event={triggering_event}, jurisdiction={jurisdiction}, depth={depth}"
        )
        return all_deadlines
