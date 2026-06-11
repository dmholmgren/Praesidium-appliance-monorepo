"""
ai_reconcile_integration.py
===========================

Component 5 of M9 AI Reconciliation: integration glue between
timesheet_reconcile_job.py and the Pass 1 / Pass 2 engines.

This module provides ONE function: run_ai_passes()

    run_ai_passes(drafts, tenant_id, session_id, user_id,
                  attorney_context, ladder_matter_names)

Called from timesheet_reconcile_job._run_async AFTER drafts are written
to timesheet_drafts but BEFORE the session is marked 'complete'. It:

  1. Pass 1 (Haiku) — classify every draft as billable/personal/ambiguous.
     Updates drafts in-place: adds classification fields to source_detail,
     flips status='personal' for personal drafts.
  2. Persists Pass 1 status mutations back to timesheet_drafts.
  3. Pass 2 (Opus) — for billable+ambiguous drafts, runs the agent loop.
     Persists timesheet_ai_matches audit rows + updates drafts to
     status='ai_review' with matter assignment + reasoning.

WHY A SEPARATE MODULE:
- Keeps the reconcile job's surgical change to ONE import + ONE function
  call. Easy to revert if something breaks.
- Pass 1 and Pass 2 modules already exist (ai_classifier, ai_matcher);
  this is just glue.
- Failure-tolerant: if either pass errors, the rule-ladder drafts that
  were already written remain intact and visible in the existing UI.

DEPENDENCIES:
- jobs.ai_classifier  (Pass 1)
- jobs.ai_matcher     (Pass 2)
- core.db.base        (AsyncSessionLocal)

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from jobs.ai_classifier import classify_drafts, apply_classification_to_draft
from jobs.ai_matcher import run_matcher

log = logging.getLogger("praesidium.jobs.ai_integration")


# ---------------------------------------------------------------------------
# Helper: hydrate drafts from the DB after they were written by the job
# ---------------------------------------------------------------------------
async def _fetch_session_drafts(
    tenant_id: str, session_id: str
) -> List[Dict[str, Any]]:
    """Read back the drafts the reconcile job just wrote, with all
    columns the AI passes need.
    """
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                SELECT id, session_id, tenant_id, user_id, entry_date,
                       matter_id, matter_name, hours, description,
                       source, source_detail, ai_confidence, status
                FROM timesheet_drafts
                WHERE session_id = :sid
                  AND TRIM(tenant_id) = :tid
                ORDER BY entry_date, source
            """),
            {"sid": session_id, "tid": tenant_id.strip()},
        )
        rows = r.mappings().fetchall()

    drafts = []
    for row in rows:
        d = dict(row)
        # Normalize matter_id to string for downstream JSON-friendly handling
        if d.get("matter_id") is not None:
            d["matter_id"] = str(d["matter_id"])
        # source_detail may come back as dict (JSONB) or string; normalize to
        # JSON string since downstream code parses it via json.loads
        if isinstance(d.get("source_detail"), dict):
            d["source_detail"] = json.dumps(d["source_detail"])
        drafts.append(d)
    return drafts


# ---------------------------------------------------------------------------
# Helper: persist Pass 1 mutations
# ---------------------------------------------------------------------------
async def _persist_pass1(drafts: List[Dict[str, Any]]) -> int:
    """After classify_drafts + apply_classification_to_draft mutate each
    draft, write the new status + augmented source_detail back to the DB.

    Returns count of rows updated.
    """
    if not drafts:
        return 0
    n = 0
    async with AsyncSessionLocal() as db:
        for d in drafts:
            try:
                detail_str = d.get("source_detail")
                if isinstance(detail_str, dict):
                    detail_str = json.dumps(detail_str)
                await db.execute(
                    text("""
                        UPDATE timesheet_drafts
                           SET status = :status,
                               source_detail = CAST(:detail AS jsonb)
                         WHERE id = :id
                    """),
                    {
                        "status": d.get("status", "pending"),
                        "detail": detail_str or "{}",
                        "id": str(d["id"]),
                    },
                )
                n += 1
            except Exception as exc:
                log.warning("Pass 1 persist failed for draft %s: %s",
                            d.get("id"), exc)
        await db.commit()
    return n


# ---------------------------------------------------------------------------
# Helper: lookup attorney context for Pass 2 (source_tk_id)
# ---------------------------------------------------------------------------
async def _build_attorney_context(
    tenant_id: str, user_id: int
) -> Dict[str, Any]:
    """Return {user_id, source_tk_id} for the attorney running this session.

    source_tk_id is the legacy Timeslips timekeeper id, used by the matcher
    for get_recent_slips_for_attorney. May be None if the attorney has no
    legacy mapping.
    """
    ctx: Dict[str, Any] = {"user_id": user_id, "source_tk_id": None}
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                text("""
                    SELECT source_tk_id
                    FROM ts_timekeepers
                    WHERE TRIM(tenant_id) = :tid
                      AND mapped_user_id = :uid
                    LIMIT 1
                """),
                {"tid": tenant_id.strip(), "uid": user_id},
            )
            row = r.fetchone()
            if row and row[0]:
                ctx["source_tk_id"] = str(row[0])
    except Exception as exc:
        # ts_timekeepers may not exist or have a different shape on every
        # tenant; we degrade gracefully and let the matcher work without it.
        log.info("Could not resolve source_tk_id for user %s: %s",
                 user_id, exc)
    return ctx


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_ai_passes(
    tenant_id: str,
    session_id: str,
    user_id: int,
    ladder_matter_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run Pass 1 (classifier) then Pass 2 (matcher) on all drafts in the
    given session.

    This is the single integration call from timesheet_reconcile_job.

    Args:
        tenant_id:    Tenant scope (will be TRIMmed before queries)
        session_id:   The reconcile session whose drafts to process
        user_id:      Attorney user_id (for source_tk_id lookup)
        ladder_matter_names: Optional {matter_id_str: matter_name} from the
                             rule ladder (passed through to Pass 2 for
                             matter_name backfill on AI-assigned matter_ids)

    Returns a stats dict (safe to log into timesheet_sessions metadata).

    Always non-fatal: any exception in either pass is caught and reported
    in the returned dict. The reconcile job's rule-ladder drafts are never
    rolled back by failures here.
    """
    if ladder_matter_names is None:
        ladder_matter_names = {}

    stats: Dict[str, Any] = {
        "ai_passes_enabled": False,
        "pass1": {"used": False, "billable": 0, "personal": 0,
                  "ambiguous": 0, "errors": None},
        "pass2": {"used": False, "batches": 0, "drafts_processed": 0,
                  "drafts_updated": 0, "input_tokens_total": 0,
                  "output_tokens_total": 0, "errors": None},
    }

    # Gate: skip the entire AI pipeline if no API key is set. Rule-ladder
    # drafts remain visible in the existing review UI exactly as before.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY not set; skipping AI passes for "
                 "session %s", session_id)
        return stats

    stats["ai_passes_enabled"] = True

    # Hydrate drafts from the DB (the reconcile job just wrote them)
    try:
        drafts = await _fetch_session_drafts(tenant_id, session_id)
    except Exception as exc:
        log.exception("Could not hydrate drafts for session %s", session_id)
        stats["pass1"]["errors"] = f"hydrate failed: {exc}"
        return stats

    if not drafts:
        log.info("No drafts to process for session %s", session_id)
        return stats

    log.info("AI passes: starting on session %s with %d drafts",
             session_id, len(drafts))

    # --------------------------------------------------------------------
    # Pass 1 — classifier
    # --------------------------------------------------------------------
    try:
        results = await classify_drafts(drafts)
        for draft, result in zip(drafts, results):
            apply_classification_to_draft(draft, result)
        # Persist status + source_detail mutations
        await _persist_pass1(drafts)

        # Stats
        stats["pass1"]["used"] = any(r.get("classifier_used") for r in results)
        for r in results:
            cls = r.get("classification", "ambiguous")
            if cls in stats["pass1"]:
                stats["pass1"][cls] += 1
        log.info("Pass 1 done: billable=%d personal=%d ambiguous=%d",
                 stats["pass1"]["billable"], stats["pass1"]["personal"],
                 stats["pass1"]["ambiguous"])
    except Exception as exc:
        log.exception("Pass 1 failed for session %s", session_id)
        stats["pass1"]["errors"] = f"{type(exc).__name__}: {exc}"
        # Continue to Pass 2 anyway — drafts default to status='pending'
        # which Pass 2 will treat as billable+ambiguous

    # --------------------------------------------------------------------
    # Pass 2 — matcher (only on billable + ambiguous; status != 'personal')
    # --------------------------------------------------------------------
    try:
        attorney_context = await _build_attorney_context(tenant_id, user_id)
        log.info("Pass 2 attorney context: %s", attorney_context)

        # Filter out personal drafts before sending to matcher (matcher
        # also has defense-in-depth filter, but keeping the prompt focused
        # is good for cost + accuracy).
        eligible = [d for d in drafts if d.get("status") != "personal"]
        log.info("Pass 2: %d eligible drafts (skipped %d personal)",
                 len(eligible), len(drafts) - len(eligible))

        result = await run_matcher(
            tenant_id=tenant_id,
            session_id=session_id,
            drafts=eligible,
            attorney_context=attorney_context,
            ladder_matter_names=ladder_matter_names,
        )
        stats["pass2"]["used"] = result["batches"] > 0
        stats["pass2"]["batches"] = result["batches"]
        stats["pass2"]["drafts_processed"] = result["drafts_processed"]
        stats["pass2"]["drafts_updated"] = result["drafts_updated"]
        stats["pass2"]["input_tokens_total"] = result["input_tokens_total"]
        stats["pass2"]["output_tokens_total"] = result["output_tokens_total"]
        log.info("Pass 2 done: %d batches, %d drafts updated, "
                 "tokens in=%d out=%d",
                 result["batches"], result["drafts_updated"],
                 result["input_tokens_total"], result["output_tokens_total"])
    except Exception as exc:
        log.exception("Pass 2 failed for session %s", session_id)
        stats["pass2"]["errors"] = f"{type(exc).__name__}: {exc}"

    return stats
