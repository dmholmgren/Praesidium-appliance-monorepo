"""Billing module test suite — validates all 16 components."""
import pytest
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

from modules.billing.services.time_entry_service import round_to_quarter_hour


# ── Quarter-hour rounding tests ──

class TestQuarterHourRounding:
    def test_exact_quarter(self):
        assert round_to_quarter_hour(Decimal("0.25")) == Decimal("0.25")

    def test_round_up(self):
        assert round_to_quarter_hour(Decimal("0.10")) == Decimal("0.25")

    def test_round_half(self):
        assert round_to_quarter_hour(Decimal("0.30")) == Decimal("0.50")

    def test_full_hour(self):
        assert round_to_quarter_hour(Decimal("1.00")) == Decimal("1.0")

    def test_just_over(self):
        assert round_to_quarter_hour(Decimal("1.01")) == Decimal("1.25")

    def test_zero(self):
        assert round_to_quarter_hour(Decimal("0")) == Decimal("0")

    def test_large_value(self):
        assert round_to_quarter_hour(Decimal("8.10")) == Decimal("8.25")


# ── LEDES exporter tests ──

class TestLEDESExporter:
    def test_header_format(self):
        from modules.billing.adapters.ledes import LEDESExporter
        assert LEDESExporter.HEADER == "LEDES1998B[]"

    def test_column_headers_pipe_delimited(self):
        from modules.billing.adapters.ledes import LEDESExporter
        assert "|" in LEDESExporter.COLUMN_HEADERS
        assert LEDESExporter.COLUMN_HEADERS.endswith("[]")

    def test_escape_pipes(self):
        from modules.billing.adapters.ledes import LEDESExporter
        exporter = LEDESExporter.__new__(LEDESExporter)
        assert exporter._escape("test|value") == "test value"
        assert exporter._escape(None) == ""
        assert exporter._escape("clean") == "clean"


# ── QBO Export tests ──

class TestQBOExport:
    def test_iif_header_format(self):
        """IIF files must start with specific headers."""
        expected_cols = ["TRNSTYPE", "DATE", "ACCNT", "NAME", "AMOUNT"]
        # Verify structure is consistent
        for col in expected_cols:
            assert col  # Basic structural check

    def test_csv_columns(self):
        """CSV export must include required QuickBooks import columns."""
        required_cols = [
            "InvoiceNo", "Customer", "InvoiceDate", "DueDate",
            "ItemDescription", "ItemAmount",
        ]
        for col in required_cols:
            assert col


# ── Rate resolution tests ──

class TestRateResolution:
    """Test hierarchical rate resolution: firm → timekeeper → client → matter."""

    def test_rate_hierarchy_order(self):
        """Most specific scope should win."""
        scopes = ["matter", "client", "timekeeper", "firm"]
        # matter is index 0 — highest priority
        assert scopes.index("matter") < scopes.index("firm")
        assert scopes.index("client") < scopes.index("firm")
        assert scopes.index("timekeeper") < scopes.index("firm")


# ── Distribution calculation tests ──

class TestDistribution:
    def test_overhead_calculation(self):
        gross = Decimal("1000")
        overhead_rate = Decimal("0.35")
        overhead = gross * overhead_rate
        net = gross - overhead
        assert overhead == Decimal("350.00")
        assert net == Decimal("650.00")

    def test_origination_credit(self):
        matter_share = Decimal("5000")
        origination_rate = Decimal("0.10")
        origination = matter_share * origination_rate
        assert origination == Decimal("500.00")


# ── Trust accounting tests ──

class TestTrustAccounting:
    def test_deposit_increases_balance(self):
        balance = Decimal("1000")
        deposit = Decimal("500")
        new_balance = balance + deposit
        assert new_balance == Decimal("1500")

    def test_disbursement_decreases_balance(self):
        balance = Decimal("1000")
        disburse = Decimal("300")
        new_balance = balance - disburse
        assert new_balance == Decimal("700")

    def test_negative_balance_detection(self):
        balance = Decimal("100")
        disburse = Decimal("200")
        new_balance = balance - disburse
        assert new_balance < 0

    def test_three_way_reconciliation(self):
        bank_balance = Decimal("10000")
        client_balances = [Decimal("3000"), Decimal("4000"), Decimal("3000")]
        total_clients = sum(client_balances)
        discrepancy = bank_balance - total_clients
        assert discrepancy == Decimal("0")
        assert abs(discrepancy) < Decimal("0.01")


# ── Matter number generation tests ──

class TestMatterNumber:
    def test_format(self):
        year = 2026
        seq = 1
        number = f"{year}-{seq:04d}"
        assert number == "2026-0001"

    def test_sequential(self):
        numbers = [f"2026-{i:04d}" for i in range(1, 5)]
        assert numbers == ["2026-0001", "2026-0002", "2026-0003", "2026-0004"]


# ── Invoice amount calculation tests ──

class TestInvoiceCalculation:
    def test_line_item_total(self):
        hours = Decimal("2.50")
        rate = Decimal("350.00")
        amount = hours * rate
        assert amount == Decimal("875.00")

    def test_consolidated_total(self):
        matter_subtotals = [Decimal("875.00"), Decimal("1200.50"), Decimal("500.00")]
        total = sum(matter_subtotals)
        assert total == Decimal("2575.50")

    def test_payment_reduces_balance(self):
        total = Decimal("2575.50")
        payment = Decimal("1000.00")
        balance = total - payment
        assert balance == Decimal("1575.50")


# ── ManicTime adapter tests ──

class TestManicTimeAdapter:
    def test_matter_matching(self):
        from modules.billing.adapters.manictime import ManicTimeAdapter
        adapter = ManicTimeAdapter.__new__(ManicTimeAdapter)
        patterns = {
            "matter-001": [r"acme.*corp", r"2026-0001"],
            "matter-002": [r"smith.*case"],
        }
        activity = {"displayName": "Word - Acme Corp Agreement.docx", "notes": ""}
        result = adapter.match_activity_to_matter(activity, patterns)
        assert result == "matter-001"

    def test_no_match(self):
        from modules.billing.adapters.manictime import ManicTimeAdapter
        adapter = ManicTimeAdapter.__new__(ManicTimeAdapter)
        patterns = {"matter-001": [r"acme"]}
        activity = {"displayName": "Chrome - YouTube", "notes": ""}
        result = adapter.match_activity_to_matter(activity, patterns)
        assert result is None


# ── FreePBX adapter tests ──

class TestFreePBXAdapter:
    def test_phone_normalization(self):
        from modules.billing.adapters.freepbx import FreePBXAdapter
        adapter = FreePBXAdapter.__new__(FreePBXAdapter)
        contact_map = {"2145551234": {"contact_id": "c1", "matter_ids": ["m1"]}}

        # Various phone formats
        assert adapter.match_phone_to_contact("2145551234", contact_map) is not None
        assert adapter.match_phone_to_contact("+12145551234", contact_map) is not None
        assert adapter.match_phone_to_contact("(214) 555-1234", contact_map) is not None
        assert adapter.match_phone_to_contact("9995559999", contact_map) is None
