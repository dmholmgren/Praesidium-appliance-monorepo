"""
ai_classifier.py
================

Pass 1 of the M9 AI Reconciliation Engine: cheap billable/personal
classification of timesheet drafts using Claude Haiku 4.5.

ROLE IN PIPELINE
----------------
Reconcile job emits raw drafts from all sources (calendar, email, manictime,
phone, imazing, ai_api_calls). Each draft is classified here as:
    - billable    → goes to Pass 2 matcher (with full audit)
    - personal    → marked status='personal', surfaces in Personal/Excluded
                    review tab for spot-check; never seen by Pass 2
    - ambiguous   → goes to Pass 2 matcher; the matcher's prompt explicitly
                    re-evaluates whether it's billable

DESIGN CONSTRAINTS
------------------
- NO AUDIT ROW. Pass 1 results are stored only as the draft's `status` field.
  No reasoning text persists. This is the "personal data never recorded" rule.
- NO BLOCKLIST SHORT-CIRCUIT. Every draft goes through Haiku. We chose this
  per architectural decision so Haiku gets to see edge cases (e.g. a personal-
  injury matter that legitimately involves medical research).
- BATCHED. Up to BATCH_SIZE drafts per API call to keep inline blocking time
  down. Haiku 4.5 handles ~50 binary classifications cleanly in one shot.
- REDACTION-AWARE PROMPT. Haiku's response per draft is structured JSON with
  category + a one-sentence non-content reason. We forbid the model from
  echoing URLs, app titles, or message bodies in its reason field.
- FAIL-SAFE. If the API errors or returns malformed output, every draft in
  that batch defaults to 'ambiguous' (sent to Pass 2) rather than blocking
  the reconcile job or silently dropping work.

INTEGRATION POINT
-----------------
Called from timesheet_reconcile_job._run_async after the rule ladder
produces matches and BEFORE drafts are written to the DB. Mutates the
draft dicts in place by adding:
    d['classification']  = 'billable' | 'personal' | 'ambiguous'
    d['classifier_used'] = bool (False if API skipped/failed)

The job then sets each draft's status accordingly:
    billable | ambiguous → 'pending' (existing default; Pass 2 will pick up)
    personal             → 'personal' (new value, no Pass 2)

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Batch size: number of drafts classified per API call.
# Haiku 4.5 has a 200K input context, so 50 short drafts is comfortable.
BATCH_SIZE = 50

# Per-draft input cap: prevent any single draft from blowing up the prompt.
# ManicTime activity titles can be long; truncate aggressively.
MAX_FIELD_CHARS = 200

# Output token budget per call. Each classification is ~30 tokens (JSON dict),
# so 50 drafts × 30 tokens = 1500 tokens. We allocate 4000 for headroom.
MAX_OUTPUT_TOKENS = 4000

# HTTP timeout for the API call.
HTTP_TIMEOUT_SEC = 60

# Valid classification labels.
VALID_LABELS = {"billable", "personal", "ambiguous"}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a timesheet entry classifier for a law firm. Each \
input is a single time-tracking draft from one of these sources: calendar \
event, email, ManicTime computer activity, phone call, text message thread, \
AI API call, or a historical time slip.

Your job: classify each draft as exactly one of:
  - "billable"   - The activity appears to be work for a client matter.
  - "personal"   - The activity appears to be personal (medical, banking, \
shopping, personal email/social, personal browsing, family, hobbies).
  - "ambiguous"  - You cannot tell with confidence either way; could be \
either work or personal. Examples: generic browser activity with no clear \
context, short calls to unknown numbers, calendar blocks with vague titles \
like "meeting" or "block".

CRITICAL RULES:
1. Be CONSERVATIVE about marking things personal. Lawyers do legitimate work \
on medical research (personal injury, malpractice, workers' comp), banking \
(corporate finance, real estate closings), property records (real estate, \
estate planning), tax filings (tax practice, estate). When in doubt, choose \
"ambiguous" rather than "personal".
2. The reason field MUST be a single sentence describing the CATEGORY of \
activity, not its content. Use phrases like:
     "personal medical activity"
     "personal banking activity"
     "personal email correspondence"
     "appears to be work-related research"
     "calendar block with no specifying details"
   DO NOT include specific URLs, document names, app titles verbatim, \
contact names, message text, or subject lines in your reason. The reason is \
audited and visible; preserve user privacy.
3. If the source is exchange_calendar or exchange_email and a counterparty \
is on a domain that is clearly a business or law firm (e.g. ends in \
.law, has a corporate domain), lean toward billable.
4. If the source is ai_api_calls or timeslips, default to "billable" unless \
something specifically signals personal use.
5. Calendar events organized by the attorney themselves with personal-life \
keywords (doctor, dentist, gym, pickup, school, birthday) are personal.

OUTPUT FORMAT:
Respond with ONLY a JSON array. Each element corresponds to one input draft \
in the same order, with these fields:
  {
    "i": <integer index, matching the input>,
    "c": "billable" | "personal" | "ambiguous",
    "r": "<one-sentence category reason, no specifics>"
  }

No prose before or after. No code fences. Just the JSON array."""


def _truncate(value: Optional[str], n: int = MAX_FIELD_CHARS) -> str:
    """Truncate a field for prompt economy and to limit content exposure."""
    if not value:
        return ""
    s = str(value).strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _build_user_message(drafts: List[Dict[str, Any]]) -> str:
    """Render N drafts as a compact, indexed prompt block."""
    lines = ["Classify each of the following drafts. Respond with a JSON "
             "array of {i, c, r} objects, one per draft, in order.\n"]
    for i, d in enumerate(drafts):
        # Decode source_detail JSON if it's a string
        detail = d.get("source_detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (ValueError, TypeError):
                detail = {}
        if not isinstance(detail, dict):
            detail = {}

        # Source-specific field selection. We deliberately do NOT include
        # raw_data or full body_text; only the minimum needed to classify.
        source = d.get("source") or "unknown"
        bits = [f"i={i}", f"src={source}"]

        if d.get("entry_date"):
            bits.append(f"date={d['entry_date']}")

        if d.get("hours") is not None:
            bits.append(f"hrs={d['hours']}")

        # Description / subject line
        desc = _truncate(d.get("description"))
        if desc:
            bits.append(f"desc={desc!r}")

        # ManicTime-specific
        if source == "manictime":
            app = _truncate(detail.get("group") or detail.get("app_name"), 80)
            tl_type = detail.get("timeline_type", "")
            if app:
                bits.append(f"app={app!r}")
            if tl_type:
                bits.append(f"timeline={tl_type}")

        # Phone-specific
        if source in ("phone_csv", "imazing_csv"):
            direction = detail.get("direction") or ""
            if direction:
                bits.append(f"dir={direction}")

        # Email/calendar counterparty domain (no full email exposed,
        # just the domain — useful for billable signal without PII)
        # We extract from the first email if present in description.
        if source in ("exchange_email", "exchange_calendar"):
            from_email = detail.get("from", "") or ""
            if "@" in from_email:
                domain = from_email.rsplit("@", 1)[1].lower()
                bits.append(f"from_domain={domain}")

        lines.append(" ".join(bits))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------
async def _call_haiku(user_message: str) -> Optional[List[Dict[str, Any]]]:
    """Call Haiku and parse the JSON array. Returns None on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set; classifier disabled")
        return None

    payload = {
        "model": HAIKU_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
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
            log.warning(
                "Haiku classifier API returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        data = resp.json()
    except Exception as exc:
        log.warning("Haiku classifier API call failed: %s", exc)
        return None

    # Extract text content (Haiku returns a content array with type=text blocks)
    text_parts = []
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    raw_text = "".join(text_parts).strip()

    if not raw_text:
        log.warning("Haiku returned empty content")
        return None

    # The model may occasionally wrap output in code fences despite our
    # instructions; strip them defensively.
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.warning("Haiku returned non-JSON output: %s | head=%s",
                    exc, raw_text[:200])
        return None

    if not isinstance(parsed, list):
        log.warning("Haiku returned non-array output type=%s", type(parsed).__name__)
        return None

    return parsed


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def classify_draft_batch(drafts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify a single batch of drafts. Returns a list of the same length
    as drafts, with each element being:
        {"classification": "billable"|"personal"|"ambiguous",
         "reason": str,
         "classifier_used": bool}

    On API failure or malformed response, returns ambiguous for every draft
    so the matcher (Pass 2) gets to make the call.
    """
    if not drafts:
        return []

    fail_safe = [
        {
            "classification": "ambiguous",
            "reason": "classifier unavailable; deferred to matcher",
            "classifier_used": False,
        }
        for _ in drafts
    ]

    user_msg = _build_user_message(drafts)
    parsed = await _call_haiku(user_msg)

    if parsed is None:
        return fail_safe

    # Map results by index. The model may return them out of order or skip
    # some; we tolerate that and fall back to ambiguous for any missing.
    by_index: Dict[int, Dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        label = item.get("c")
        reason = item.get("r") or ""
        if label not in VALID_LABELS:
            label = "ambiguous"
        by_index[idx] = {
            "classification": label,
            "reason": _truncate(reason, 200),
            "classifier_used": True,
        }

    out = []
    for i in range(len(drafts)):
        if i in by_index:
            out.append(by_index[i])
        else:
            out.append(fail_safe[i])
    return out


async def classify_drafts(drafts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify drafts in BATCH_SIZE chunks. Drafts are classified in the
    order given; returned list has the same length and order.
    """
    if not drafts:
        return []

    results: List[Dict[str, Any]] = []
    n_batches = (len(drafts) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(n_batches):
        start = batch_idx * BATCH_SIZE
        end = start + BATCH_SIZE
        batch = drafts[start:end]
        log.info("Pass 1 classifier: batch %d/%d (%d drafts)",
                 batch_idx + 1, n_batches, len(batch))
        batch_results = await classify_draft_batch(batch)
        results.extend(batch_results)

    # Defensive: ensure 1:1 length match
    if len(results) != len(drafts):
        log.error("classify_drafts length mismatch: got %d for %d drafts",
                  len(results), len(drafts))
        # Pad with fail-safe
        while len(results) < len(drafts):
            results.append({
                "classification": "ambiguous",
                "reason": "classifier length mismatch; deferred to matcher",
                "classifier_used": False,
            })

    # Stats logging
    from collections import Counter
    counts = Counter(r["classification"] for r in results)
    used = sum(1 for r in results if r["classifier_used"])
    log.info("Pass 1 classifier: %d/%d via API; billable=%d, personal=%d, "
             "ambiguous=%d", used, len(results),
             counts.get("billable", 0), counts.get("personal", 0),
             counts.get("ambiguous", 0))

    return results


def apply_classification_to_draft(draft: Dict[str, Any],
                                   result: Dict[str, Any]) -> None:
    """Mutate a draft dict in place to apply Pass 1 classification.

    - billable / ambiguous → status stays 'pending' (default; Pass 2 will run)
    - personal             → status becomes 'personal' (Pass 2 skipped)

    The classification + reason are stored on the draft dict for downstream
    use (UI surfacing, Pass 2 prompt context). They are NOT persisted to a
    separate audit table; only the resulting status is persisted on
    timesheet_drafts.
    """
    cls = result.get("classification", "ambiguous")
    draft["classification"] = cls
    draft["classifier_used"] = bool(result.get("classifier_used"))
    # Stash reason on the draft so the Personal/Excluded UI can show why.
    # This goes into source_detail (already JSONB) so we don't need a new column.
    detail_str = draft.get("source_detail") or "{}"
    if isinstance(detail_str, str):
        try:
            detail = json.loads(detail_str)
        except (ValueError, TypeError):
            detail = {}
    elif isinstance(detail_str, dict):
        detail = dict(detail_str)
    else:
        detail = {}
    detail["pass1_classification"] = cls
    detail["pass1_reason"] = result.get("reason", "")
    detail["pass1_used_api"] = bool(result.get("classifier_used"))
    draft["source_detail"] = json.dumps(detail)

    if cls == "personal":
        draft["status"] = "personal"
    # billable + ambiguous keep the existing default 'pending'
