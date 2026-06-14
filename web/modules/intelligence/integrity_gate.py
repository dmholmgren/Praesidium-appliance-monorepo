"""
integrity_gate — adversarial-document defense at the AI inference boundary.

The integrity scanner (integrity_scan.py) DETECTS hidden instructions, invisible
Unicode, and prompt-injection patterns at ingestion and records them in
doc_integrity_flags with char offsets into the §0 canonical string. This module
makes that detection LOAD-BEARING: before any eDiscovery document's text is sent
to an AI model, the flagged spans are neutralized in the PROMPT COPY of the text.

Invariants:
  - The §0 canonical string is NEVER mutated. Sanitization operates only on the
    transient prompt text passed to the model. The hidden text remains verbatim
    in canonical as evidence of bad-faith production.
  - Only eDiscovery documents are gated (document_source='ediscovery_documents').
    DMS and document-less calls (dashboard briefings, drafting) pass through
    untouched and pay zero cost.
  - Decision policy (locked with Dennis, v18.6):
      * Critical flag with a locatable [char_start,char_end) span -> redact that
        span in the prompt copy, then PROCEED.
      * Critical flag that cannot be localized to an offset (no offsets, or
        offsets out of range for this text) -> SKIP the AI pass (fail-safe).
      * Warning/info flags -> never block, never redact (logged context only).
  - Gate ERRORS fail OPEN (a gate bug must not take down every AI call); gate
    DECISIONS fail SAFE (a localized injection is always redacted). The two are
    different: an exception in the gate is not a decision to skip.

The redaction marker is visible-on-purpose so a reviewer reading the prompt/
transcript sees exactly where content was removed.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

GATE_VERSION = "1.0.0"

# Flag types that carry actionable char offsets into the canonical/prompt text.
_OFFSET_FLAG_TYPES = {"instruction_pattern", "hidden_instruction", "unicode_tag_chars"}

# document_source (source table) -> integrity corpus name
_SOURCE_TO_CORPUS = {
    "ediscovery_documents": "ediscovery",
    # dms intentionally absent: DMS is not gated (different, low-volume load).
}

_REDACTION = "[REDACTED: hidden instruction removed by integrity gate]"


@dataclass
class GateDecision:
    gated: bool = False               # did the gate engage at all?
    action: str = "pass"              # pass | sanitized | skip | error
    sanitized_text: Optional[str] = None
    critical_count: int = 0
    redacted_spans: int = 0
    flag_types: list = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def should_skip(self) -> bool:
        return self.action == "skip"


def _corpus_for_source(document_source: Optional[str]) -> Optional[str]:
    if not document_source:
        return None
    return _SOURCE_TO_CORPUS.get(document_source.strip())


async def _fetch_critical_flags(tenant_id: str, corpus: str, doc_id: str):
    """Return critical flags for a doc, with offsets where present."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text("""
                SELECT flag_type, char_start, char_end
                  FROM doc_integrity_flags
                 WHERE TRIM(tenant_id) = :tid
                   AND corpus = :corpus
                   AND doc_id = CAST(:doc AS uuid)
                   AND severity = 'critical'
                 ORDER BY char_start NULLS LAST
            """),
            {"tid": (tenant_id or "").strip(), "corpus": corpus, "doc": str(doc_id)},
        )
        return [dict(r._mapping) for r in rows]


def _apply_redactions(prompt_text: str, flags: list) -> tuple[str, int, bool]:
    """Redact locatable critical spans in the PROMPT COPY.

    Returns (sanitized_text, spans_redacted, all_critical_localized).
    all_critical_localized is False if any critical flag had no usable offset
    within this text -> caller must SKIP.
    """
    spans = []
    all_localized = True
    n = len(prompt_text)
    for f in flags:
        ft = f.get("flag_type")
        cs, ce = f.get("char_start"), f.get("char_end")
        locatable = (
            ft in _OFFSET_FLAG_TYPES
            and cs is not None and ce is not None
            and 0 <= cs < ce <= n
        )
        if locatable:
            spans.append((cs, ce))
        else:
            # A critical flag we cannot pin to a span in THIS text. Render-level
            # criticals (e.g. a hidden_instruction discovered via PDF render with
            # no canonical offset) cannot be surgically removed from the prompt
            # text -> we cannot guarantee the model won't see it -> skip.
            all_localized = False

    if not spans:
        return prompt_text, 0, all_localized

    # Merge overlapping/adjacent spans, then splice with the visible marker.
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))

    out, cursor = [], 0
    for s, e in merged:
        out.append(prompt_text[cursor:s])
        out.append(_REDACTION)
        cursor = e
    out.append(prompt_text[cursor:])
    return "".join(out), len(merged), all_localized


async def evaluate(
    *,
    tenant_id: str,
    document_source: Optional[str],
    document_id: Optional[str],
    prompt_text: str,
) -> GateDecision:
    """Evaluate the gate for one AI call. Pure of side effects on canonical.

    Engages only for eDiscovery documents with a document_id. Everything else
    returns a pass-through decision immediately.
    """
    corpus = _corpus_for_source(document_source)
    if not corpus or not document_id:
        return GateDecision(gated=False, action="pass")

    try:
        flags = await _fetch_critical_flags(tenant_id, corpus, document_id)
    except Exception:
        # Fail OPEN on gate error: do not block legitimate work because the
        # flag store was unreachable. Logged loudly for ops.
        logger.exception("integrity_gate: flag lookup failed for %s/%s; failing open",
                         corpus, document_id)
        return GateDecision(gated=True, action="error", sanitized_text=prompt_text,
                            reason="flag_lookup_failed")

    if not flags:
        return GateDecision(gated=True, action="pass", critical_count=0)

    sanitized, n_spans, all_localized = _apply_redactions(prompt_text or "", flags)
    ftypes = sorted({f["flag_type"] for f in flags})

    if not all_localized:
        logger.warning(
            "integrity_gate: SKIP %s/%s — %d critical flag(s), unlocalizable span; "
            "types=%s", corpus, document_id, len(flags), ftypes)
        return GateDecision(gated=True, action="skip", critical_count=len(flags),
                            redacted_spans=n_spans, flag_types=ftypes,
                            reason="critical_flag_not_localizable")

    logger.warning(
        "integrity_gate: SANITIZED %s/%s — redacted %d span(s) from prompt copy; "
        "types=%s (canonical untouched)", corpus, document_id, n_spans, ftypes)
    return GateDecision(gated=True, action="sanitized", sanitized_text=sanitized,
                        critical_count=len(flags), redacted_spans=n_spans,
                        flag_types=ftypes, reason="sanitized")
