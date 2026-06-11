#!/usr/bin/env python3
"""
jobs/email_task_scanner.py

Tier 1: Regex/heuristic pre-scan of email body text for commitment
and deadline signals. Flags candidates for Tier 2 AI extraction.

Runs periodically (e.g., every 30 minutes or after exchange_sync).

Usage:
  python3 /app/jobs/email_task_scanner.py [tenant_id] [--limit N] [--days N] [--dry-run]

Or via cron inside praesidium-web container:
  */30 * * * * cd /app && python3 jobs/email_task_scanner.py >> /tmp/task_scanner.log 2>&1
"""
from __future__ import annotations
import sys, os, re, json, logging, asyncio, argparse
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_URL", os.environ.get("DATABASE_URL", ""))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [task-scanner] %(message)s")
logger = logging.getLogger("task_scanner")

DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# ── Tier 1 Pattern Definitions ──────────────────────────────────────────
COMMITMENT_PATTERNS: List[Tuple[re.Pattern, str, str]] = []

def _p(pattern: str, name: str, strength: str = "medium"):
    COMMITMENT_PATTERNS.append((re.compile(pattern, re.IGNORECASE | re.MULTILINE), name, strength))

# ── Self-commitments ────────────────────────────────────────────────────
_p(r"\bi'?ll\s+(?:do|send|get|have|prepare|draft|review|file|submit|finish|complete|handle|take care of)\s+(?:it|that|this|them)",
   "commitment_self_future", "strong")
_p(r"\bi'?ll\s+(?:do|send|get|have|prepare|draft|review|file|submit|finish|complete)\s+\w+\s+(?:by|before|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|next week|end of (?:day|week|month))",
   "commitment_self_deadline", "strong")
_p(r"\bi\s+will\s+(?:do|send|get|have|prepare|draft|review|file|submit|finish|complete|handle)\b",
   "commitment_i_will", "strong")
_p(r"\blet\s+me\s+(?:get|send|prepare|draft|review|handle|take care of|look into|check on)\b",
   "commitment_let_me", "medium")
_p(r"\bi'?ll\s+(?:circle back|follow up|get back to you|loop back|touch base|reach out)\b",
   "commitment_follow_up", "strong")
_p(r"\bi\s+need\s+to\s+(?:send|file|draft|prepare|review|submit|complete|finish|schedule|call|email)\b",
   "commitment_i_need_to", "medium")

# ── Requests to someone ─────────────────────────────────────────────────
_p(r"\bplease\s+(?:send|prepare|draft|review|file|submit|complete|forward|provide|schedule|confirm|sign|execute|update|revise)\b",
   "request_please", "strong")
_p(r"\bcould\s+you\s+(?:please\s+)?(?:send|prepare|draft|review|file|submit|forward|provide|schedule|confirm|sign)\b",
   "request_could_you", "medium")
_p(r"\bcan\s+you\s+(?:please\s+)?(?:send|prepare|draft|review|file|submit|forward|provide|schedule|confirm|sign)\b",
   "request_can_you", "medium")
_p(r"\bwould\s+you\s+(?:please\s+)?(?:send|prepare|draft|review|file|submit|forward|provide|schedule|confirm|sign)\b",
   "request_would_you", "medium")
_p(r"\bneed\s+you\s+to\s+(?:send|prepare|draft|review|file|submit|complete|forward|schedule|confirm|sign)\b",
   "request_need_you_to", "strong")
_p(r"\bmake\s+sure\s+(?:to|you)\s+(?:send|file|submit|complete|review|sign|forward)\b",
   "request_make_sure", "strong")

# ── Deadline references ──────────────────────────────────────────────────
_p(r"\b(?:due|deadline|due date)\s*(?:is|:)?\s*(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|tomorrow|next\s+(?:week|monday|tuesday|wednesday|thursday|friday)|end\s+of\s+(?:day|week|month|business))\b",
   "deadline_explicit", "strong")
_p(r"\bby\s+(?:close\s+of\s+business|COB|end\s+of\s+(?:day|week|month|business)|EOD|EOB|tomorrow|tonight|next\s+(?:week|monday|tuesday|wednesday|thursday|friday)|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
   "deadline_by", "strong")
_p(r"\bno\s+later\s+than\b", "deadline_no_later_than", "strong")
_p(r"\bASAP\b", "deadline_asap", "medium")
_p(r"\btime.?sensitive\b", "deadline_time_sensitive", "medium")
_p(r"\burgent(?:ly)?\b", "deadline_urgent", "medium")

# ── Scheduling / meeting ─────────────────────────────────────────────────
_p(r"\blet'?s\s+(?:schedule|set up|plan|arrange|book)\s+(?:a\s+)?(?:meeting|call|conference|depo|deposition|hearing|mediation)\b",
   "schedule_lets", "medium")
_p(r"\bschedule\s+(?:a\s+)?(?:meeting|call|conference|depo|deposition|hearing|mediation)\b",
   "schedule_directive", "medium")

# ── Follow-up / reminder ────────────────────────────────────────────────
_p(r"\bremind\s+me\s+to\b", "reminder_remind_me", "strong")
_p(r"\bdon'?t\s+forget\s+to\b", "reminder_dont_forget", "strong")
_p(r"\bfollow\s*up\s+(?:on|with|about|regarding)\b", "follow_up_on", "medium")
_p(r"\bneed\s+(?:a\s+)?response\s+(?:by|before)\b", "need_response_by", "strong")

# ── Legal-specific ───────────────────────────────────────────────────────
_p(r"\b(?:response|answer|reply)\s+(?:is\s+)?due\s+(?:on|by|in)\b", "legal_response_due", "strong")
_p(r"\b(?:discovery|interrogator(?:y|ies)|RFP|request\s+for\s+production|subpoena)\s+(?:response|deadline|due)\b",
   "legal_discovery_deadline", "strong")
_p(r"\bfiling\s+deadline\b", "legal_filing_deadline", "strong")
_p(r"\bstatute\s+of\s+limitations?\b", "legal_sol", "strong")
_p(r"\b(?:hearing|trial|mediation|arbitration|deposition)\s+(?:is\s+)?(?:set|scheduled)\s+(?:for|on)\b",
   "legal_event_scheduled", "strong")


# ── Exclusion patterns ───────────────────────────────────────────────────
EXCLUSION_PATTERNS: List[re.Pattern] = [
    re.compile(r"unsubscribe", re.IGNORECASE),
    re.compile(r"this\s+(?:email|message)\s+(?:is|was)\s+sent\s+(?:by|from|to)\b", re.IGNORECASE),
    re.compile(r"confidentiality\s+notice", re.IGNORECASE),
    re.compile(r"do\s+not\s+reply\s+to\s+this", re.IGNORECASE),
    re.compile(r"automated\s+(?:message|notification|email)", re.IGNORECASE),
    re.compile(r"newsletter", re.IGNORECASE),
    re.compile(r"marketing\s+(?:email|communication)", re.IGNORECASE),
]


def is_likely_automated(text: str) -> bool:
    hits = sum(1 for p in EXCLUSION_PATTERNS if p.search(text))
    return hits >= 2


def scan_email_body(body: str) -> List[Dict]:
    if not body or len(body.strip()) < 20:
        return []
    if is_likely_automated(body):
        return []

    signals = []
    seen_patterns = set()

    for regex, name, strength in COMMITMENT_PATTERNS:
        for match in regex.finditer(body):
            if name in seen_patterns:
                continue
            seen_patterns.add(name)
            start = max(0, match.start() - 60)
            end = min(len(body), match.end() + 60)
            context = body[start:end].replace("\n", " ").strip()
            signals.append({
                "phrase": match.group().strip(),
                "pattern": name,
                "strength": strength,
                "offset": match.start(),
                "context": context,
            })
    return signals


def compute_signal_score(signals: List[Dict]) -> float:
    if not signals:
        return 0.0
    weights = {"strong": 0.4, "medium": 0.2, "weak": 0.1}
    total = sum(weights.get(s["strength"], 0.1) for s in signals)
    return min(1.0, total)


async def get_unscanned_emails(tenant_id: str, limit: int = 500, days: int = 60) -> List[Dict]:
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id::text, subject, body_text, body_preview,
                       from_email, from_display, received_at,
                       matched_matter_id::text, routing_status
                FROM email_routing_queue
                WHERE TRIM(tenant_id) = :tid
                  AND task_extraction_status IS NULL
                  AND (body_text IS NOT NULL OR body_preview IS NOT NULL)
                  AND received_at >= NOW() - CAST(:days || ' days' AS INTERVAL)
                ORDER BY received_at DESC
                LIMIT :lim
            """),
            {"tid": tenant_id.strip(), "days": str(days), "lim": limit}
        )
        return [dict(r) for r in result.mappings().all()]


async def update_scan_result(tenant_id: str, email_id: str, status: str,
                             signals: Optional[List[Dict]] = None, score: float = 0.0):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text
    result_json = None
    if signals:
        result_json = json.dumps({
            "tier1_signals": signals,
            "signal_score": round(score, 3),
            "scanned_at": datetime.utcnow().isoformat() + "Z",
        })
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE email_routing_queue
                SET task_extraction_status = :status,
                    task_extraction_result = CAST(:result AS jsonb),
                    task_extraction_at = NOW()
                WHERE id = CAST(:eid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"tid": tenant_id.strip(), "eid": email_id, "status": status, "result": result_json}
        )
        await session.commit()


async def run_scan(tenant_id: str, limit: int = 500, days: int = 60, dry_run: bool = False):
    logger.info("Starting Tier 1 scan: tenant=%s, limit=%d, days=%d, dry_run=%s",
                tenant_id, limit, days, dry_run)
    emails = await get_unscanned_emails(tenant_id, limit=limit, days=days)
    logger.info("Found %d unscanned emails", len(emails))
    if not emails:
        return {"scanned": 0, "flagged": 0, "clean": 0}

    flagged = 0
    clean = 0
    for email in emails:
        email_id = email["id"]
        body = email.get("body_text") or email.get("body_preview") or ""
        signals = scan_email_body(body)
        if signals:
            score = compute_signal_score(signals)
            if dry_run:
                logger.info("  [DRY-RUN] FLAGGED: %s | subject='%s' | signals=%d | score=%.2f | top=%s",
                            email_id, (email.get("subject") or "")[:60], len(signals), score, signals[0]["pattern"])
                for s in signals:
                    logger.info("    -> [%s] %s: '%s'", s["strength"], s["pattern"], s["phrase"])
            else:
                await update_scan_result(tenant_id, email_id, "flagged", signals, score)
            flagged += 1
        else:
            if not dry_run:
                await update_scan_result(tenant_id, email_id, "scanned")
            clean += 1

    results = {"scanned": len(emails), "flagged": flagged, "clean": clean}
    logger.info("Scan complete: %s", results)
    return results


def main():
    parser = argparse.ArgumentParser(description="Email Task Scanner (Tier 1)")
    parser.add_argument("tenant_id", nargs="?", default=DEFAULT_TENANT)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_scan(args.tenant_id, limit=args.limit, days=args.days, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
