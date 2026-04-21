"""
Praesidium Intelligence Module
==============================

The platform's single AI integration point. Every Anthropic API call flows
through the anthropic_adapter per Architectural Constraint #11.

Public surface:
    AICallContext   — identity + (module, purpose) routing key
    AICallResult    — structured response with call_id, cost, warnings
    call()          — inline execution (route handlers, short workflows)
    enqueue()       — background execution (corpus-wide jobs, long workflows)
    resolve_prompt()— render a versioned prompt_templates row

Cost discipline:
    Cap breaches flow: fallback -> override (bills overage to matter) -> reject.
    Warning thresholds (default 75%, 90%) emit response headers the route layer
    can forward to the client for ambient UX surfacing.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, USPTO Reg. No. 54,168
"""

from .anthropic_adapter import (
    AICallContext,
    AICallResult,
    AIRouting,
    RenderedPrompt,
    AILayerError,
    AIKeyMissingError,
    AIRoutingMissingError,
    AIPromptNotFoundError,
    AIPromptVariablesError,
    AICapBreach,
    call,
    enqueue,
    execute_queued_call,
    resolve_prompt,
    strip_markdown_fences,
)
from .ai_usage_routes import router as ai_usage_router
from .widget_service import (
    get_my_matters_ai_usage,
    get_firm_ai_usage,
    get_unallocated_review,
)

__all__ = [
    "AICallContext",
    "AICallResult",
    "AIRouting",
    "RenderedPrompt",
    "AILayerError",
    "AIKeyMissingError",
    "AIRoutingMissingError",
    "AIPromptNotFoundError",
    "AIPromptVariablesError",
    "AICapBreach",
    "call",
    "enqueue",
    "execute_queued_call",
    "resolve_prompt",
    "strip_markdown_fences",
    "ai_usage_router",
    "get_my_matters_ai_usage",
    "get_firm_ai_usage",
    "get_unallocated_review",
]
