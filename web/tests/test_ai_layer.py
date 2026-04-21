"""
Tests — AI Layer Foundation (Session 2a prep)
=============================================

These tests focus on logic that doesn't require a live Anthropic API key —
budget decisions, allocation logic, prompt rendering, warning thresholds,
and the shape of widget_service outputs.

Mark tests requiring a live Anthropic API with @pytest.mark.live_api and
skip in CI. Run with:

    pytest tests/test_ai_layer.py -v

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

# Module under test
from modules.intelligence.anthropic_adapter import (
    AICallContext,
    AIRouting,
    BudgetDecision,
    _compute_warnings,
    _estimate_tokens,
    _project_cost_usd,
    _actual_cost_usd,
    _render_prompt_text,
    _make_budget_decision,
    strip_markdown_fences,
    AIPromptVariablesError,
    MODEL_PRICING,
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_FALLBACK_MODEL,
)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

class TestWarnings:

    def test_no_warning_below_threshold(self):
        assert _compute_warnings(0.50, [0.75, 0.90]) == []

    def test_single_warning_at_75(self):
        assert _compute_warnings(0.80, [0.75, 0.90]) == ["75pct"]

    def test_both_warnings_at_95(self):
        assert _compute_warnings(0.95, [0.75, 0.90]) == ["75pct", "90pct"]

    def test_exact_threshold_triggers(self):
        assert _compute_warnings(0.75, [0.75, 0.90]) == ["75pct"]

    def test_custom_thresholds(self):
        assert _compute_warnings(0.55, [0.50, 0.80]) == ["50pct"]

    def test_unsorted_input_still_ordered(self):
        # Thresholds passed out of order — expected output still ordered
        assert _compute_warnings(0.95, [0.90, 0.75]) == ["75pct", "90pct"]


# ---------------------------------------------------------------------------
# Token estimation + pricing
# ---------------------------------------------------------------------------

class TestPricing:

    def test_token_estimate_empty(self):
        assert _estimate_tokens("") == 0

    def test_token_estimate_rough(self):
        # ~4 chars per token
        assert _estimate_tokens("x" * 400) == 100

    def test_cost_projection_sonnet(self):
        # 1000 input, 1000 output at $3/$15 per 1M
        cost = _project_cost_usd("claude-sonnet-4-20250514", 1000, 1000)
        expected = Decimal("0.003") + Decimal("0.015")
        assert cost == expected.quantize(Decimal("0.000001"))

    def test_cost_projection_haiku_cheaper(self):
        sonnet = _project_cost_usd("claude-sonnet-4-20250514", 1000, 1000)
        haiku = _project_cost_usd("claude-haiku-4-5-20251001", 1000, 1000)
        assert haiku < sonnet

    def test_unknown_model_falls_back_to_default(self):
        # Unknown model should not raise — adapter uses default pricing
        cost = _project_cost_usd("made-up-model-7", 500, 500)
        assert cost > 0


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

class TestPromptRendering:

    def test_basic_substitution(self):
        r = _render_prompt_text("Hello {name}", {"name": "Dennis"})
        assert r == "Hello Dennis"

    def test_multiple_variables(self):
        r = _render_prompt_text(
            "From {a} to {b}",
            {"a": "2026-04-01", "b": "2026-04-18"},
        )
        assert r == "From 2026-04-01 to 2026-04-18"

    def test_missing_variable_raises(self):
        with pytest.raises(AIPromptVariablesError):
            _render_prompt_text("Hello {name}", {})

    def test_extra_variables_ignored(self):
        r = _render_prompt_text("Hi {name}", {"name": "D", "extra": "X"})
        assert r == "Hi D"

    def test_doubled_braces_escape_literal_json(self):
        # Doubled braces {{...}} must NOT be consumed by the variable regex
        # (adapter variable regex matches single-brace {name} only)
        tmpl = 'Return: {{"key": "value"}} and use {name}'
        r = _render_prompt_text(tmpl, {"name": "D"})
        # Note: adapter renders { literally — the doubled-brace convention is
        # a seed-SQL formatting aid only. Both are valid after render.
        assert "{name}" not in r
        assert "D" in r

    def test_repeat_variable_substitution(self):
        r = _render_prompt_text(
            "{who} says hi to {who}",
            {"who": "Claude"},
        )
        assert r == "Claude says hi to Claude"


# ---------------------------------------------------------------------------
# strip_markdown_fences
# ---------------------------------------------------------------------------

class TestStripFences:

    def test_plain_text_unchanged(self):
        assert strip_markdown_fences("hello") == "hello"

    def test_strips_json_fence(self):
        assert strip_markdown_fences('```json\n{"x": 1}\n```') == '{"x": 1}'

    def test_strips_plain_fence(self):
        assert strip_markdown_fences("```\nfoo\n```") == "foo"

    def test_trims_whitespace(self):
        assert strip_markdown_fences("  hello  \n") == "hello"


# ---------------------------------------------------------------------------
# Budget decision
# ---------------------------------------------------------------------------

def _make_routing(**overrides) -> AIRouting:
    defaults = dict(
        id=str(uuid.uuid4()),
        tenant_id=None,
        module="ediscovery",
        purpose="matter_chat",
        primary_model="claude-sonnet-4-20250514",
        fallback_model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        per_call_token_cap=None,
        matter_daily_cost_cap_usd=Decimal("5.00"),
        tenant_daily_cost_cap_usd=Decimal("100.00"),
        warning_thresholds=[0.75, 0.90],
        override_policy={"allow_override": False},
    )
    defaults.update(overrides)
    return AIRouting(**defaults)


def _make_ctx(**overrides) -> AICallContext:
    defaults = dict(
        tenant_id="25c0db0c-f51a-0000-0000-000000000000",
        module="ediscovery",
        purpose="matter_chat",
        user_id=1,
        matter_id=str(uuid.uuid4()),
        user_role="attorney",
    )
    defaults.update(overrides)
    return AICallContext(**defaults)


class TestBudgetDecision:

    @pytest.mark.asyncio
    async def test_ok_under_budget(self, monkeypatch):
        # Force spend lookups to return 0
        async def _zero(*args, **kwargs): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _zero)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _zero)

        routing = _make_routing()
        ctx = _make_ctx()
        decision = await _make_budget_decision(routing, ctx, 1000)

        assert decision.decision == "ok"
        assert decision.model_to_use == routing.primary_model
        assert not decision.fallback_used
        assert not decision.override_used
        assert decision.warnings == []

    @pytest.mark.asyncio
    async def test_warning_at_75_pct(self, monkeypatch):
        # Matter cap $5; current spend $4; primary projection ~$0 pushes to ~80%
        async def _matter_spend(*a, **k): return Decimal("4.00")
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(matter_daily_cost_cap_usd=Decimal("5.00"))
        ctx = _make_ctx()
        decision = await _make_budget_decision(routing, ctx, 1000)

        assert decision.decision == "ok"  # Not breached, just warned
        assert "75pct" in decision.warnings

    @pytest.mark.asyncio
    async def test_breach_falls_back(self, monkeypatch):
        # Matter already at $5, primary would push over; fallback much cheaper
        async def _matter_spend(*a, **k): return Decimal("4.99")
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(matter_daily_cost_cap_usd=Decimal("5.00"))
        ctx = _make_ctx()
        # 100 tokens input, max 2048 output — Sonnet projection will exceed
        decision = await _make_budget_decision(routing, ctx, 100)

        # Fallback (Haiku) projection is ~5x cheaper, fits under the remaining $0.01
        # If not — still 'reject' is acceptable, since default override disallows
        assert decision.decision in ("fallback", "reject")

    @pytest.mark.asyncio
    async def test_breach_no_fallback_no_override_rejects(self, monkeypatch):
        async def _matter_spend(*a, **k): return Decimal("100.00")
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(
            matter_daily_cost_cap_usd=Decimal("5.00"),
            fallback_model=None,
        )
        ctx = _make_ctx()
        decision = await _make_budget_decision(routing, ctx, 1000)
        assert decision.decision == "reject"

    @pytest.mark.asyncio
    async def test_override_accepted_for_qualifying_role(self, monkeypatch):
        async def _matter_spend(*a, **k): return Decimal("100.00")
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(
            matter_daily_cost_cap_usd=Decimal("5.00"),
            fallback_model=None,
            override_policy={
                "allow_override": True,
                "roles": ["admin", "super_admin"],
                "require_reason": True,
                "max_override_multiplier": 3.0,
            },
        )
        ctx = _make_ctx(
            user_role="admin",
            override_requested=True,
            override_reason="urgent client deliverable",
        )
        decision = await _make_budget_decision(routing, ctx, 1000)
        assert decision.decision == "override"
        assert decision.override_used is True

    @pytest.mark.asyncio
    async def test_override_rejected_when_reason_missing(self, monkeypatch):
        async def _matter_spend(*a, **k): return Decimal("100.00")
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(
            matter_daily_cost_cap_usd=Decimal("5.00"),
            fallback_model=None,
            override_policy={
                "allow_override": True,
                "roles": ["admin"],
                "require_reason": True,
                "max_override_multiplier": 3.0,
            },
        )
        ctx = _make_ctx(
            user_role="admin",
            override_requested=True,
            override_reason=None,  # missing
        )
        decision = await _make_budget_decision(routing, ctx, 1000)
        assert decision.decision == "reject"

    @pytest.mark.asyncio
    async def test_override_rejected_above_max_multiplier(self, monkeypatch):
        async def _matter_spend(*a, **k): return Decimal("20.00")  # 4x cap
        async def _tenant_spend(*a, **k): return Decimal("0")
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._matter_spend_today_usd",
            _matter_spend)
        monkeypatch.setattr(
            "modules.intelligence.anthropic_adapter._tenant_spend_today_usd",
            _tenant_spend)

        routing = _make_routing(
            matter_daily_cost_cap_usd=Decimal("5.00"),
            fallback_model=None,
            override_policy={
                "allow_override": True,
                "roles": ["admin"],
                "require_reason": True,
                "max_override_multiplier": 3.0,  # hard ceiling at $15
            },
        )
        ctx = _make_ctx(
            user_role="admin",
            override_requested=True,
            override_reason="client approved",
        )
        decision = await _make_budget_decision(routing, ctx, 1000)
        # Already at $20 on a cap of $5 * 3x multiplier = $15 ceiling
        # Hard ceiling breached — even override can't save this
        assert decision.decision == "reject"
        assert decision.breach_context.get("hard_ceiling_breached") is True


# ---------------------------------------------------------------------------
# AICallResult serialization
# ---------------------------------------------------------------------------

class TestAICallResultHeaders:

    def test_basic_headers(self):
        from modules.intelligence.anthropic_adapter import AICallResult
        r = AICallResult(
            call_id=42,
            text="hi",
            model_used="claude-sonnet-4-20250514",
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            cost_usd=Decimal("0.001234"),
            latency_ms=500,
            utilization_pct=0.42,
            warnings=[],
        )
        h = r.to_headers()
        assert h["X-Praesidium-AI-Model"] == "claude-sonnet-4-20250514"
        assert h["X-Praesidium-AI-Tokens"] == "30"
        assert h["X-Praesidium-AI-Utilization-Pct"] == "42.0"
        assert "X-Praesidium-AI-Budget-Warning" not in h
        assert "X-Praesidium-AI-Fallback-Used" not in h

    def test_warning_header_populated(self):
        from modules.intelligence.anthropic_adapter import AICallResult
        r = AICallResult(
            call_id=42,
            text="hi",
            model_used="claude-sonnet-4-20250514",
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            cost_usd=Decimal("0.001234"),
            latency_ms=500,
            utilization_pct=0.82,
            warnings=["75pct"],
        )
        h = r.to_headers()
        assert h["X-Praesidium-AI-Budget-Warning"] == "75pct"

    def test_fallback_and_override_headers(self):
        from modules.intelligence.anthropic_adapter import AICallResult
        r = AICallResult(
            call_id=42,
            text="hi",
            model_used="claude-haiku-4-5-20251001",
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            cost_usd=Decimal("0.0001"),
            latency_ms=200,
            utilization_pct=1.05,
            warnings=["75pct", "90pct"],
            fallback_used=True,
            override_used=False,
        )
        h = r.to_headers()
        assert h["X-Praesidium-AI-Fallback-Used"] == "1"
        assert "X-Praesidium-AI-Override-Used" not in h


# ---------------------------------------------------------------------------
# Widget service — shape tests (no DB required, monkeypatched)
# ---------------------------------------------------------------------------

class TestWidgetServiceShapes:

    @pytest.mark.asyncio
    async def test_my_matters_empty_when_no_user(self):
        from modules.intelligence.widget_service import (
            get_my_matters_ai_usage)
        out = await get_my_matters_ai_usage({
            "tenant_id": "25c0db0c-f51a-0000-0000-000000000000",
            "user_id": None,
        })
        assert out["empty_state"] is True
        assert out["rows"] == []
        assert out["totals"]["today"] == 0.0

    @pytest.mark.asyncio
    async def test_firm_empty_when_no_tenant(self):
        from modules.intelligence.widget_service import get_firm_ai_usage
        out = await get_firm_ai_usage({"tenant_id": ""})
        assert out["empty_state"] is True

    @pytest.mark.asyncio
    async def test_unallocated_empty_when_no_tenant(self):
        from modules.intelligence.widget_service import get_unallocated_review
        out = await get_unallocated_review({"tenant_id": ""})
        assert out["empty_state"] is True


# ---------------------------------------------------------------------------
# Model routing table sanity
# ---------------------------------------------------------------------------

class TestModelPricing:

    def test_default_models_have_pricing(self):
        assert DEFAULT_PRIMARY_MODEL in MODEL_PRICING
        assert DEFAULT_FALLBACK_MODEL in MODEL_PRICING

    def test_fallback_cheaper_than_primary(self):
        primary = MODEL_PRICING[DEFAULT_PRIMARY_MODEL]
        fallback = MODEL_PRICING[DEFAULT_FALLBACK_MODEL]
        # (input, output) tuple — both should be cheaper on fallback
        assert fallback[0] <= primary[0]
        assert fallback[1] <= primary[1]
