#!/usr/bin/env python3
"""
Praesidium AI Layer — Call Site Refactor Script
==================================================
Applies the anthropic_adapter to the four existing hardcoded call sites.

    1. billing/api/intake_api.py       — intake document analysis
    2. admin/timesheet_api.py           — timesheet reconciliation
    3. ediscovery/routes/chat.py        — matter chat (two call sites)
    4. ediscovery/tag_intelligence.py   — tag suggestions

Run on WEB-01 after applying migration 0044_ai_layer_foundation and seed SQL.

    python3 refactor_call_sites.py --apply
    python3 refactor_call_sites.py --verify   # dry-run, reports diffs

Each refactor performs an idempotent in-place str-replace. If the target file
is already refactored, the script detects and skips. Makes .bak backups.

Architectural notes:
    - Adapter calls go through modules.intelligence.call() with an AICallContext
    - Raw httpx POSTs and _get_api_key helpers are removed
    - Request metadata (template, routing) flows to ai_api_calls automatically
    - Response headers with utilization/warnings surface to HTMX for the
      ambient budget-warning UX
    - The chat module's multi-turn messages array is preserved via
      raw_user_prompt + raw_system_prompt (template stub only documents it)

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Refactor blocks — each is (file_path, marker, before, after)
# `marker` is a short string unique to the refactored state; if present, the
# script knows the refactor has already been applied and skips.
# ---------------------------------------------------------------------------

ROOT = Path("/app")

REFACTORS = []

# -----------------------------------------------------------------------
# 1. billing/api/intake_api.py
#    Replace: _get_api_key helper + httpx POST block with adapter call.
# -----------------------------------------------------------------------
REFACTORS.append({
    "name": "billing.intake_api",
    "path": ROOT / "modules/billing/api/intake_api.py",
    "marker": "from modules.intelligence import call as ai_call",
    "replacements": [
        # Import block: replace httpx import and add adapter import
        (
            "import httpx\nfrom fastapi import APIRouter, File, Request, UploadFile\nfrom fastapi.responses import JSONResponse\nfrom sqlalchemy import text\n\nfrom core.db.base import AsyncSessionLocal",
            "from fastapi import APIRouter, File, Request, UploadFile\nfrom fastapi.responses import JSONResponse\n\nfrom modules.intelligence import (\n    call as ai_call,\n    resolve_prompt,\n    AICallContext,\n    AICapBreach,\n    AILayerError,\n    strip_markdown_fences,\n)",
        ),
        # Remove _get_api_key helper entirely — adapter handles this
        # (scan by the unique function signature, stop at the next blank+def)
        # We replace the whole block from the comment banner down through the
        # last line of the function. Keep the rest of the file unchanged.
        (
            "# ---------------------------------------------------------------------------\n# API key helper — identical to ediscovery/routes/chat.py pattern\n# ---------------------------------------------------------------------------\n\nasync def _get_api_key(tenant_id: str) -> str:",
            "# ---------------------------------------------------------------------------\n# API key, routing, prompt rendering, cost caps, and call attribution are now\n# handled by modules.intelligence.anthropic_adapter.\n# ---------------------------------------------------------------------------\n\nasync def _unused_api_key_stub(tenant_id: str) -> str:",
        ),
    ],
    # Final multi-line replacement that swaps the ad-hoc httpx block for adapter call.
    # Applied after the above — sectioned into its own list for clarity.
    "block_replacements": [
        {
            "before": """    try:
        api_key = await _get_api_key(tenant_id)

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":          api_key,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages":   [{"role": "user", "content": prompt}],
                }
            )

        if resp.status_code != 200:
            logger.error("Anthropic API error: %s", resp.text[:300])
            return JSONResponse(
                {"error": "AI analysis failed. Check API key configuration."},
                status_code=502,
            )

        data     = resp.json()
        raw_text = data["content"][0]["text"]

        # Parse JSON — strip any markdown fences
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        fields = json.loads(raw_text)
        return JSONResponse({"extracted_fields": fields, "status": "ok"})""",
            "after": """    try:
        # Render prompt from prompt_templates (versioned)
        rendered = await resolve_prompt(
            tenant_id=tenant_id,
            slug="billing.intake_analyze",
            variables={"document_text": combined[:10000]},
        )
        # Get user_id if present in request state for attribution
        user_id = getattr(getattr(request.state, "user", None), "id", None)
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="billing",
            purpose="intake_analyze",
            user_id=user_id,
            matter_id=None,  # Intake is pre-matter by definition
        )
        result = await ai_call(ctx, prompt=rendered)

        # Parse JSON — strip any markdown fences (adapter utility)
        raw_text = strip_markdown_fences(result.text)
        fields = json.loads(raw_text)
        response = JSONResponse({"extracted_fields": fields, "status": "ok"})
        # Forward budget warnings as headers for ambient UX
        for k, v in result.to_headers().items():
            response.headers[k] = v
        return response

    except AICapBreach as exc:
        return JSONResponse(
            {"error": "AI budget cap reached for this workflow.",
             "breach": exc.context},
            status_code=429,
        )
    except AILayerError as exc:
        logger.error("AI layer error in intake: %s", exc)
        return JSONResponse(
            {"error": "AI analysis failed. Check API key configuration."},
            status_code=502,
        )""",
        },
    ],
})

# -----------------------------------------------------------------------
# 2. admin/timesheet_api.py
#    Replace: ANTHROPIC_MODEL constant + httpx call block.
# -----------------------------------------------------------------------
REFACTORS.append({
    "name": "admin.timesheet_api",
    "path": ROOT / "modules/admin/timesheet_api.py",
    "marker": "from modules.intelligence import call as ai_call",
    "replacements": [
        (
            'ANTHROPIC_MODEL = "claude-sonnet-4-20250514"',
            "# Model routing now lives in ai_model_routing — see modules.intelligence",
        ),
    ],
    "block_replacements": [
        # Timesheet api has a long prompt string followed by an httpx POST.
        # We keep the prompt assembly (matter_list, entries_summary) but swap
        # the httpx POST for adapter.call with the DB prompt template.
        {
            "before": """    prompt = f\"\"\"You are a legal billing assistant helping attorney {user_name} reconstruct their timesheet for {date_from} to {date_to}.

Active matters:
{matter_list or '  (none found)'}

Raw time entries from all sources (index: [source] date | hours | matter | description):
{chr(10).join(entries_summary)}

For each entry, respond with a JSON array where each element has:
- index: the entry index number
- matter_id: matched matter ID string or null
- matter_name: matched matter name or null
- hours: suggested hours (float, rounded to nearest 0.25)
- narrative: professional billing narrative (concise, specific, billable)
- confidence: 0.0 to 1.0 confidence score for matter attribution
- notes: brief explanation of your attribution decision

Rules:
1. Match entries to matters based on context clues in descriptions
2. For ai_api_calls entries, try to attribute based on module names
3. For phone entries with no context, leave matter_id null and confidence low
4. Timeslips entries are already attributed — keep their matter, improve narrative only
5. Flag duplicate or overlapping entries in notes
6. Respond ONLY with the JSON array, no other text.\"\"\"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(""",
            "after": """    try:
        rendered = await resolve_prompt(
            tenant_id=tenant_id,
            slug="admin.timesheet_reconcile",
            variables={
                "user_name":       user_name,
                "date_from":       str(date_from),
                "date_to":         str(date_to),
                "matter_list":     matter_list or "  (none found)",
                "entries_summary": chr(10).join(entries_summary),
            },
        )
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="admin",
            purpose="timesheet_reconcile",
            matter_id=None,  # Cross-matter by design — routes to firm_overhead
        )
        result = await ai_call(ctx, prompt=rendered)
        raw_text = strip_markdown_fences(result.text)
        enriched_entries = json.loads(raw_text)
        return enriched_entries
    except json.JSONDecodeError as exc:
        logger.error("timesheet reconcile JSON parse error: %s", exc)
        return entries  # Return originals on parse failure
    except AILayerError as exc:
        logger.error("timesheet reconcile AI error: %s", exc)
        return entries

    # Legacy block retained for reference only — never executed:
    if False:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(""",
        },
    ],
    "import_add": "from modules.intelligence import (\n    call as ai_call,\n    resolve_prompt,\n    AICallContext,\n    AILayerError,\n    strip_markdown_fences,\n)",
    "import_anchor": "import httpx",
})

# -----------------------------------------------------------------------
# 3. ediscovery/routes/chat.py
#    Two call sites share the same pattern. For the chat endpoint (multi-turn),
#    we preserve the messages array and pass it through as raw_user_prompt
#    with the context system prompt built from the template variable.
# -----------------------------------------------------------------------
REFACTORS.append({
    "name": "ediscovery.chat",
    "path": ROOT / "modules/ediscovery/routes/chat.py",
    "marker": "from modules.intelligence import call as ai_call",
    "replacements": [],
    "block_replacements": [
        # First httpx call site
        {
            "before": """    # Call Claude
    try:
        import httpx
        api_key = await _get_api_key(tenant_id)

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":""",
            "after": """    # Call Claude via the adapter
    try:
        # Multi-turn chat: collapse messages into a single user_prompt.
        # The system_prompt is pulled from SYSTEM_PROMPTS; matter_context is
        # appended per existing behavior. Template slug documents the shape;
        # we pass system + user directly to preserve multi-turn fidelity.
        user_messages_text = "\\n\\n".join(
            f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
            for m in messages
        )
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="ediscovery",
            purpose="matter_chat",
            matter_id=matter_id or None,
            user_id=getattr(user, "id", None) if user else None,
        )
        ai_result = await ai_call(
            ctx,
            raw_system_prompt=system_prompt,
            raw_user_prompt=user_messages_text,
        )
        reply = ai_result.text
        search_terms, boolean_query = _extract_search_terms(reply)
        response = JSONResponse({
            "reply": reply,
            "search_terms": search_terms,
            "boolean_query": boolean_query,
        })
        for k, v in ai_result.to_headers().items():
            response.headers[k] = v
        return response

    except AICapBreach as exc:
        return JSONResponse(
            {"error": "AI budget cap reached for this matter.",
             "breach": exc.context},
            status_code=429,
        )
    except AILayerError as exc:
        logger.error("Chat AI error: %s", exc)
        return JSONResponse({"error": "Chat unavailable."}, status_code=502)

    # Legacy paths retained only for reference — never executed:
    if False:
        import httpx
        api_key = await _get_api_key(tenant_id)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":""",
        },
    ],
    "import_add": "from modules.intelligence import (\n    call as ai_call,\n    AICallContext,\n    AILayerError,\n    AICapBreach,\n)",
    "import_anchor": "from fastapi.responses import JSONResponse",
})

# -----------------------------------------------------------------------
# 4. ediscovery/tag_intelligence.py
#    Uses the official anthropic SDK rather than httpx. Swap to adapter.
# -----------------------------------------------------------------------
REFACTORS.append({
    "name": "ediscovery.tag_intelligence",
    "path": ROOT / "modules/ediscovery/tag_intelligence.py",
    "marker": "from modules.intelligence import call as ai_call",
    "replacements": [],
    "block_replacements": [
        {
            "before": """    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        recommendations = json.loads(raw)""",
            "after": """    try:
        # Use the adapter — routing row ediscovery.tag_suggest
        # (primary: haiku, no fallback, per-matter cap $10/day)
        ctx = AICallContext(
            tenant_id=tenant_id,
            module="ediscovery",
            purpose="tag_suggest",
            matter_id=matter_id,
        )
        ai_result = await ai_call(ctx, raw_user_prompt=prompt)
        raw = strip_markdown_fences(ai_result.text)
        recommendations = json.loads(raw)""",
        },
    ],
    "import_add": "from modules.intelligence import (\n    call as ai_call,\n    AICallContext,\n    AILayerError,\n    strip_markdown_fences,\n)",
    "import_anchor": "import logging",
})


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _apply_replacements(text: str, replacements: list) -> tuple[str, int]:
    """Apply a list of (before, after) replacements. Returns (text, n_applied)."""
    applied = 0
    for before, after in replacements:
        if before in text:
            text = text.replace(before, after, 1)
            applied += 1
    return text, applied


def _apply_block_replacements(text: str, blocks: list) -> tuple[str, int]:
    applied = 0
    for block in blocks:
        before = block["before"]
        after = block["after"]
        if before in text:
            text = text.replace(before, after, 1)
            applied += 1
    return text, applied


def _insert_import(text: str, anchor: str, addition: str) -> tuple[str, bool]:
    """Insert `addition` after the line containing `anchor`, if not present."""
    if addition in text:
        return text, False
    if anchor not in text:
        return text, False
    # Insert after the anchor line
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if anchor in ln:
            lines.insert(i + 1, addition)
            return "\n".join(lines), True
    return text, False


def process_refactor(spec: dict, *, apply: bool, verbose: bool) -> dict:
    path: Path = spec["path"]
    name = spec["name"]
    result = {"name": name, "path": str(path), "status": "unknown",
              "changes": 0}

    if not path.exists():
        result["status"] = "missing"
        return result

    original = path.read_text()

    # Idempotency check
    if spec.get("marker") and spec["marker"] in original:
        result["status"] = "already_applied"
        return result

    text = original
    changes = 0

    # String replacements
    if spec.get("replacements"):
        text, n = _apply_replacements(text, spec["replacements"])
        changes += n

    # Block replacements
    if spec.get("block_replacements"):
        text, n = _apply_block_replacements(text, spec["block_replacements"])
        changes += n

    # Import insertion
    if spec.get("import_add") and spec.get("import_anchor"):
        text, added = _insert_import(
            text, spec["import_anchor"], spec["import_add"]
        )
        if added:
            changes += 1

    if changes == 0:
        result["status"] = "no_match"
        return result

    if verbose:
        print(f"  [{name}] {changes} changes pending")

    if apply:
        backup = path.with_suffix(path.suffix + ".bak-ai-layer")
        shutil.copy2(path, backup)
        path.write_text(text)
        result["status"] = "applied"
        result["backup"] = str(backup)
    else:
        result["status"] = "would_apply"

    result["changes"] = changes
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (otherwise dry-run)")
    parser.add_argument("--verify", action="store_true",
                        help="Report which files need refactoring")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Praesidium AI Layer — Call Site Refactor [{mode}]")
    print("=" * 60)

    exit_code = 0
    for spec in REFACTORS:
        result = process_refactor(
            spec, apply=args.apply, verbose=args.verbose
        )
        status = result["status"]
        icon = {
            "applied":          "✓",
            "would_apply":      "→",
            "already_applied":  "·",
            "no_match":         "✗",
            "missing":          "?",
        }.get(status, "?")
        print(f"  {icon} {result['name']:40s} {status:20s} "
              f"({result['changes']} changes)")
        if status in ("no_match", "missing"):
            exit_code = 1

    if exit_code:
        print("\nSome refactors could not be applied. Check the files "
              "manually.")
    elif args.apply:
        print("\nAll refactors applied. Restart praesidium-web:")
        print("    docker restart praesidium-web")
        print("\nRun pytest to verify:")
        print("    bash r.sh")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
