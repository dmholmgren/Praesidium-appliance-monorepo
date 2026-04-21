"""
COMP 3 — Rules Database Seed.

FRCP master ruleset (base layer for all federal deadlines).
State delta files: TRCP (TX), CCP (CA), CRCP (CO).
Federal and state holidays.

Architecture: FRCP is the master. State rules are delta files — only what
differs from FRCP. Post-2021 Texas harmonization means minimal TRCP deltas.

Loaded via: python -m modules.court.seed_data.rules_seed --tenant-id <UUID>
"""

from __future__ import annotations

from datetime import date

# ──────────────────────────────────────────────────────────────
# FRCP MASTER RULES — Base layer for all federal calculations
# ──────────────────────────────────────────────────────────────

FRCP_RULES = [
    # Answer / Response deadlines
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 12(a)(1)(A)(i)",
        "rule_title": "Answer to Complaint",
        "triggering_event": "service_of_complaint",
        "deadline_description": "Answer or responsive pleading due",
        "duration_days": 21,
        "duration_type": "calendar",
        "direction": "after",
        "service_method_adjustments": {"mail": 3, "electronic": 3, "other": 3},
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 12(a)(1)(A)(ii)",
        "rule_title": "Answer after Waiver of Service",
        "triggering_event": "waiver_of_service",
        "deadline_description": "Answer due after waiver of service (domestic)",
        "duration_days": 60,
        "duration_type": "calendar",
        "direction": "after",
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 12(a)(4)(A)",
        "rule_title": "Answer after Motion to Dismiss Denied",
        "triggering_event": "motion_to_dismiss_denied",
        "deadline_description": "Answer due after denial of Rule 12 motion",
        "duration_days": 14,
        "duration_type": "calendar",
        "direction": "after",
    },
    # Discovery deadlines
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 26(a)(1)",
        "rule_title": "Initial Disclosures",
        "triggering_event": "rule_26f_conference",
        "deadline_description": "Initial disclosures due",
        "duration_days": 14,
        "duration_type": "calendar",
        "direction": "after",
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 26(f)",
        "rule_title": "Discovery Planning Conference",
        "triggering_event": "scheduling_conference",
        "deadline_description": "Parties must confer about discovery plan",
        "duration_days": 21,
        "duration_type": "calendar",
        "direction": "before",
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 33(b)(2)",
        "rule_title": "Interrogatory Response",
        "triggering_event": "interrogatories_served",
        "deadline_description": "Responses to interrogatories due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "service_method_adjustments": {"mail": 3, "electronic": 3},
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 34(b)(2)(A)",
        "rule_title": "Document Production Response",
        "triggering_event": "production_request_served",
        "deadline_description": "Response to document production request due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "service_method_adjustments": {"mail": 3, "electronic": 3},
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 36(a)(3)",
        "rule_title": "Requests for Admission Response",
        "triggering_event": "rfa_served",
        "deadline_description": "Responses to requests for admission due (deemed admitted if missed)",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "service_method_adjustments": {"mail": 3, "electronic": 3},
    },
    # Motion practice
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 56",
        "rule_title": "Summary Judgment Response",
        "triggering_event": "summary_judgment_filed",
        "deadline_description": "Response to motion for summary judgment due",
        "duration_days": 21,
        "duration_type": "calendar",
        "direction": "after",
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRCP 59(b)",
        "rule_title": "Motion for New Trial",
        "triggering_event": "judgment_entered",
        "deadline_description": "Motion for new trial due",
        "duration_days": 28,
        "duration_type": "calendar",
        "direction": "after",
    },
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "FRAP 4(a)(1)(A)",
        "rule_title": "Notice of Appeal",
        "triggering_event": "judgment_entered",
        "deadline_description": "Notice of appeal due (civil)",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
    },
    # Derivative: Reply brief after response
    {
        "rule_set": "frcp",
        "jurisdiction": "federal",
        "rule_number": "Local (common)",
        "rule_title": "Reply Brief",
        "triggering_event": "motion_response_filed",
        "deadline_description": "Reply brief due",
        "duration_days": 14,
        "duration_type": "calendar",
        "direction": "after",
    },
]


# ──────────────────────────────────────────────────────────────
# TRCP DELTA RULES — Texas (post-2021 harmonization, minimal deltas)
# ──────────────────────────────────────────────────────────────

TRCP_DELTA_RULES = [
    {
        "rule_set": "trcp",
        "jurisdiction": "TX",
        "rule_number": "TRCP 99(b)",
        "rule_title": "Answer to Citation",
        "triggering_event": "service_of_citation",
        "deadline_description": "Answer day (Monday following 20 days after service)",
        "duration_days": 20,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
        "service_method_adjustments": {},
    },
    {
        "rule_set": "trcp",
        "jurisdiction": "TX",
        "rule_number": "TRCP 194.2",
        "rule_title": "Required Disclosures",
        "triggering_event": "answer_filed",
        "deadline_description": "Required disclosures due (TRCP 194 — Texas equivalent of Rule 26)",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "trcp",
        "jurisdiction": "TX",
        "rule_number": "TRCP 196.2(a)",
        "rule_title": "Response to Discovery",
        "triggering_event": "discovery_request_served",
        "deadline_description": "Written discovery responses due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "trcp",
        "jurisdiction": "TX",
        "rule_number": "TRCP 329b(a)",
        "rule_title": "Motion for New Trial",
        "triggering_event": "judgment_signed",
        "deadline_description": "Motion for new trial due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "trcp",
        "jurisdiction": "TX",
        "rule_number": "TRAP 26.1(a)",
        "rule_title": "Notice of Appeal",
        "triggering_event": "judgment_signed",
        "deadline_description": "Notice of appeal due (accelerated to 20 days if certain post-judgment motions overruled)",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
]


# ──────────────────────────────────────────────────────────────
# CCP DELTA RULES — California (more divergent from federal)
# ──────────────────────────────────────────────────────────────

CCP_DELTA_RULES = [
    {
        "rule_set": "ccp",
        "jurisdiction": "CA",
        "rule_number": "CCP 412.20(a)(3)",
        "rule_title": "Answer to Complaint",
        "triggering_event": "service_of_summons",
        "deadline_description": "Answer to complaint due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "ccp",
        "jurisdiction": "CA",
        "rule_number": "CCP 2030.260(a)",
        "rule_title": "Interrogatory Response",
        "triggering_event": "interrogatories_served",
        "deadline_description": "Responses to interrogatories due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
        "service_method_adjustments": {"mail": 5, "electronic": 2},
    },
    {
        "rule_set": "ccp",
        "jurisdiction": "CA",
        "rule_number": "CCP 2031.260(a)",
        "rule_title": "Document Production Response",
        "triggering_event": "production_request_served",
        "deadline_description": "Response to document production demand due",
        "duration_days": 30,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
        "service_method_adjustments": {"mail": 5, "electronic": 2},
    },
    {
        "rule_set": "ccp",
        "jurisdiction": "CA",
        "rule_number": "CCP 659(a)",
        "rule_title": "Motion for New Trial",
        "triggering_event": "notice_of_entry_of_judgment",
        "deadline_description": "Motion for new trial: notice of intention due",
        "duration_days": 15,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "ccp",
        "jurisdiction": "CA",
        "rule_number": "CRC 8.104(a)(1)",
        "rule_title": "Notice of Appeal",
        "triggering_event": "notice_of_entry_of_judgment",
        "deadline_description": "Notice of appeal due (California — 60 days from notice of entry)",
        "duration_days": 60,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
]


# ──────────────────────────────────────────────────────────────
# CRCP DELTA RULES — Colorado
# ──────────────────────────────────────────────────────────────

CRCP_DELTA_RULES = [
    {
        "rule_set": "crcp",
        "jurisdiction": "CO",
        "rule_number": "CRCP 12(a)",
        "rule_title": "Answer to Complaint",
        "triggering_event": "service_of_complaint",
        "deadline_description": "Answer or responsive pleading due (Colorado)",
        "duration_days": 21,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "crcp",
        "jurisdiction": "CO",
        "rule_number": "CRCP 26(a)(1)",
        "rule_title": "Initial Disclosures",
        "triggering_event": "case_management_conference",
        "deadline_description": "Initial disclosures due (Colorado)",
        "duration_days": 14,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
    {
        "rule_set": "crcp",
        "jurisdiction": "CO",
        "rule_number": "CAR 4(a)",
        "rule_title": "Notice of Appeal",
        "triggering_event": "judgment_entered",
        "deadline_description": "Notice of appeal due (Colorado — 49 days)",
        "duration_days": 49,
        "duration_type": "calendar",
        "direction": "after",
        "is_delta": True,
    },
]


# ──────────────────────────────────────────────────────────────
# FEDERAL HOLIDAYS (recurring, generated per year)
# ──────────────────────────────────────────────────────────────

def generate_federal_holidays(year: int) -> list[dict]:
    """Generate federal court holidays for a given year."""
    from datetime import timedelta

    holidays = []

    def _add(name: str, d: date):
        holidays.append({
            "jurisdiction": "federal",
            "holiday_date": d,
            "holiday_name": name,
            "year": year,
        })

    # Fixed-date holidays (observed rules: Sat→Fri, Sun→Mon)
    def _observed(d: date) -> date:
        if d.weekday() == 5:  # Saturday
            return d - timedelta(days=1)
        if d.weekday() == 6:  # Sunday
            return d + timedelta(days=1)
        return d

    _add("New Year's Day", _observed(date(year, 1, 1)))
    _add("Juneteenth", _observed(date(year, 6, 19)))
    _add("Independence Day", _observed(date(year, 7, 4)))
    _add("Veterans Day", _observed(date(year, 11, 11)))
    _add("Christmas Day", _observed(date(year, 12, 25)))

    # Monday-anchored holidays
    # MLK Day: 3rd Monday in January
    jan1 = date(year, 1, 1)
    first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
    _add("MLK Jr. Day", first_monday + timedelta(weeks=2))

    # Presidents' Day: 3rd Monday in February
    feb1 = date(year, 2, 1)
    first_monday = feb1 + timedelta(days=(7 - feb1.weekday()) % 7)
    _add("Presidents' Day", first_monday + timedelta(weeks=2))

    # Memorial Day: last Monday in May
    may31 = date(year, 5, 31)
    _add("Memorial Day", may31 - timedelta(days=(may31.weekday()) % 7))

    # Labor Day: 1st Monday in September
    sep1 = date(year, 9, 1)
    _add("Labor Day", sep1 + timedelta(days=(7 - sep1.weekday()) % 7))

    # Columbus Day: 2nd Monday in October
    oct1 = date(year, 10, 1)
    first_monday = oct1 + timedelta(days=(7 - oct1.weekday()) % 7)
    _add("Columbus Day", first_monday + timedelta(weeks=1))

    # Thanksgiving: 4th Thursday in November
    nov1 = date(year, 11, 1)
    first_thurs = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    _add("Thanksgiving", first_thurs + timedelta(weeks=3))

    return holidays


def generate_texas_holidays(year: int) -> list[dict]:
    """Texas state holidays in addition to federal."""
    holidays = generate_federal_holidays(year)
    for h in holidays:
        h["jurisdiction"] = "TX"

    # Texas-specific additions
    from datetime import timedelta
    # Texas Independence Day (March 2)
    holidays.append({
        "jurisdiction": "TX",
        "holiday_date": date(year, 3, 2),
        "holiday_name": "Texas Independence Day",
        "year": year,
    })
    # San Jacinto Day (April 21)
    holidays.append({
        "jurisdiction": "TX",
        "holiday_date": date(year, 4, 21),
        "holiday_name": "San Jacinto Day",
        "year": year,
    })
    # Day after Thanksgiving
    nov1 = date(year, 11, 1)
    first_thurs = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    thanksgiving = first_thurs + timedelta(weeks=3)
    holidays.append({
        "jurisdiction": "TX",
        "holiday_date": thanksgiving + timedelta(days=1),
        "holiday_name": "Day After Thanksgiving",
        "year": year,
    })
    # Christmas Eve & Day After Christmas
    holidays.append({
        "jurisdiction": "TX",
        "holiday_date": date(year, 12, 24),
        "holiday_name": "Christmas Eve",
        "year": year,
    })
    holidays.append({
        "jurisdiction": "TX",
        "holiday_date": date(year, 12, 26),
        "holiday_name": "Day After Christmas",
        "year": year,
    })

    return holidays


def generate_california_holidays(year: int) -> list[dict]:
    """California state holidays in addition to federal."""
    holidays = generate_federal_holidays(year)
    for h in holidays:
        h["jurisdiction"] = "CA"

    # California-specific: Cesar Chavez Day (March 31)
    holidays.append({
        "jurisdiction": "CA",
        "holiday_date": date(year, 3, 31),
        "holiday_name": "Cesar Chavez Day",
        "year": year,
    })

    return holidays


def generate_colorado_holidays(year: int) -> list[dict]:
    """Colorado state holidays — mostly follows federal."""
    holidays = generate_federal_holidays(year)
    for h in holidays:
        h["jurisdiction"] = "CO"
    return holidays


# ──────────────────────────────────────────────────────────────
# Seed loader
# ──────────────────────────────────────────────────────────────

ALL_RULE_SETS = FRCP_RULES + TRCP_DELTA_RULES + CCP_DELTA_RULES + CRCP_DELTA_RULES


def get_all_rules() -> list[dict]:
    """Return all rules for seeding."""
    return ALL_RULE_SETS


def get_all_holidays(years: list[int]) -> list[dict]:
    """Return all holidays for the given years."""
    holidays = []
    for year in years:
        holidays.extend(generate_federal_holidays(year))
        holidays.extend(generate_texas_holidays(year))
        holidays.extend(generate_california_holidays(year))
        holidays.extend(generate_colorado_holidays(year))
    return holidays
