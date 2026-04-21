"""
Praesidium AI Layer — Anthropic Adapter

The single choke point for every Anthropic API call made by the platform.
Enforces Architectural Constraint #11 (AI operations cost-capped and attributed)
structurally rather than by convention.

Responsibilities
----------------
    1. BYOK key fetch from credentials_vault
    2. Model routing — lookup (module, purpose) in ai_model_routing
    3. Cost cap enforcement — fallback-then-override-then-bill
    4. Warning thresholds — 75% / 90% with header + OOB banner
    5. Prompt template resolution — render from prompt_template_versions
    6. Cost attribution — write row to ai_api_calls on every call
    7. Exception logging — write row to ai_cost_exceptions on any breach
    8. Dual-mode execution — inline (await call()) or enqueued (await enqueue())

Breach flow (Dennis's spec — Apr 18, 2026)
------------------------------------------
    1. projected_cost computed from prompt tokens * rate
    2. if (today_spend + projected) >= matter_cap:
         fallback_model attempted first
    3. if fallback_model also breaches:
         if override_policy allows and user role qualifies:
             use primary model, log exception, mark for matter billing
         else:
             hard reject with AICapBreach exception
    4. warning thresholds: 75% yellow, 90% orange, 100% fallback-or-reject

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, USPTO Reg. No. 54,168
"""

from __future__ import annotations

import os
import json
import uuid
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal
from typing import Any, Optional

import httpx
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_HTTP_TIMEOUT_S = 90.0

# Model pricing — USD per 1M tokens (input, output)
# These are the canonical defaults. A tenant or platform override can place
# pricing in ai_model_routing if Anthropic's pricing changes; the adapter
# always reads from this table as a last resort.
# Source of truth: docs.anthropic.com; confirm on price changes.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":            (15.00, 75.00),
    "claude-opus-4-6":            (15.00, 75.00),
    "claude-sonnet-4-6":           (3.00, 15.00),
    "claude-sonnet-4-20250514":    (3.00, 15.00),
    "claude-haiku-4-5-20251001":   (1.00,  5.00),
    "claude-3-5-sonnet-20241022":  (3.00, 15.00),
    "claude-3-5-haiku-20241022":   (0.80,  4.00),
}

DEFAULT_PRIMARY_MODEL = "claude-sonnet-4-20250514"
DEFAULT_FALLBACK_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AILayerError(Exception):
    """Base class for AI layer errors."""


class AIKeyMissingError(AILayerError):
    """No Anthropic API key in credentials_vault for this tenant."""


class AIRoutingMissingError(AILayerError):
    """No ai_model_routing row for (module, purpose) and no platform default."""


class AIPromptNotFoundError(AILayerError):
    """Prompt template slug not resolvable."""


class AIPromptVariablesError(AILayerError):
    """Variables required by template not provided."""


class AICapBreach(AILayerError):
    """
    Cap breach that cannot be resolved by fallback or override.
    Carries structured context for the caller (intended for 429 responses).
    """

    def __init__(self, message: str, context: dict):
        super().__init__(message)
        self.context = context


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AIRouting:
    """Resolved routing row for a (module, purpose) call."""
    id: str
    tenant_id: Optional[str]
    module: str
    purpose: str
    primary_model: str
    fallback_model: Optional[str]
    max_tokens: int
    per_call_token_cap: Optional[int]
    matter_daily_cost_cap_usd: Optional[Decimal]
    tenant_daily_cost_cap_usd: Optional[Decimal]
    warning_thresholds: list[float]
    override_policy: dict[str, Any]


@dataclass
class AICallResult:
    """
    Structured result from an adapter call. Callers should treat `text` as the
    primary payload; `warnings`, `exception`, and `actual_model` are for
    attribution and UI surfacing.
    """
    call_id: int
    text: str
    model_used: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal
    latency_ms: int
    utilization_pct: float
    warnings: list[str] = field(default_factory=list)
    exception_id: Optional[str] = None
    fallback_used: bool = False
    override_used: bool = False
    status: str = "ok"

    def to_headers(self) -> dict[str, str]:
        """HTTP headers the route layer can forward to the client for UX hooks."""
        h: dict[str, str] = {
            "X-Praesidium-AI-Model": self.model_used,
            "X-Praesidium-AI-Tokens": str(self.total_tokens),
            "X-Praesidium-AI-Cost-USD": f"{self.cost_usd:.6f}",
            "X-Praesidium-AI-Utilization-Pct":
                f"{self.utilization_pct * 100:.1f}",
        }
        if self.warnings:
            h["X-Praesidium-AI-Budget-Warning"] = ",".join(self.warnings)
        if self.fallback_used:
            h["X-Praesidium-AI-Fallback-Used"] = "1"
        if self.override_used:
            h["X-Praesidium-AI-Override-Used"] = "1"
        return h


@dataclass
class RenderedPrompt:
    """Output of prompt_service.render — passed to adapter.call()."""
    template_id: Optional[str]
    template_version: Optional[int]
    system_prompt: Optional[str]
    user_prompt: str
    response_format: str = "text"


# ---------------------------------------------------------------------------
# Context helper — call-level identity for attribution
# ---------------------------------------------------------------------------

@dataclass
class AICallContext:
    """
    Identity of the caller. Threaded through every adapter invocation so that
    ai_api_calls and ai_cost_exceptions carry full attribution.
    """
    tenant_id: str
    module: str
    purpose: str
    user_id: Optional[int] = None
    matter_id: Optional[str] = None
    user_role: Optional[str] = None
    # Override-related — set by the route when a user explicitly authorizes
    # overage. The adapter does not trust these flags without a role check.
    override_requested: bool = False
    override_reason: Optional[str] = None

    def normalized(self) -> "AICallContext":
        """Return a copy with tenant_id stripped (CHAR(36) trailing spaces)."""
        return AICallContext(
            tenant_id=(self.tenant_id or "").strip(),
            module=self.module,
            purpose=self.purpose,
            user_id=self.user_id,
            matter_id=self.matter_id,
            user_role=self.user_role,
            override_requested=self.override_requested,
            override_reason=self.override_reason,
        )


# ---------------------------------------------------------------------------
# BYOK key fetch
# ---------------------------------------------------------------------------

def _derive_fernet_key() -> bytes:
    """
    Derive the Fernet key from SECRET_KEY using the pattern established in
    modules/tenant_admin/tenant_admin.py. The first 32 chars of SECRET_KEY
    (padded with '0' to 32 bytes if short), base64-urlsafe-encoded. Must
    match that module exactly so keys written by the BYOK UI decrypt here.
    """
    import base64
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    return base64.urlsafe_b64encode(key_bytes)


async def _get_anthropic_key(tenant_id: str) -> str:
    tid = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT encrypted_key
                  FROM credentials_vault
                 WHERE TRIM(tenant_id) = :tid
                   AND provider = 'anthropic'
                   AND key_type = 'api_key'
                 ORDER BY updated_at DESC
                 LIMIT 1
            """),
            {"tid": tid},
        )
        row = result.first()
    if not row or not row[0]:
        raise AIKeyMissingError(
            f"No Anthropic API key in credentials_vault for tenant {tid}"
        )
    encrypted = row[0]
    # Decrypt using the Fernet pattern from tenant_admin. Pre-encryption
    # keys (raw sk-ant-api...) are rejected here so a malformed vault row
    # doesn't silently work; the BYOK UI is the only write path.
    try:
        from cryptography.fernet import Fernet, InvalidToken
        f = Fernet(_derive_fernet_key())
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise AIKeyMissingError(
            f"Anthropic API key for tenant {tid} failed decryption. "
            f"Re-provision via tenant admin BYOK UI."
        ) from exc


# ---------------------------------------------------------------------------
# Routing resolution
# ---------------------------------------------------------------------------

async def _resolve_routing(
    tenant_id: str, module: str, purpose: str
) -> AIRouting:
    """
    Tenant-specific row wins over NULL platform default. Published rows only.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id::text, tenant_id, module, purpose,
                       primary_model, fallback_model,
                       max_tokens, per_call_token_cap,
                       matter_daily_cost_cap_usd, tenant_daily_cost_cap_usd,
                       warning_thresholds, override_policy
                  FROM ai_model_routing
                 WHERE module = :module
                   AND purpose = :purpose
                   AND status = 'published'
                   AND (TRIM(tenant_id) = :tid OR tenant_id IS NULL)
                 ORDER BY CASE WHEN tenant_id IS NULL THEN 1 ELSE 0 END
                 LIMIT 1
            """),
            {"module": module, "purpose": purpose, "tid": tenant_id.strip()},
        )
        row = result.first()

    if not row:
        raise AIRoutingMissingError(
            f"No ai_model_routing row for module={module!r} purpose={purpose!r}"
        )

    thresholds = row[10] or [0.75, 0.90]
    if isinstance(thresholds, str):
        thresholds = json.loads(thresholds)
    override_policy = row[11] or {"allow_override": False}
    if isinstance(override_policy, str):
        override_policy = json.loads(override_policy)

    return AIRouting(
        id=row[0],
        tenant_id=(row[1] or "").strip() or None,
        module=row[2],
        purpose=row[3],
        primary_model=row[4],
        fallback_model=row[5],
        max_tokens=row[6],
        per_call_token_cap=row[7],
        matter_daily_cost_cap_usd=row[8],
        tenant_daily_cost_cap_usd=row[9],
        warning_thresholds=[float(t) for t in thresholds],
        override_policy=override_policy,
    )


# ---------------------------------------------------------------------------
# Spend lookup
# ---------------------------------------------------------------------------

async def _matter_spend_today_usd(
    tenant_id: str, matter_id: Optional[str]
) -> Decimal:
    """
    Sum cost_usd for successful and failed calls today for (tenant, matter).
    Matter-null calls bill to the tenant-daily bucket only.

    Reads from the promoted ai_api_calls.matter_id column. Excludes
    allocation_status='reallocated' rows — retroactive allocation is
    bookkeeping only and must not re-trigger cap checks per the
    Apr 18, 2026 spec.
    """
    if not matter_id:
        return Decimal("0")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COALESCE(SUM(cost_usd), 0)
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND matter_id = CAST(:mid AS uuid)
                   AND allocation_status != 'reallocated'
                   AND created_at::date = CURRENT_DATE
            """),
            {"tid": tenant_id.strip(), "mid": str(matter_id)},
        )
        return Decimal(str(result.scalar() or 0))


async def _tenant_spend_today_usd(tenant_id: str) -> Decimal:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COALESCE(SUM(cost_usd), 0)
                  FROM ai_api_calls
                 WHERE TRIM(tenant_id) = :tid
                   AND created_at::date = CURRENT_DATE
            """),
            {"tid": tenant_id.strip()},
        )
        return Decimal(str(result.scalar() or 0))


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def _project_cost_usd(
    model: str, estimated_input_tokens: int, max_output_tokens: int
) -> Decimal:
    """
    Worst-case projection used for pre-call budget check. Conservative —
    assumes max_output_tokens will be returned.
    """
    input_rate, output_rate = MODEL_PRICING.get(
        model, MODEL_PRICING[DEFAULT_PRIMARY_MODEL]
    )
    input_cost = Decimal(str(input_rate)) * Decimal(estimated_input_tokens) / Decimal("1000000")
    output_cost = Decimal(str(output_rate)) * Decimal(max_output_tokens) / Decimal("1000000")
    return (input_cost + output_cost).quantize(Decimal("0.000001"))


def _actual_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    input_rate, output_rate = MODEL_PRICING.get(
        model, MODEL_PRICING[DEFAULT_PRIMARY_MODEL]
    )
    input_cost = Decimal(str(input_rate)) * Decimal(input_tokens) / Decimal("1000000")
    output_cost = Decimal(str(output_rate)) * Decimal(output_tokens) / Decimal("1000000")
    return (input_cost + output_cost).quantize(Decimal("0.000001"))


def _estimate_tokens(text_content: str) -> int:
    """
    Rough token count — 1 token ≈ 4 characters English. Good enough for
    pre-call projection. The Anthropic response provides the actual count.
    """
    if not text_content:
        return 0
    return max(1, len(text_content) // 4)


# ---------------------------------------------------------------------------
# Budget decision
# ---------------------------------------------------------------------------

@dataclass
class BudgetDecision:
    decision: str                # 'ok' | 'fallback' | 'override' | 'reject'
    model_to_use: str
    utilization_pct: float       # against matter cap (or tenant if no matter cap)
    matter_spend_today: Decimal
    tenant_spend_today: Decimal
    projected_cost: Decimal
    warnings: list[str] = field(default_factory=list)
    fallback_used: bool = False
    override_used: bool = False
    breach_context: dict = field(default_factory=dict)


def _compute_warnings(
    utilization_pct: float, thresholds: list[float]
) -> list[str]:
    """Return list of warning tags ('75pct','90pct') matching crossed thresholds."""
    w: list[str] = []
    for t in sorted(thresholds):
        if utilization_pct >= t:
            w.append(f"{int(t * 100)}pct")
    return w


async def _make_budget_decision(
    routing: AIRouting,
    ctx: AICallContext,
    estimated_input_tokens: int,
) -> BudgetDecision:
    """
    Returns a BudgetDecision describing which model to use and what happens next.
    No DB writes occur here — the caller logs the exception if one is needed.
    """
    matter_spend = await _matter_spend_today_usd(ctx.tenant_id, ctx.matter_id)
    tenant_spend = await _tenant_spend_today_usd(ctx.tenant_id)

    primary_proj = _project_cost_usd(
        routing.primary_model, estimated_input_tokens, routing.max_tokens
    )
    fallback_proj = (
        _project_cost_usd(routing.fallback_model, estimated_input_tokens,
                          routing.max_tokens)
        if routing.fallback_model else None
    )

    matter_cap = routing.matter_daily_cost_cap_usd
    tenant_cap = routing.tenant_daily_cost_cap_usd

    # Utilization is the max of matter and tenant utilization — whichever is tighter
    utilization = 0.0
    if matter_cap and matter_cap > 0 and ctx.matter_id:
        utilization = max(
            utilization,
            float((matter_spend + primary_proj) / matter_cap),
        )
    if tenant_cap and tenant_cap > 0:
        utilization = max(
            utilization,
            float((tenant_spend + primary_proj) / tenant_cap),
        )

    warnings = _compute_warnings(utilization, routing.warning_thresholds)

    def would_breach(model_projected: Decimal) -> tuple[bool, str]:
        if matter_cap and ctx.matter_id and (matter_spend + model_projected) > matter_cap:
            return True, (
                f"matter_daily_cost_cap exceeded: spend_today="
                f"{matter_spend} + projected={model_projected} > cap={matter_cap}"
            )
        if tenant_cap and (tenant_spend + model_projected) > tenant_cap:
            return True, (
                f"tenant_daily_cost_cap exceeded: spend_today="
                f"{tenant_spend} + projected={model_projected} > cap={tenant_cap}"
            )
        return False, ""

    breached, reason = would_breach(primary_proj)
    if not breached:
        return BudgetDecision(
            decision="ok",
            model_to_use=routing.primary_model,
            utilization_pct=utilization,
            matter_spend_today=matter_spend,
            tenant_spend_today=tenant_spend,
            projected_cost=primary_proj,
            warnings=warnings,
        )

    breach_ctx = {
        "breach_reason": reason,
        "matter_spend_today": float(matter_spend),
        "tenant_spend_today": float(tenant_spend),
        "matter_cap": float(matter_cap) if matter_cap else None,
        "tenant_cap": float(tenant_cap) if tenant_cap else None,
        "primary_projected": float(primary_proj),
        "fallback_projected": float(fallback_proj) if fallback_proj else None,
    }

    # Try fallback first (Dennis's spec)
    if fallback_proj is not None and routing.fallback_model:
        fb_breached, fb_reason = would_breach(fallback_proj)
        if not fb_breached:
            return BudgetDecision(
                decision="fallback",
                model_to_use=routing.fallback_model,
                utilization_pct=utilization,
                matter_spend_today=matter_spend,
                tenant_spend_today=tenant_spend,
                projected_cost=fallback_proj,
                warnings=warnings,
                fallback_used=True,
                breach_context=breach_ctx,
            )
        breach_ctx["fallback_breach_reason"] = fb_reason

    # Fallback also breached (or no fallback). Check override.
    op_ = routing.override_policy or {}
    if (op_.get("allow_override")
            and ctx.override_requested
            and ctx.user_role in (op_.get("roles") or [])):
        # Optional ceiling: even overridden, can't exceed multiplier * cap
        max_mult = float(op_.get("max_override_multiplier") or 0)
        if max_mult and matter_cap:
            hard_ceiling = Decimal(str(max_mult)) * matter_cap
            if (matter_spend + primary_proj) > hard_ceiling:
                breach_ctx["hard_ceiling_breached"] = True
                return BudgetDecision(
                    decision="reject",
                    model_to_use=routing.primary_model,
                    utilization_pct=utilization,
                    matter_spend_today=matter_spend,
                    tenant_spend_today=tenant_spend,
                    projected_cost=primary_proj,
                    warnings=warnings,
                    breach_context=breach_ctx,
                )
        # Override accepted
        if op_.get("require_reason") and not ctx.override_reason:
            return BudgetDecision(
                decision="reject",
                model_to_use=routing.primary_model,
                utilization_pct=utilization,
                matter_spend_today=matter_spend,
                tenant_spend_today=tenant_spend,
                projected_cost=primary_proj,
                warnings=warnings,
                breach_context={**breach_ctx,
                                "reject_reason": "override_reason_required"},
            )
        return BudgetDecision(
            decision="override",
            model_to_use=routing.primary_model,
            utilization_pct=utilization,
            matter_spend_today=matter_spend,
            tenant_spend_today=tenant_spend,
            projected_cost=primary_proj,
            warnings=warnings,
            override_used=True,
            breach_context=breach_ctx,
        )

    # Hard reject.
    return BudgetDecision(
        decision="reject",
        model_to_use=routing.primary_model,
        utilization_pct=utilization,
        matter_spend_today=matter_spend,
        tenant_spend_today=tenant_spend,
        projected_cost=primary_proj,
        warnings=warnings,
        breach_context=breach_ctx,
    )


# ---------------------------------------------------------------------------
# Persistence — ai_api_calls, ai_cost_exceptions
# ---------------------------------------------------------------------------

async def _insert_ai_api_call(
    *,
    ctx: AICallContext,
    routing: Optional[AIRouting],
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    status: str,
    error_message: Optional[str],
    request_metadata: dict,
) -> int:
    """
    Write to ai_api_calls. Returns id for linkage.

    Determines allocation_status and unallocated_reason from context:
        matter_id present + policy allows billing -> 'allocated'
        matter_id absent                          -> 'unallocated' + 'no_matter_context'
        matter_id present + policy.bill_overage_to_matter == false
            -> 'firm_overhead' + 'policy_firm_overhead'

    Note: 'matter_unavailable' is set by a downstream reconciliation job
    if the matter_id references a deleted/archived matter. The adapter
    doesn't try to verify matter existence inline — the call already
    succeeded, and blocking on a matter lookup adds latency without
    preventing the spend.
    """
    alloc_status = "allocated"
    unalloc_reason: Optional[str] = None

    if not ctx.matter_id:
        alloc_status = "unallocated"
        unalloc_reason = "no_matter_context"
    elif routing is not None:
        op_ = routing.override_policy or {}
        bill_to_matter = op_.get("bill_overage_to_matter", True)
        # Only route to firm_overhead on policy-driven non-billable workflows.
        # This applies to ALL calls under such a routing, not just overages —
        # e.g. admin.timesheet_reconcile is cross-matter by nature and should
        # land in firm overhead even when user_id is set.
        if bill_to_matter is False:
            alloc_status = "firm_overhead"
            unalloc_reason = "policy_firm_overhead"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO ai_api_calls
                  (tenant_id, user_id, provider, model, module, purpose,
                   input_tokens, output_tokens, total_tokens,
                   cost_usd, latency_ms, status, error_message,
                   request_metadata,
                   matter_id, allocation_status, unallocated_reason)
                VALUES
                  (:tenant_id, :user_id, :provider, :model, :module, :purpose,
                   :input_tokens, :output_tokens, :total_tokens,
                   :cost_usd, :latency_ms, :status, :error_message,
                   CAST(:request_metadata AS jsonb),
                   CAST(:matter_id AS uuid), :alloc_status, :unalloc_reason)
                RETURNING id
            """),
            {
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
                "provider": provider,
                "model": model,
                "module": ctx.module,
                "purpose": ctx.purpose,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "status": status,
                "error_message": error_message,
                "request_metadata": json.dumps(request_metadata),
                "matter_id": ctx.matter_id,
                "alloc_status": alloc_status,
                "unalloc_reason": unalloc_reason,
            },
        )
        call_id = result.scalar()
        await session.commit()
        return int(call_id)


async def _insert_cost_exception(
    *,
    ctx: AICallContext,
    routing: AIRouting,
    decision: BudgetDecision,
    breach_type: str,
    ai_api_call_id: Optional[int],
) -> str:
    """Write to ai_cost_exceptions. Returns id."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO ai_cost_exceptions
                  (tenant_id, matter_id, user_id, module, purpose,
                   routing_id, ai_api_call_id,
                   breach_type, breach_reason,
                   attempted_model, actual_model,
                   projected_cost_usd, matter_spend_today_usd, matter_cap_usd,
                   override_used, override_reason, override_approved_by,
                   disposition)
                VALUES
                  (:tenant_id,
                   CAST(:matter_id AS uuid),
                   :user_id, :module, :purpose,
                   CAST(:routing_id AS uuid),
                   :ai_api_call_id,
                   :breach_type, :breach_reason,
                   :attempted_model, :actual_model,
                   :projected_cost_usd, :matter_spend, :matter_cap,
                   :override_used, :override_reason, :override_approved_by,
                   'pending')
                RETURNING id::text
            """),
            {
                "tenant_id": ctx.tenant_id,
                "matter_id": ctx.matter_id,
                "user_id": ctx.user_id,
                "module": ctx.module,
                "purpose": ctx.purpose,
                "routing_id": routing.id,
                "ai_api_call_id": ai_api_call_id,
                "breach_type": breach_type,
                "breach_reason": json.dumps(decision.breach_context),
                "attempted_model": routing.primary_model,
                "actual_model": decision.model_to_use,
                "projected_cost_usd": decision.projected_cost,
                "matter_spend": decision.matter_spend_today,
                "matter_cap": routing.matter_daily_cost_cap_usd,
                "override_used": decision.override_used,
                "override_reason": ctx.override_reason,
                "override_approved_by": ctx.user_id if decision.override_used else None,
            },
        )
        exc_id = result.scalar()
        await session.commit()
        return str(exc_id)


# ---------------------------------------------------------------------------
# HTTP call
# ---------------------------------------------------------------------------

async def _anthropic_request(
    api_key: str,
    model: str,
    max_tokens: int,
    system_prompt: Optional[str],
    user_prompt: str,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> tuple[dict, int]:
    """Return (response_json, latency_ms)."""
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_prompt:
        payload["system"] = system_prompt

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json=payload,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code != 200:
        raise AILayerError(
            f"Anthropic API error {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json(), latency_ms


def _extract_text(response_json: dict) -> str:
    """Extract concatenated text from Anthropic content blocks."""
    blocks = response_json.get("content") or []
    parts: list[str] = []
    for b in blocks:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts).strip()


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$",
                            re.MULTILINE | re.DOTALL)


def strip_markdown_fences(raw: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` wrappers. Adapter utility."""
    s = raw.strip()
    if s.startswith("```"):
        s = _JSON_FENCE_RE.sub("", s).strip()
    return s


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _render_prompt_text(template: str, variables: dict[str, Any]) -> str:
    """
    Render a template with {placeholder} substitution. Missing variables raise
    AIPromptVariablesError. Extra variables are ignored.
    """
    required = set(_VAR_RE.findall(template or ""))
    missing = required - set(variables.keys())
    if missing:
        raise AIPromptVariablesError(
            f"Prompt template missing variables: {sorted(missing)}"
        )
    return _VAR_RE.sub(lambda m: str(variables.get(m.group(1), "")), template)


async def resolve_prompt(
    tenant_id: str,
    slug: str,
    variables: Optional[dict[str, Any]] = None,
    version: Optional[int] = None,
) -> RenderedPrompt:
    """
    Resolve a prompt_templates slug to a rendered prompt. Tenant-specific
    slug wins over platform default (same resolution rule as ai_model_routing).
    """
    variables = variables or {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT t.id::text, t.current_version
                  FROM prompt_templates t
                 WHERE t.slug = :slug
                   AND t.status = 'published'
                   AND (TRIM(t.tenant_id) = :tid OR t.tenant_id IS NULL)
                 ORDER BY CASE WHEN t.tenant_id IS NULL THEN 1 ELSE 0 END
                 LIMIT 1
            """),
            {"slug": slug, "tid": (tenant_id or "").strip()},
        )
        row = result.first()
        if not row:
            raise AIPromptNotFoundError(f"No prompt_templates row for slug={slug!r}")
        template_id, current_version = row[0], int(row[1])
        use_version = version or current_version

        v_result = await session.execute(
            text("""
                SELECT system_prompt, user_prompt, response_format
                  FROM prompt_template_versions
                 WHERE template_id = CAST(:tid AS uuid)
                   AND version = :ver
                 LIMIT 1
            """),
            {"tid": template_id, "ver": use_version},
        )
        v_row = v_result.first()
        if not v_row:
            raise AIPromptNotFoundError(
                f"No prompt_template_versions row for slug={slug!r} v={use_version}"
            )

    rendered_system = (
        _render_prompt_text(v_row[0], variables) if v_row[0] else None
    )
    rendered_user = _render_prompt_text(v_row[1], variables)
    return RenderedPrompt(
        template_id=template_id,
        template_version=use_version,
        system_prompt=rendered_system,
        user_prompt=rendered_user,
        response_format=v_row[2] or "text",
    )


# ---------------------------------------------------------------------------
# Main entry point — call()
# ---------------------------------------------------------------------------

async def call(
    ctx: AICallContext,
    *,
    prompt: Optional[RenderedPrompt] = None,
    raw_user_prompt: Optional[str] = None,
    raw_system_prompt: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
) -> AICallResult:
    """
    Execute an inline AI call. Exactly one of `prompt` or `raw_user_prompt`
    must be provided. `prompt` is preferred — it carries template_id and
    template_version for provenance, which the adapter writes into
    ai_api_calls.request_metadata.

    The adapter NEVER bypasses routing or cost caps. Callers who need to
    skip routing should still go through call() with a direct raw_user_prompt
    and an appropriate (module, purpose) that maps to a permissive routing row.
    """
    if (prompt is None) == (raw_user_prompt is None):
        raise ValueError(
            "call() requires exactly one of prompt= or raw_user_prompt="
        )

    ctx = ctx.normalized()
    routing = await _resolve_routing(ctx.tenant_id, ctx.module, ctx.purpose)

    # Pull prompt text
    if prompt is not None:
        system_prompt = prompt.system_prompt
        user_prompt = prompt.user_prompt
        template_id = prompt.template_id
        template_version = prompt.template_version
    else:
        system_prompt = raw_system_prompt
        user_prompt = raw_user_prompt
        template_id = None
        template_version = None

    # Per-call token cap (either routing.per_call_token_cap or max_tokens)
    effective_max_tokens = (
        max_tokens_override or routing.max_tokens
    )
    if routing.per_call_token_cap:
        effective_max_tokens = min(effective_max_tokens, routing.per_call_token_cap)

    # Budget decision
    est_input = _estimate_tokens(
        (system_prompt or "") + "\n" + (user_prompt or "")
    )
    decision = await _make_budget_decision(routing, ctx, est_input)

    # Short-circuit: reject without making the HTTP call
    if decision.decision == "reject":
        exc_id = await _insert_cost_exception(
            ctx=ctx, routing=routing, decision=decision,
            breach_type="hard_cap", ai_api_call_id=None,
        )
        raise AICapBreach(
            "AI cost cap breach — fallback unavailable or insufficient, "
            "and override declined or not permitted.",
            context={
                "exception_id": exc_id,
                "utilization_pct": decision.utilization_pct,
                "breach": decision.breach_context,
                "warnings": decision.warnings,
            },
        )

    # Fetch the API key — AFTER routing is resolved so a missing-key error
    # doesn't consume an ai_cost_exceptions row for an unrelated reason.
    api_key = await _get_anthropic_key(ctx.tenant_id)

    # Execute
    status = "ok"
    error_message: Optional[str] = None
    response_json: dict = {}
    latency_ms = 0
    try:
        response_json, latency_ms = await _anthropic_request(
            api_key=api_key,
            model=decision.model_to_use,
            max_tokens=effective_max_tokens,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except AILayerError as exc:
        status = "error"
        error_message = str(exc)[:1000]
        # Write the failure and re-raise
        call_id = await _insert_ai_api_call(
            ctx=ctx,
            routing=routing,
            provider="anthropic",
            model=decision.model_to_use,
            input_tokens=0, output_tokens=0, total_tokens=0,
            cost_usd=Decimal("0"),
            latency_ms=latency_ms,
            status=status, error_message=error_message,
            request_metadata={
                "matter_id": ctx.matter_id,
                "routing_id": routing.id,
                "template_id": template_id,
                "template_version": template_version,
                "decision": decision.decision,
                "warnings": decision.warnings,
            },
        )
        log.error("AI call failed module=%s purpose=%s tenant=%s: %s",
                  ctx.module, ctx.purpose, ctx.tenant_id, exc)
        raise

    usage = response_json.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = input_tokens + output_tokens
    cost_usd = _actual_cost_usd(decision.model_to_use, input_tokens, output_tokens)
    response_text = _extract_text(response_json)

    call_id = await _insert_ai_api_call(
        ctx=ctx,
        routing=routing,
        provider="anthropic",
        model=decision.model_to_use,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status="ok",
        error_message=None,
        request_metadata={
            "matter_id": ctx.matter_id,
            "routing_id": routing.id,
            "template_id": template_id,
            "template_version": template_version,
            "decision": decision.decision,
            "warnings": decision.warnings,
            "fallback_used": decision.fallback_used,
            "override_used": decision.override_used,
        },
    )

    # Log an exception row if the budget decision was anything but 'ok'
    exc_id: Optional[str] = None
    if decision.decision == "fallback":
        exc_id = await _insert_cost_exception(
            ctx=ctx, routing=routing, decision=decision,
            breach_type="fallback_used", ai_api_call_id=call_id,
        )
    elif decision.decision == "override":
        exc_id = await _insert_cost_exception(
            ctx=ctx, routing=routing, decision=decision,
            breach_type="override_used", ai_api_call_id=call_id,
        )

    return AICallResult(
        call_id=call_id,
        text=response_text,
        model_used=decision.model_to_use,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        utilization_pct=decision.utilization_pct,
        warnings=decision.warnings,
        exception_id=exc_id,
        fallback_used=decision.fallback_used,
        override_used=decision.override_used,
        status="ok",
    )


# ---------------------------------------------------------------------------
# Dual-mode — enqueue() for background work
# ---------------------------------------------------------------------------

async def enqueue(
    ctx: AICallContext,
    *,
    prompt: Optional[RenderedPrompt] = None,
    raw_user_prompt: Optional[str] = None,
    raw_system_prompt: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
    queue_name: str = "ai_calls",
) -> str:
    """
    Enqueue an AI call for background execution via RQ. Returns a queued_call_id
    (a UUID the caller can poll or subscribe to). The RQ worker picks up the
    job and calls this module's `execute_queued_call` with the payload.

    This function writes a stub row to ai_api_calls with status='queued' so
    callers have something to poll and so queued calls contribute to today's
    spend projections at the moment of enqueue (prevents thundering herd
    submissions from bypassing caps).

    Note: This function intentionally does not import rq at module top — RQ
    is only used in background worker VMs and we want the adapter importable
    from any VM.
    """
    ctx = ctx.normalized()
    # Pre-check routing exists; cheap and prevents garbage enqueues
    await _resolve_routing(ctx.tenant_id, ctx.module, ctx.purpose)

    queued_call_id = str(uuid.uuid4())
    payload = {
        "queued_call_id": queued_call_id,
        "ctx": asdict(ctx),
        "prompt": asdict(prompt) if prompt else None,
        "raw_user_prompt": raw_user_prompt,
        "raw_system_prompt": raw_system_prompt,
        "max_tokens_override": max_tokens_override,
    }

    # Stub row — so UI can show "queued" immediately.
    # Note: the worker will write a fresh row on execution; this row is for
    # the queued-state UI signal only. We use status='queued'.
    # allocation_status is tentatively set here and corrected on execution.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO ai_api_calls
                  (tenant_id, user_id, provider, model, module, purpose,
                   input_tokens, output_tokens, total_tokens,
                   cost_usd, latency_ms, status, request_metadata,
                   matter_id, allocation_status, unallocated_reason)
                VALUES
                  (:tenant_id, :user_id, 'anthropic',
                   'pending', :module, :purpose,
                   0, 0, 0, 0, 0, 'queued',
                   CAST(:meta AS jsonb),
                   CAST(:matter_id AS uuid),
                   :alloc_status, :unalloc_reason)
            """),
            {
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
                "module": ctx.module,
                "purpose": ctx.purpose,
                "meta": json.dumps({
                    "queued_call_id": queued_call_id,
                    "matter_id": ctx.matter_id,
                }),
                "matter_id": ctx.matter_id,
                "alloc_status": "allocated" if ctx.matter_id else "unallocated",
                "unalloc_reason": None if ctx.matter_id else "no_matter_context",
            },
        )
        await session.commit()

    try:
        from redis import Redis
        from rq import Queue
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        q = Queue(queue_name, connection=Redis.from_url(redis_url))
        q.enqueue(
            "modules.intelligence.anthropic_adapter.execute_queued_call",
            payload,
            job_timeout="15m",
        )
    except Exception as exc:
        # If RQ isn't available (tests, offline dev), log but don't fail —
        # the queued row will remain in status='queued' for retry.
        log.warning("enqueue: RQ unavailable (%s) — queued_call_id=%s "
                    "remains in 'queued' state",
                    exc, queued_call_id)

    return queued_call_id


def execute_queued_call(payload: dict) -> dict:
    """
    RQ entry point. Runs the adapter call synchronously inside the worker.
    Returns a serializable dict of the result.
    """
    import asyncio

    async def _run() -> dict:
        ctx_d = payload.get("ctx") or {}
        ctx = AICallContext(**ctx_d)
        prompt_d = payload.get("prompt")
        prompt_obj = RenderedPrompt(**prompt_d) if prompt_d else None
        result = await call(
            ctx,
            prompt=prompt_obj,
            raw_user_prompt=payload.get("raw_user_prompt"),
            raw_system_prompt=payload.get("raw_system_prompt"),
            max_tokens_override=payload.get("max_tokens_override"),
        )
        return {
            "queued_call_id": payload.get("queued_call_id"),
            "call_id": result.call_id,
            "text": result.text,
            "model_used": result.model_used,
            "total_tokens": result.total_tokens,
            "cost_usd": float(result.cost_usd),
            "utilization_pct": result.utilization_pct,
            "warnings": result.warnings,
            "fallback_used": result.fallback_used,
            "override_used": result.override_used,
            "exception_id": result.exception_id,
        }

    return asyncio.run(_run())
