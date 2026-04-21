"""
Praesidium — Billing Chat Wire-Up v4 (files only)
==================================================
The routing row is already seeded via direct psql. This script only does
the two file patches — billing_routes.py and billing/__init__.py — which
v2/v3 never reached because the seed failed first.

No database calls. Idempotent.
"""
from __future__ import annotations
import shutil
from pathlib import Path

ROUTES = Path("/app/modules/billing/api/billing_routes.py")
INIT   = Path("/app/modules/billing/__init__.py")


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
    print("\n[1/2] billing_routes.py")
    src = ROUTES.read_text()
    if "billing_chat_router" in src:
        print("  SKIP  already patched")
        return

    # Find the stub block. Try the decorator line directly since the
    # header comment may not match byte-for-byte.
    idx = src.find("@billing_routes.post")
    if idx < 0:
        # Maybe the block got partially touched — look for the stub function
        idx = src.find("async def billing_chat_stream")
        if idx < 0:
            print(f"  ERROR: cannot locate chat block anchor in {ROUTES}")
            raise SystemExit(1)
        # back up to the preceding "from fastapi.responses import StreamingResponse"
        resp_idx = src.rfind("from fastapi.responses import StreamingResponse", 0, idx)
        if resp_idx > 0:
            idx = resp_idx
            # back up further to the preceding header comment if present
            header = src.rfind("# Billing Chat", 0, idx)
            if header > 0 and (idx - header) < 500:
                idx = src.rfind("# ----", 0, header)
                if idx < 0:
                    idx = header

    # Back up from the @decorator line to its preceding header comment if present
    if "@billing_routes.post" in src[idx:idx+100]:
        resp_idx = src.rfind("from fastapi.responses import StreamingResponse", 0, idx)
        if resp_idx > 0 and (idx - resp_idx) < 400:
            idx = resp_idx
        header = src.rfind("# Billing Chat", 0, idx)
        if header > 0 and (idx - header) < 600:
            dashes = src.rfind("# ----", 0, header)
            if dashes > 0 and (header - dashes) < 200:
                idx = dashes
            else:
                idx = header

    bak = ROUTES.with_suffix(ROUTES.suffix + ".bak-chatwire4")
    if not bak.exists():
        shutil.copy2(ROUTES, bak)
        print(f"  backup -> {bak.name}")

    old_len = len(src) - idx
    new_src = src[:idx].rstrip() + "\n\n\n" + NEW_BLOCK
    ROUTES.write_text(new_src)
    print(f"  OK    replaced {old_len} bytes with {len(NEW_BLOCK)} bytes")


def patch_init():
    print("\n[2/2] billing/__init__.py")
    src = INIT.read_text()
    if "billing_chat_router" in src:
        print("  SKIP  already patched")
        return

    anchor = "app.include_router(billing_routes_router)"
    if anchor not in src:
        print(f"  ERROR: anchor not found in {INIT}")
        raise SystemExit(1)

    addition = (
        "app.include_router(billing_routes_router)\n"
        "        from modules.billing.api.billing_routes import billing_chat_router\n"
        "        app.include_router(billing_chat_router)"
    )

    bak = INIT.with_suffix(INIT.suffix + ".bak-chatwire4")
    if not bak.exists():
        shutil.copy2(INIT, bak)
        print(f"  backup -> {bak.name}")

    INIT.write_text(src.replace(anchor, addition, 1))
    print("  OK    billing_chat_router registered")


def main():
    print("Praesidium — Billing Chat Wire-Up v4 (files only)")
    print("=" * 60)

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

    print("\nDone. Restart:")
    print("  docker exec praesidium-web find /app/modules -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true")
    print("  docker restart praesidium-web")


if __name__ == "__main__":
    main()
