"""
ai_matcher.py
=============

Pass 2 of the M9 AI Reconciliation Engine: matter assignment via a local
Claude agent loop using Opus 4.7 and the curated tools registered in
reconciliation_tools.py.

ROLE IN PIPELINE
----------------
Pass 1 (ai_classifier.py) classifies drafts as billable / personal /
ambiguous. Personal drafts skip Pass 2. Billable + ambiguous drafts arrive
here, grouped by entry_date.

For each day batch:
  1. Render a prompt describing every draft in that day
  2. Call Opus 4.7 with the curated tool registry from reconciliation_tools
  3. Run a local agent loop: when Claude returns tool_use blocks, execute
     them via dispatch_tool() and feed results back as tool_result blocks
  4. Cap the loop at MAX_ITERATIONS to prevent runaway agents
  5. When Claude returns its final text response (a JSON array of matches),
     parse it, write the audit rows + draft updates inside one transaction

DESIGN CONSTRAINTS
------------------
- Opus 4.7 (claude-opus-4-7). NO temperature/top_p/top_k parameters
  (returns 400 on Opus 4.7).
- Hard cap at 8 tool-call iterations per batch.
- Per-day batches: all drafts for a given entry_date in one Claude call.
- ALWAYS REQUIRES HUMAN REVIEW: every match writes status='ai_review'.
  No auto-assignment regardless of confidence. Confidence is used only
  for UI sort order.
- Audit rows go into timesheet_ai_matches with full reasoning + tool_calls.
- The matcher PROMPT explicitly instructs Claude not to echo personal
  ManicTime activity in its reasoning_text. Pass 1 already filtered most
  personals out, but the redaction guard is belt-and-suspenders.
- Fail-safe: any API/agent failure → that batch's drafts get an audit row
  with outcome='fallback' or 'error' and remain status='pending' (rule
  ladder match stands).

INTEGRATION POINT
-----------------
Called from timesheet_reconcile_job._run_async after Pass 1 classification
and after rule-ladder draft writes. Reads drafts from timesheet_drafts,
runs Pass 2, writes timesheet_ai_matches rows + updates drafts.

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from jobs.reconciliation_tools import (
    ANTHROPIC_TOOL_DEFS,
    dispatch_tool,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
OPUS_MODEL = "claude-opus-4-7"

# Hard cap on agent-loop iterations. After this many tool-use rounds we
# force Claude to return a final answer. 8 is enough for ~5 lookups + a
# couple of refinement queries; runaway loops are a real risk on Opus.
MAX_ITERATIONS = 8

# Max output tokens per single API call inside the agent loop.
# Opus 4.7 supports up to 128k; we use 32k to give plenty of headroom for
# the new tokenizer (1.35x token usage) plus tool_use overhead. Output
# usage is typically 500-2000 tokens per draft batch.
MAX_OUTPUT_TOKENS = 32000

# HTTP timeout per call. Opus + tool-use can be slow on hard problems.
HTTP_TIMEOUT_SEC = 180

# Per-draft text caps when constructing the prompt (privacy + token economy)
MAX_DESC_CHARS = 300
MAX_DETAIL_CHARS = 200


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a legal-billing reconciliation specialist. Your \
job is to look at a day's worth of timesheet drafts from a law firm and \
match each draft to the correct client matter.

You have access to curated database tools that let you look up contacts, \
matters, prior time entries, and email-routing history for the firm. Use \
them strategically. You are NOT permitted to write to the database — only \
read.

WORKFLOW
========
1. Read the entire day's drafts first to spot patterns. Multiple drafts \
often relate to the same matter (a phone call to Bill, then four emails \
with Bill, then a calendar block — likely all the same case).

2. Investigate the strongest signals first:
   - Phone numbers and emails should be looked up via find_contact_by_phone \
or find_contact_by_email; then call get_matters_for_contact on any contact \
hits to see what matters they're linked to.
   - For email drafts where the counterparty has already had emails routed \
elsewhere, find_email_thread_matter is a strong cross-source signal.
   - For draft descriptions mentioning a specific matter name or party \
(e.g. "Hat Creek brief", "Trivedi closing"), use search_matters.
   - For ambiguous drafts where you have an attorney's source_tk_id, \
get_recent_slips_for_attorney shows their billing patterns.

3. Once you have a candidate matter, optionally call get_matter_details to \
confirm.

4. Decide the match for every draft. You may decide a draft cannot be \
confidently matched — that is a valid output.

CRITICAL TOOL USAGE NOTES
=========================
- get_recent_slips_for_attorney requires a REAL source_tk_id (a string \
identifying the attorney in the legacy timekeeper table). If the user \
message does not provide an "Attorney source_tk_id" line, DO NOT call this \
tool — there is no reliable value to pass. Calling it with the user_id \
or a guessed value will return empty or wrong results.
- For matter_id values, only use UUIDs that came back from a tool result. \
Never invent a UUID.
- Phone numbers can be passed in any format (parens, dashes, spaces are OK).

OUTPUT FORMAT
=============
After your investigation, return a SINGLE JSON array. One element per draft \
in the input order. Each element has these fields:

  {
    "draft_id": "<the draft id you were given>",
    "matter_id": "<UUID string>" | null,
    "matter_name": "<human name>" | null,
    "confidence": <number, 0.00 to 1.00>,
    "reasoning": "<short paragraph (2-4 sentences max)>"
  }

Output ONLY the JSON array. No prose before or after. No code fences.

CONFIDENCE RUBRIC
=================
  0.95+ : Direct contact link via matter_contacts; or party email with \
existing routed emails to the same matter
  0.80+ : Strong inferential match (matter name keyword + party phone/email \
matches a known matter contact)
  0.60+ : Reasonable inference (matter keyword in description, OR \
historical billing pattern for this attorney + party)
  0.40+ : Weak inference (matter keyword only, or party hint only)
  <0.40 : Insufficient signal — return matter_id=null and explain why

REASONING GUARDRAILS — IMPORTANT
================================
The reasoning field is stored in an audit log subject to legal review. \
Follow these rules:

- Describe HOW you matched, not WHAT was in any private content.
- For ManicTime activity, refer to it by category ("browser activity", \
"document editing"), NOT by URL or document title.
- Do not quote message text, email body content, or transcript fragments.
- Do not speculate about personal life. If a draft looks personal but \
slipped past the classifier, set matter_id=null with reasoning like \
"draft appears non-billable; no matter assignment."
- Mention specific contacts and matters by name when they directly explain \
the match — that is appropriate audit trail content.

If you have not yet returned the JSON array after several tool calls, \
return your best-effort answer with low confidence rather than continuing \
to investigate."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def _truncate(value: Optional[str], n: int) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _render_draft_for_prompt(draft: Dict[str, Any]) -> str:
    """Render a single draft as a compact line for the matcher prompt."""
    detail = draft.get("source_detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (ValueError, TypeError):
            detail = {}
    if not isinstance(detail, dict):
        detail = {}

    parts = [f"draft_id={draft.get('id')!r}"]
    parts.append(f"src={draft.get('source', 'unknown')}")
    if draft.get("entry_date"):
        parts.append(f"date={draft['entry_date']}")
    if draft.get("hours") is not None:
        parts.append(f"hrs={draft['hours']}")

    desc = _truncate(draft.get("description"), MAX_DESC_CHARS)
    if desc:
        parts.append(f"desc={desc!r}")

    # Source-specific hints (only the lookup keys, not full content)
    source = draft.get("source", "")
    if source in ("phone_csv", "imazing_csv"):
        # Phone number hint goes here for the matcher to look up
        # We pull the raw_number from detail since description may not have it
        raw = detail.get("raw_number")
        if raw:
            parts.append(f"phone={raw!r}")
        cp_phone = detail.get("cp_phone") or ""
        if cp_phone and not raw:
            parts.append(f"phone={cp_phone!r}")

    if source == "exchange_email":
        from_email = detail.get("from") or ""
        if from_email:
            parts.append(f"from={from_email!r}")
        # If pre-matched by the email connector, expose that as a hint
        pre = detail.get("pre_matched_matter")
        if pre and pre != "None":
            parts.append(f"pre_routed_matter_id={pre!r}")

    if source == "exchange_calendar":
        organizer = detail.get("organizer") or ""
        if organizer:
            parts.append(f"organizer={_truncate(organizer, 80)!r}")
        pre = detail.get("pre_matched_matter")
        if pre and pre != "None":
            parts.append(f"pre_routed_matter_id={pre!r}")

    if source == "manictime":
        app = _truncate(detail.get("group") or "", 80)
        if app:
            parts.append(f"app={app!r}")

    # Pass-1 classifier hint (informational only — Claude is allowed to disagree)
    cls = detail.get("pass1_classification")
    if cls:
        parts.append(f"pass1={cls}")

    # Rule ladder's tentative guess (so Claude can confirm or override)
    if draft.get("matter_id"):
        parts.append(f"rule_ladder_guess={draft['matter_id']!r}")
    if draft.get("ai_confidence") is not None:
        parts.append(f"rule_ladder_conf={draft['ai_confidence']}")

    return " ".join(parts)


def _build_user_message(day: _date, drafts: List[Dict[str, Any]],
                        attorney_context: Optional[Dict[str, Any]] = None) -> str:
    """Render a day's drafts as the user message."""
    lines = []
    lines.append(f"Day: {day.isoformat()}")
    if attorney_context:
        lines.append(f"Attorney user_id: {attorney_context.get('user_id')}")
        if attorney_context.get("source_tk_id"):
            lines.append(f"Attorney source_tk_id (for ts_slips lookup): "
                         f"{attorney_context['source_tk_id']}")
    lines.append(f"Total drafts to match: {len(drafts)}")
    lines.append("")
    lines.append("DRAFTS:")
    for d in drafts:
        lines.append("  " + _render_draft_for_prompt(d))
    lines.append("")
    lines.append(
        "Match each draft above to a matter using the available tools. "
        "Return a JSON array of {draft_id, matter_id, matter_name, "
        "confidence, reasoning} objects, one per draft, in the same order."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API call + agent loop
# ---------------------------------------------------------------------------
async def _call_opus(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Single-turn call to Opus 4.7 with the curated tool set.

    Note: Opus 4.7 rejects temperature/top_p/top_k. We omit them.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set; matcher disabled")
        return None

    payload = {
        "model": OPUS_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": ANTHROPIC_TOOL_DEFS,
        "messages": messages,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
            resp = await client.post(
                ANTHROPIC_API_URL, headers=headers, json=payload
            )
        if resp.status_code != 200:
            log.warning("Opus matcher API returned %d: %s",
                        resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except Exception as exc:
        log.warning("Opus matcher API call failed: %s", exc)
        return None


def _extract_final_text(content_blocks: List[Dict[str, Any]]) -> str:
    """Concatenate all text blocks from a response."""
    parts = []
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "".join(parts).strip()


def _parse_match_array(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse Claude's final JSON array.

    Handles three response shapes that Opus 4.7 emits despite the prompt
    instructions to output ONLY the array:
      1. Clean array:           [{"draft_id":...}]
      2. Code-fenced array:     ```json\n[...]\n```
      3. Prose-prefixed array:  "Some explanation text\n[...]"
                                or  "[...]\nSome trailing thoughts"

    We strip code fences, then locate the outermost top-level JSON array by
    finding the first '[' that has a matching ']' that contains the closing
    of the bracket-balanced span. Robust against arrays containing strings
    with embedded brackets.
    """
    if not raw_text:
        return None
    txt = raw_text.strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()

    # Fast path: text starts with a bracket
    if txt.startswith("["):
        candidate = txt
    else:
        # Locate the first top-level '[' that is not inside a string
        candidate = _extract_first_json_array(txt)
        if candidate is None:
            log.warning(
                "Matcher returned text with no JSON array detected.\n"
                "  raw_text length: %d\n"
                "  head (first 500c): %s\n"
                "  tail (last 200c):  %s",
                len(raw_text), raw_text[:500],
                raw_text[-200:] if len(raw_text) > 200 else "",
            )
            return None

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        log.warning(
            "Matcher returned non-JSON output: %s\n"
            "  raw_text length: %d\n"
            "  head (first 500c): %s\n"
            "  tail (last 200c):  %s",
            exc, len(raw_text), raw_text[:500],
            raw_text[-200:] if len(raw_text) > 200 else "",
        )
        return None
    if not isinstance(parsed, list):
        log.warning("Matcher returned non-array output type=%s",
                    type(parsed).__name__)
        return None
    return parsed


def _extract_first_json_array(text: str) -> Optional[str]:
    """Scan text for a balanced top-level JSON array. Returns the substring
    starting at the opening '[' through its matching ']', accounting for
    strings (which may contain unescaped brackets) and escape sequences.
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        for j in range(i, n):
            ch = text[j]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[i : j + 1]
        # Found '[' but no matching ']' — give up here; try next '['
        i += 1
    return None


async def _run_agent_loop(
    tenant_id: str,
    initial_user_message: str,
) -> Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, Any]], Dict[str, int]]:
    """Run the local tool-use loop.

    Returns:
        (parsed_match_array | None, tool_call_log, token_usage)
        - parsed_match_array: the final JSON array Claude returned, or None
          if the loop ended without a final answer
        - tool_call_log: list of {tool, input, output_summary} dicts (audit)
        - token_usage: {"input_tokens": int, "output_tokens": int} (totals
          across all loop iterations)
    """
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": initial_user_message},
    ]
    tool_call_log: List[Dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0

    for iteration in range(MAX_ITERATIONS):
        log.info("Matcher loop iteration %d/%d", iteration + 1, MAX_ITERATIONS)
        resp = await _call_opus(messages)
        if resp is None:
            return None, tool_call_log, {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        usage = resp.get("usage") or {}
        total_input_tokens += int(usage.get("input_tokens") or 0)
        total_output_tokens += int(usage.get("output_tokens") or 0)

        content = resp.get("content") or []
        stop_reason = resp.get("stop_reason")
        log.info("  iter %d stop_reason=%s in_tok=%s out_tok=%s",
                 iteration + 1, stop_reason,
                 usage.get("input_tokens"), usage.get("output_tokens"))

        # Append the assistant turn to message history
        messages.append({"role": "assistant", "content": content})

        # If Claude stopped naturally (end_turn) or hit max_tokens, parse the
        # text content as the final answer.
        if stop_reason != "tool_use":
            final_text = _extract_final_text(content)
            if stop_reason == "max_tokens":
                log.warning(
                    "Matcher hit max_tokens before completing answer; "
                    "final_text length=%d (likely truncated JSON)",
                    len(final_text),
                )
            parsed = _parse_match_array(final_text)
            return parsed, tool_call_log, {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        # Otherwise: execute tool_use blocks
        tool_results: List[Dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            tool_name = block.get("name")
            tool_input = block.get("input") or {}
            log.info("  -> tool_use: %s(%s)", tool_name,
                     ", ".join(f"{k}={v!r}" for k, v in tool_input.items()))

            # Execute via the registry
            result = await dispatch_tool(tool_name, tenant_id, **tool_input)

            # Audit log entry (truncated for storage economy)
            output_str = json.dumps(result, default=str)
            tool_call_log.append({
                "iter": iteration + 1,
                "tool": tool_name,
                "input": tool_input,
                "output_ok": result.get("ok"),
                "output_summary": _truncate(output_str, 500),
            })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": output_str,
                "is_error": not result.get("ok", False),
            })

        if not tool_results:
            # Defensive: Claude said tool_use but emitted no tool_use blocks
            log.warning("Matcher signaled tool_use but emitted no tool blocks")
            return None, tool_call_log, {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        messages.append({"role": "user", "content": tool_results})

    # Hit iteration cap. Force one final non-tool turn to extract any answer.
    log.warning("Matcher hit MAX_ITERATIONS=%d; forcing final answer",
                MAX_ITERATIONS)
    messages.append({
        "role": "user",
        "content": (
            "You have used the maximum number of tool calls. Return your "
            "best-effort JSON array of matches now, with low confidence "
            "values where you are unsure. Do not call any more tools."
        ),
    })
    resp = await _call_opus(messages)
    if resp is None:
        return None, tool_call_log, {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }
    usage = resp.get("usage") or {}
    total_input_tokens += int(usage.get("input_tokens") or 0)
    total_output_tokens += int(usage.get("output_tokens") or 0)
    final_text = _extract_final_text(resp.get("content") or [])
    parsed = _parse_match_array(final_text)
    return parsed, tool_call_log, {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }


# ---------------------------------------------------------------------------
# Persistence: write timesheet_ai_matches rows + update drafts
# ---------------------------------------------------------------------------
async def _persist_match_results(
    tenant_id: str,
    session_id: str,
    batch_id: str,
    source_label: str,
    drafts: List[Dict[str, Any]],
    matches: List[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    token_usage: Dict[str, int],
    ladder_matter_names: Dict[str, str],
) -> int:
    """Write one timesheet_ai_matches row per draft and update each draft's
    matter assignment + status. Returns count of drafts updated.

    Always sets status='ai_review' regardless of confidence — per the
    "always require human review" decision. Confidence is recorded for UI
    sort order only.
    """
    # Index matches by draft_id for O(1) lookup
    matches_by_id: Dict[str, Dict[str, Any]] = {}
    for m in matches:
        if not isinstance(m, dict):
            continue
        did = m.get("draft_id")
        if did:
            matches_by_id[str(did)] = m

    updated = 0
    async with AsyncSessionLocal() as db:
        for draft in drafts:
            draft_id = str(draft["id"])
            match = matches_by_id.get(draft_id)

            if match is None:
                # Claude returned no match for this draft; record an audit
                # row with outcome='no_match' so we have a complete trail.
                await db.execute(
                    text("""
                        INSERT INTO timesheet_ai_matches
                            (id, tenant_id, draft_id, session_id, matter_id,
                             matter_name, confidence, reasoning_text,
                             tool_calls, model, input_tokens, output_tokens,
                             source_label, batch_id, outcome)
                        VALUES
                            (:id, :tid, :did, :sid, NULL,
                             NULL, NULL, :reason,
                             CAST(:tcalls AS jsonb), :model, :in_tok, :out_tok,
                             :src, :bid, 'no_match')
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": tenant_id.strip(),
                        "did": draft_id,
                        "sid": session_id,
                        "reason": "Matcher returned no entry for this draft",
                        "tcalls": json.dumps(tool_calls),
                        "model": OPUS_MODEL,
                        "in_tok": token_usage.get("input_tokens", 0),
                        "out_tok": token_usage.get("output_tokens", 0),
                        "src": source_label,
                        "bid": batch_id,
                    },
                )
                continue

            matter_id = match.get("matter_id")
            if matter_id == "" or matter_id == "null":
                matter_id = None
            matter_name = match.get("matter_name")
            confidence = match.get("confidence")
            reasoning = match.get("reasoning") or ""

            # Validate matter_id is a UUID-ish string before binding it as UUID.
            mid_for_cast: Optional[str] = None
            if matter_id and isinstance(matter_id, str):
                if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", matter_id):
                    mid_for_cast = matter_id

            # If the matter_name wasn't provided but we know it from the rule
            # ladder's name lookup, fill it in.
            if mid_for_cast and not matter_name:
                matter_name = ladder_matter_names.get(mid_for_cast)

            outcome = "matched" if mid_for_cast else "no_match"

            # 1. Insert audit row
            await db.execute(
                text("""
                    INSERT INTO timesheet_ai_matches
                        (id, tenant_id, draft_id, session_id, matter_id,
                         matter_name, confidence, reasoning_text,
                         tool_calls, model, input_tokens, output_tokens,
                         source_label, batch_id, outcome)
                    VALUES
                        (:id, :tid, :did, :sid, CAST(:mid AS uuid),
                         :mname, :conf, :reason,
                         CAST(:tcalls AS jsonb), :model, :in_tok, :out_tok,
                         :src, :bid, :outcome)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "tid": tenant_id.strip(),
                    "did": draft_id,
                    "sid": session_id,
                    "mid": mid_for_cast,
                    "mname": matter_name,
                    "conf": confidence,
                    "reason": reasoning,
                    "tcalls": json.dumps(tool_calls),
                    "model": OPUS_MODEL,
                    "in_tok": token_usage.get("input_tokens", 0),
                    "out_tok": token_usage.get("output_tokens", 0),
                    "src": source_label,
                    "bid": batch_id,
                    "outcome": outcome,
                },
            )

            # 2. Update draft (always status='ai_review', even on high
            # confidence, per the locked decision)
            if mid_for_cast:
                await db.execute(
                    text("""
                        UPDATE timesheet_drafts
                           SET matter_id = CAST(:mid AS uuid),
                               matter_name = :mname,
                               ai_confidence = :conf,
                               ai_narrative = :reason,
                               status = 'ai_review'
                         WHERE id = :did
                    """),
                    {
                        "mid": mid_for_cast,
                        "mname": matter_name,
                        "conf": confidence,
                        "reason": _truncate(reasoning, 1000),
                        "did": draft_id,
                    },
                )
                updated += 1
            else:
                # No matter assignment, but still record reasoning + status
                await db.execute(
                    text("""
                        UPDATE timesheet_drafts
                           SET ai_confidence = :conf,
                               ai_narrative = :reason,
                               status = 'ai_review'
                         WHERE id = :did
                    """),
                    {
                        "conf": confidence,
                        "reason": _truncate(reasoning, 1000),
                        "did": draft_id,
                    },
                )

        await db.commit()
    return updated


async def _persist_batch_failure(
    tenant_id: str,
    session_id: str,
    batch_id: str,
    source_label: str,
    drafts: List[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    token_usage: Dict[str, int],
    error_message: str,
) -> None:
    """Audit row per draft when a batch fails. Drafts keep rule-ladder
    assignments; status stays 'pending' so they show in the normal review.
    """
    async with AsyncSessionLocal() as db:
        for draft in drafts:
            await db.execute(
                text("""
                    INSERT INTO timesheet_ai_matches
                        (id, tenant_id, draft_id, session_id, matter_id,
                         matter_name, confidence, reasoning_text,
                         tool_calls, model, input_tokens, output_tokens,
                         source_label, batch_id, outcome, error_message)
                    VALUES
                        (:id, :tid, :did, :sid, NULL,
                         NULL, NULL, NULL,
                         CAST(:tcalls AS jsonb), :model, :in_tok, :out_tok,
                         :src, :bid, 'fallback', :err)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "tid": tenant_id.strip(),
                    "did": str(draft["id"]),
                    "sid": session_id,
                    "tcalls": json.dumps(tool_calls),
                    "model": OPUS_MODEL,
                    "in_tok": token_usage.get("input_tokens", 0),
                    "out_tok": token_usage.get("output_tokens", 0),
                    "src": source_label,
                    "bid": batch_id,
                    "err": _truncate(error_message, 500),
                },
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_matcher(
    tenant_id: str,
    session_id: str,
    drafts: List[Dict[str, Any]],
    attorney_context: Optional[Dict[str, Any]] = None,
    ladder_matter_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run Pass 2 matching across the given drafts.

    Args:
        tenant_id: Tenant scope for tool calls.
        session_id: Reconcile session id (for audit linkage).
        drafts: List of draft dicts that need matching. Should already have
                been filtered to billable+ambiguous by Pass 1; personal
                drafts will be ignored if passed in.
        attorney_context: Optional dict with user_id and source_tk_id, used
                to enable get_recent_slips_for_attorney.
        ladder_matter_names: Optional {matter_id_str: matter_name} from the
                rule ladder, used to backfill matter_name when Claude
                returns matter_id without name.

    Returns:
        {"batches": int, "drafts_processed": int, "drafts_updated": int,
         "input_tokens_total": int, "output_tokens_total": int}
    """
    if ladder_matter_names is None:
        ladder_matter_names = {}

    if not drafts:
        return {"batches": 0, "drafts_processed": 0, "drafts_updated": 0,
                "input_tokens_total": 0, "output_tokens_total": 0}

    # Filter out personals (defense in depth — caller should have done this)
    pending = [d for d in drafts if d.get("status") != "personal"]
    skipped = len(drafts) - len(pending)
    if skipped:
        log.info("Matcher: skipping %d personal drafts", skipped)

    if not pending:
        return {"batches": 0, "drafts_processed": 0, "drafts_updated": 0,
                "input_tokens_total": 0, "output_tokens_total": 0}

    # Group by entry_date (per-day batches)
    by_day: Dict[_date, List[Dict[str, Any]]] = defaultdict(list)
    for d in pending:
        ed = d.get("entry_date")
        if isinstance(ed, str):
            try:
                ed = _date.fromisoformat(ed)
            except ValueError:
                ed = None
        if not isinstance(ed, _date):
            log.warning("Draft %s has no parseable entry_date; skipping",
                        d.get("id"))
            continue
        by_day[ed].append(d)

    total_in_tokens = 0
    total_out_tokens = 0
    total_updated = 0
    n_batches = 0

    for day in sorted(by_day.keys()):
        day_drafts = by_day[day]
        batch_id = f"day-{day.isoformat()}-{uuid.uuid4().hex[:8]}"
        log.info("Matcher: starting batch %s with %d drafts",
                 batch_id, len(day_drafts))
        n_batches += 1

        try:
            user_msg = _build_user_message(day, day_drafts, attorney_context)
            t0 = time.time()
            matches, tool_calls, usage = await _run_agent_loop(
                tenant_id, user_msg
            )
            elapsed = time.time() - t0
            log.info("Matcher batch %s loop done in %.1fs (in=%d out=%d)",
                     batch_id, elapsed,
                     usage["input_tokens"], usage["output_tokens"])

            total_in_tokens += usage["input_tokens"]
            total_out_tokens += usage["output_tokens"]

            if matches is None:
                log.warning("Matcher batch %s returned no parseable matches",
                            batch_id)
                await _persist_batch_failure(
                    tenant_id, session_id, batch_id, "per_day",
                    day_drafts, tool_calls, usage,
                    "Loop did not produce a parseable JSON answer",
                )
                continue

            updated = await _persist_match_results(
                tenant_id, session_id, batch_id, "per_day",
                day_drafts, matches, tool_calls, usage, ladder_matter_names,
            )
            total_updated += updated
            log.info("Matcher batch %s updated %d/%d drafts",
                     batch_id, updated, len(day_drafts))

        except Exception as exc:
            log.exception("Matcher batch %s failed", batch_id)
            try:
                await _persist_batch_failure(
                    tenant_id, session_id, batch_id, "per_day",
                    day_drafts, [], {"input_tokens": 0, "output_tokens": 0},
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                log.exception("Could not record batch failure audit row")

    return {
        "batches": n_batches,
        "drafts_processed": len(pending),
        "drafts_updated": total_updated,
        "input_tokens_total": total_in_tokens,
        "output_tokens_total": total_out_tokens,
    }
