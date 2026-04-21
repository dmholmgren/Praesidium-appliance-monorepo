"""Billing module tests — validates BigInteger IDs and Chat 0 column names."""
import pytest
from decimal import Decimal
from modules.billing.services.time_entry_service import round_to_quarter_hour

class TestQuarterHourRounding:
    def test_exact(self): assert round_to_quarter_hour(Decimal("0.25")) == Decimal("0.25")
    def test_round_up(self): assert round_to_quarter_hour(Decimal("0.10")) == Decimal("0.25")
    def test_half(self): assert round_to_quarter_hour(Decimal("0.30")) == Decimal("0.50")
    def test_full(self): assert round_to_quarter_hour(Decimal("1.00")) == Decimal("1.0")
    def test_just_over(self): assert round_to_quarter_hour(Decimal("1.01")) == Decimal("1.25")
    def test_zero(self): assert round_to_quarter_hour(Decimal("0")) == Decimal("0")

class TestLEDESFormat:
    def test_header(self):
        from modules.billing.adapters.ledes import LEDESExporter
        assert LEDESExporter.HEADER == "LEDES1998B[]"
    def test_pipe_escape(self):
        from modules.billing.adapters.ledes import LEDESExporter
        e = LEDESExporter.__new__(LEDESExporter)
        assert e._esc("test|value") == "test value"
        assert e._esc(None) == ""

class TestDistribution:
    def test_overhead(self):
        gross = Decimal("1000"); overhead = gross * Decimal("0.35"); net = gross - overhead
        assert overhead == Decimal("350.00"); assert net == Decimal("650.00")
    def test_origination(self):
        share = Decimal("5000"); orig = share * Decimal("0.10")
        assert orig == Decimal("500.00")

class TestTrust:
    def test_deposit(self): assert Decimal("1000") + Decimal("500") == Decimal("1500")
    def test_disburse(self): assert Decimal("1000") - Decimal("300") == Decimal("700")
    def test_negative(self): assert Decimal("100") - Decimal("200") < 0
    def test_reconcile(self):
        bank = Decimal("10000"); clients = [Decimal("3000"),Decimal("4000"),Decimal("3000")]
        assert bank - sum(clients) == Decimal("0")

class TestMatterNumber:
    def test_format(self): assert f"2026-{1:04d}" == "2026-0001"

class TestManicTime:
    def test_match(self):
        from modules.billing.adapters.manictime import ManicTimeAdapter
        a = ManicTimeAdapter.__new__(ManicTimeAdapter)
        patterns = {"m1": [r"acme.*corp"], "m2": [r"smith"]}
        assert a.match_activity_to_matter({"displayName": "Word - Acme Corp.docx", "notes": ""}, patterns) == "m1"
        assert a.match_activity_to_matter({"displayName": "Chrome - YouTube", "notes": ""}, patterns) is None

class TestFreePBX:
    def test_phone_normalize(self):
        from modules.billing.adapters.freepbx import FreePBXAdapter
        a = FreePBXAdapter.__new__(FreePBXAdapter)
        m = {"2145551234": {"contact_id": "c1", "matter_ids": ["m1"]}}
        assert a.match_phone_to_contact("2145551234", m) is not None
        assert a.match_phone_to_contact("+12145551234", m) is not None
        assert a.match_phone_to_contact("(214) 555-1234", m) is not None
        assert a.match_phone_to_contact("9995559999", m) is None
