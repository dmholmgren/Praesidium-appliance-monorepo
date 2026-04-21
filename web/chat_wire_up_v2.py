"""
Praesidium — Billing Chat Wire-Up v2
=====================================
Two-part fix:
  1. Repairs a latent baseline bug — line 796 has `@billing_routes.post(...)`
     but the router on line 20 is named `router`. This NameError has prevented
     the entire billing_routes.py module from importing since baseline,
     404-ing all 16 endpoints in the file. The startup warning
     "billing_routes not loaded: name 'billing_routes' is not defined"
     has been visible the whole time.

  2. Replaces the stub body with an adapter-backed implementation.

The endpoint path stays /api/v1/billing/chat/stream (what the widget POSTs to).
Since the router has prefix="/api/billing", we attach this one endpoint to
a SECOND no-prefix router exposed from the same module, so the full path
resolves as written.

Idempotent. .bak-chatwire2 backup.
"""
from __future__ import annotations
import shutil
import asyncio
from pathlib import Path

ROUTES = Path("/app/modules/billing/api/billing_routes.py")
INIT   = Path("/app/modules/billing/__init__.py")


# ---------------------------------------------------------------------------
# Step 1 — Seed routing default (idempotent)
# ---------------------------------------------------------------------------
async def seed_routing():
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    print("\n[1/3] Seeding ai_model_routing default for billing.chat")
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT id FROM ai_model_routing
             WHERE tenant_id IS NULL
               AND module = 'billing' AND purpose = 'chat'
             LIMIT 1
        """))
        if r.first():
            print("  SKIP  routing row already exists")
            return
        await session.execute(text("""
            INSERT INTO ai_model_routing
                (tenant_id, module, purpose, primary_model, fallback_model,
                 max_tokens, matter_daily_cost_cap_usd, tenant_daily_cost_cap_usd,
                 warning_thresholds, override_policy, status)
            VALUES
                (NULL, 'billing', 'chat',
                 'claude-sonnet-4-20250514', 'claude-haiku-4-5-20251001', 2048,
                 2.00, 200.00,
                 '[0.75, 0.90]'::jsonb,
                 '{"allow_override":true,"roles":["partner","admin"],
                   "require_reason":true,"max_override_multiplier":2.0,
                   "bill_overage_to_matter":false}'::jsonb,
                 'active')
        """))
        await session.commit()
    print("  OK    seeded billing.chat routing")


# ---------------------------------------------------------------------------
# Step 2 — Patch the chat endpoint in billing_routes.py.
#
# Strategy: replace everything from "# Billing Chat — stub endpoint" to EOF
# with a fixed block that:
#   - Creates a second router object without a prefix (billing_chat_router)
#   - Registers the chat endpoint on THAT router at exactly /api/v1/billing/chat/stream
#   - Wires the adapter
# The __init__.py then gets a second include_router for billing_chat_router.
# ---------------------------------------------------------------------------
OLD_BLOCK_START_MARKER = "# ---------------------------------------------------------------------------\n# Billing Chat — stub endpoint"

NEW_BLOCK = '''# ---------------------------------------------------------------------------
# Billing Chat — adapter-backed SSE endpoint
#
# NOTE: Registered on its OWN router (billing_chat_router) with no prefix so
# the final URL is exactly /api/v1/billing/chat/stream. The main `router` has
# prefix="/api/billing" which would otherwise cause a double-prefix.
#
# The prior stub used `@billing_routes.post(...)` — a NameError against a
# router that was never declared. This caused billing_routes.py to fail at
# import for every endpoint in the file. Fixed here.
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse

billing_chat_router = APIRouter(tags=["billing-chat"])


@billing_chat_router.post("/api/v1/billing/chat/stream")
async def billing_chat_stream(request: Request):
    """
    Billing Intelligence chat — adapter-backed, non-streaming under the hood.
    Emits Anthropic's complete response word-by-word as SSE for typing feel.

    Context tonight (deferred for scoping session):
      matter_id=None  -> allocation_status='unallocated' + 'no_matter_context'
    """
    import asyncio as _asyncio
    from modules.intelligence import (
        call as ai_call,
        AICallContext,
        AIKeyMissingError,
        AICapExceededError,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}

    messages = body.get("messages") or []
    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"),
        None,
    )
    if not last_user or not last_user.get("content"):
        async def _empty():
            yield "data: (empty message)\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")

    user_prompt = last_user["content"]

    # Prior conversation as context (exclude error bubbles)
    history_lines = []
    for m in messages[:-1]:
        content = m.get("content") or ""
        if not content or content.startswith("⚠"):
            continue
        who = "User" if m.get("role") == "user" else "Assistant"
        history_lines.append(f"{who}: {content}")
    history_block = "\\n\\n".join(history_lines)

    system_prompt = (
        "You are Praesidium's Billing Intelligence assistant. You help the "
        "user analyze WIP, classify time entries under UTBMS, interpret AR "
        "aging, strategize collections, and evaluate timekeeper utilization. "
        "Respond concisely in plain text (no markdown). Ask clarifying "
        "questions when the user's intent is ambiguous."
    )

    if history_block:
        combined = (
            f"{system_prompt}\\n\\nPrior conversation:\\n{history_block}"
            f"\\n\\nUser: {user_prompt}\\n\\nAssistant:"
        )
    else:
        combined = f"{system_prompt}\\n\\nUser: {user_prompt}\\n\\nAssistant:"

    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user = getattr(request.state, "current_user", None)
    user_id = getattr(user, "id", None) if user else None

    ctx = AICallContext(
        tenant_id=tenant_id,
        module="billing",
        purpose="chat",
        user_id=user_id,
        matter_id=None,
    )

    try:
        result = await ai_call(ctx, raw_user_prompt=combined)
        text_response = result.text or "(empty response)"
    except AIKeyMissingError:
        text_response = (
            "⚠ Anthropic API key is not configured for this tenant. "
            "Wire the key in Firm Settings → API Keys to enable chat."
        )
    except AICapExceededError as exc:
        text_response = (
            f"⚠ Daily AI budget reached: {exc}. Contact a partner to "
            "increase limits or try again tomorrow."
        )
    except Exception as exc:
        text_response = f"⚠ Unexpected error: {type(exc).__name__}: {exc}"

    async def _stream():
        for word in text_response.split(" "):
            yield f"data: {word} \\n\\n"
            await _asyncio.sleep(0.015)
        yield "data: [DONE]\\n\\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")
'''


def patch_routes():
    print("\n[2/3] Patching billing_routes.py")

    src = ROUTES.read_text()

    if "billing_chat_router" in src:
        print("  SKIP  billing_chat_router already present")
        return

    # Find the start of the old stub block
    idx = src.find(OLD_BLOCK_START_MARKER)
    if idx < 0:
        # Fall back to locating the decorator line directly
        idx = src.find("@billing_routes.post")
        if idx < 0:
            print("  ERROR: cannot locate chat block to replace")
            raise SystemExit(1)
        # Back up to the preceding header comment if present
        # Just anchor at the @billing_routes line itself — less clean but safe
        block_start = idx
    else:
        block_start = idx

    # Backup
    bak = ROUTES.with_suffix(ROUTES.suffix + ".bak-chatwire2")
    if not bak.exists():
        shutil.copy2(ROUTES, bak)
        print(f"  backup -> {bak.name}")

    # Replace from block_start to EOF with NEW_BLOCK
    new_src = src[:block_start].rstrip() + "\n\n\n" + NEW_BLOCK
    ROUTES.write_text(new_src)
    print(f"  OK    rewrote chat block ({len(src) - block_start} bytes old -> "
          f"{len(NEW_BLOCK)} bytes new)")


# ---------------------------------------------------------------------------
# Step 3 — Register billing_chat_router in __init__.py
# ---------------------------------------------------------------------------
def patch_init():
    print("\n[3/3] Registering billing_chat_router in billing/__init__.py")

    src = INIT.read_text()

    if "billing_chat_router" in src:
        print("  SKIP  billing_chat_router already registered")
        return

    # Find the existing billing_routes_router import+register block and add
    # a sibling registration for billing_chat_router right after it.
    anchor = "app.include_router(billing_routes_router)"
    if anchor not in src:
        print(f"  ERROR: anchor not found in __init__.py — cannot register second router")
        raise SystemExit(1)

    addition = (
        "app.include_router(billing_routes_router)\n"
        "        # Chat endpoint lives on its own no-prefix router so\n"
        "        # /api/v1/billing/chat/stream resolves without double-prefix.\n"
        "        from modules.billing.api.billing_routes import billing_chat_router\n"
        "        app.include_router(billing_chat_router)"
    )

    bak = INIT.with_suffix(INIT.suffix + ".bak-chatwire2")
    if not bak.exists():
        shutil.copy2(INIT, bak)
        print(f"  backup -> {bak.name}")

    INIT.write_text(src.replace(anchor, addition, 1))
    print("  OK    registered billing_chat_router")


def main():
    print("Praesidium — Billing Chat Wire-Up v2")
    print("=" * 60)

    asyncio.run(seed_routing())
    patch_routes()
    patch_init()

    import ast
    for p, label in [(ROUTES, "billing_routes.py"), (INIT, "billing/__init__.py")]:
        try:
            ast.parse(p.read_text())
            print(f"  {label} parses clean")
        except SyntaxError as e:
            print(f"  SYNTAX ERROR in {label}: {e}")
            raise SystemExit(1)

    print("\nDone. Restart required (Python file changes):")
    print("  docker exec praesidium-web find /app/modules -name __pycache__ "
          "-type d -exec rm -rf {} + 2>/dev/null || true")
    print("  docker restart praesidium-web")
    print("\nAfter restart, look for the DISAPPEARANCE of the startup warning:")
    print("  'billing_routes not loaded: name billing_routes is not defined'")
    print("That warning going away means the other 16 billing endpoints in")
    print("that file are ALSO now live for the first time since baseline.")


if __name__ == "__main__":
    main()
