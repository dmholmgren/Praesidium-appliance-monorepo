"""
Praesidium — Billing Chat Wire-Up v3
=====================================
Same as v2, but fixes the SQL bind-param parsing bug — JSON values with
colons inside string literals were being treated as :bind parameters by
SQLAlchemy's text() parser. Now we pass the JSON as bound params with
CAST(:name AS jsonb), matching the platform's architectural constant.

Skips step 2 (patch_routes) and step 3 (patch_init) if already applied
from a prior v2 attempt — we only need to retry step 1 here if v2 made
it through the patching stages before the seed failed.

Idempotent.
"""
from __future__ import annotations
import shutil
import asyncio
import json
from pathlib import Path

ROUTES = Path("/app/modules/billing/api/billing_routes.py")
INIT   = Path("/app/modules/billing/__init__.py")


async def seed_routing():
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal

    print("\n[1/3] Seeding ai_model_routing default for billing.chat")

    override_policy = json.dumps({
        "allow_override": True,
        "roles": ["partner", "admin"],
        "require_reason": True,
        "max_override_multiplier": 2.0,
        "bill_overage_to_matter": False,
    })
    warning_thresholds = json.dumps([0.75, 0.90])

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

        await session.execute(
            text("""
                INSERT INTO ai_model_routing
                    (tenant_id, module, purpose,
                     primary_model, fallback_model, max_tokens,
                     matter_daily_cost_cap_usd, tenant_daily_cost_cap_usd,
                     warning_thresholds, override_policy, status)
                VALUES
                    (NULL, 'billing', 'chat',
                     'claude-sonnet-4-20250514',
                     'claude-haiku-4-5-20251001',
                     2048,
                     2.00, 200.00,
                     CAST(:thresholds AS jsonb),
                     CAST(:policy     AS jsonb),
                     'active')
            """),
            {"thresholds": warning_thresholds, "policy": override_policy},
        )
        await session.commit()
    print("  OK    seeded billing.chat routing")


def check_patches_applied():
    """v2 may have partially applied the file edits before seed failed."""
    routes_patched = "billing_chat_router" in ROUTES.read_text()
    init_patched   = "billing_chat_router" in INIT.read_text()
    print(f"\n[2/3] patch_routes state: {'already applied' if routes_patched else 'NOT APPLIED'}")
    print(f"[3/3] patch_init   state: {'already applied' if init_patched else 'NOT APPLIED'}")
    return routes_patched, init_patched


# ---------------------------------------------------------------------------
# Fallback re-apply in case v2 rolled back file edits when seed failed.
# (It shouldn't have — text writes are atomic — but belt-and-suspenders.)
# ---------------------------------------------------------------------------
OLD_BLOCK_START_MARKER = "# ---------------------------------------------------------------------------\n# Billing Chat — stub endpoint"

NEW_BLOCK = '''# ---------------------------------------------------------------------------
# Billing Chat — adapter-backed SSE endpoint
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse

billing_chat_router = APIRouter(tags=["billing-chat"])


@billing_chat_router.post("/api/v1/billing/chat/stream")
async def billing_chat_stream(request: Request):
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
    src = ROUTES.read_text()
    if "billing_chat_router" in src:
        return False
    idx = src.find(OLD_BLOCK_START_MARKER)
    if idx < 0:
        idx = src.find("@billing_routes.post")
        if idx < 0:
            print("  ERROR: cannot locate chat block")
            raise SystemExit(1)
    bak = ROUTES.with_suffix(ROUTES.suffix + ".bak-chatwire3")
    if not bak.exists():
        shutil.copy2(ROUTES, bak)
    ROUTES.write_text(src[:idx].rstrip() + "\n\n\n" + NEW_BLOCK)
    print("  APPLIED patch_routes (v2 had not taken)")
    return True


def patch_init():
    src = INIT.read_text()
    if "billing_chat_router" in src:
        return False
    anchor = "app.include_router(billing_routes_router)"
    if anchor not in src:
        print("  ERROR: anchor missing in __init__.py")
        raise SystemExit(1)
    addition = (
        "app.include_router(billing_routes_router)\n"
        "        from modules.billing.api.billing_routes import billing_chat_router\n"
        "        app.include_router(billing_chat_router)"
    )
    bak = INIT.with_suffix(INIT.suffix + ".bak-chatwire3")
    if not bak.exists():
        shutil.copy2(INIT, bak)
    INIT.write_text(src.replace(anchor, addition, 1))
    print("  APPLIED patch_init (v2 had not taken)")
    return True


def main():
    print("Praesidium — Billing Chat Wire-Up v3")
    print("=" * 60)

    asyncio.run(seed_routing())

    routes_ok, init_ok = check_patches_applied()
    if not routes_ok:
        patch_routes()
    if not init_ok:
        patch_init()

    import ast
    for p, label in [(ROUTES, "billing_routes.py"), (INIT, "billing/__init__.py")]:
        try:
            ast.parse(p.read_text())
            print(f"  {label} parses clean")
        except SyntaxError as e:
            print(f"  SYNTAX ERROR in {label}: {e}")
            raise SystemExit(1)

    print("\nDone. Restart:")
    print("  docker exec praesidium-web find /app/modules -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true")
    print("  docker restart praesidium-web")


if __name__ == "__main__":
    main()
