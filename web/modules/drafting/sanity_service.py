"""
modules/drafting/sanity_service.py

9-layer document sanity check runner.

Architecture:
    - Loads active layers from sanity_check_config (DB rows, not code)
    - Dispatches each layer to a typed handler function
    - Layers that need a research connector check availability first;
      if ConnectorNotConfigured -> result='skipped', non-blocking
    - Layer 9 (bates_production) skipped if matter has no eDiscovery
      collections (requires_ediscovery_context = TRUE on config row)
    - All results written to sanity_check_log
    - Returns SanityRunResult with per-layer outcomes and overall severity

Layer handlers:
    Layer 1  firm_learning         -- AI: prior docs, judge/counsel profiles
    Layer 2  matter_context        -- AI: party names, cause numbers, dates
    Layer 3  citation_verify       -- research_service: citation lookup
    Layer 4  legal_research        -- research_service: recent developments
    Layer 5  internet_current      -- research_service: web (skipped if unconfigured)
    Layer 6  structure_completeness-- AI: required sections, word count
    Layer 7  style_professionalism -- AI: consistency, contradictions
    Layer 8  transaction_compliance-- AI: practice-area-specific checks
    Layer 9  bates_production      -- bates_service: M5iii production check

Called by:
    drafting_service.py

Never called by:
    Routes, templates, or any HTTP layer
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.drafting.research_service import (
    ConnectorNotConfigured,
    ConnectorUnavailable,
    get_active_connector_for_layer,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LayerFinding:
    severity: str       # 'info' | 'warning' | 'critical'
    message: str
    location: str = ''
    suggestion: str = ''


@dataclass
class LayerResult:
    layer_number: int
    layer_name: str
    result: str         # 'pass' | 'warning' | 'critical' | 'skipped' | 'error'
    findings: list[LayerFinding] = field(default_factory=list)
    skip_reason: str = ''
    log_id: Optional[UUID] = None


@dataclass
class SanityRunResult:
    session_id: UUID
    overall: str        # 'pass' | 'warning' | 'critical' | 'error'
    layers: list[LayerResult] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0

    @property
    def has_blocking_issues(self) -> bool:
        return self.critical_count > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_anthropic_key(tenant_id: str) -> Optional[str]:
    """Resolve tenant Anthropic key. Returns None if unavailable (non-fatal for sanity)."""
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT encrypted_key FROM credentials_vault "
                "WHERE trim(tenant_id) = :tid "
                "  AND provider = 'anthropic' "
                "  AND key_type = 'api_key' "
                "LIMIT 1"
            ),
            {'tid': tenant_id.strip()},
        )
        rec = row.fetchone()
        if rec and rec.encrypted_key:
            return rec.encrypted_key
    return os.environ.get('ANTHROPIC_API_KEY') or None


async def _has_ediscovery_collections(tenant_id: str, matter_id: Optional[UUID]) -> bool:
    """Return True if the matter has any eDiscovery collections."""
    if not matter_id:
        return False
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text(
                "SELECT COUNT(*) as cnt FROM ediscovery_collections "
                "WHERE matter_id = :mid AND trim(tenant_id) = :tid"
            ),
            {'mid': str(matter_id), 'tid': tenant_id.strip()},
        )
        r = row.fetchone()
        return bool(r and r.cnt > 0)


async def _load_active_layers(matter_has_ediscovery: bool) -> list[dict]:
    """Load active sanity check layers from DB, filtering Layer 9 if no eDiscovery."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT layer_number, layer_name, check_type, "
                "       requires_connector_type, requires_ediscovery_context, "
                "       is_active "
                "FROM sanity_check_config "
                "WHERE is_active = TRUE "
                "ORDER BY layer_number"
            ),
        )
        layers = []
        for r in rows.fetchall():
            if r.requires_ediscovery_context and not matter_has_ediscovery:
                continue
            layers.append({
                'layer_number': r.layer_number,
                'layer_name': r.layer_name,
                'check_type': r.check_type,
                'requires_connector_type': r.requires_connector_type,
                'requires_ediscovery_context': r.requires_ediscovery_context,
            })
        return layers


async def _write_layer_result(
    session_id: UUID,
    tenant_id: str,
    layer: dict,
    result: str,
    findings: list[LayerFinding],
    skip_reason: str,
) -> UUID:
    """Write a sanity_check_log row and return its id."""
    log_id = uuid4()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO sanity_check_log "
                "(id, session_id, tenant_id, layer_number, layer_name, "
                " result, findings, skip_reason, run_at) "
                "VALUES (:id, :sid, :tid, :ln, :lname, "
                " :result, CAST(:findings AS jsonb), :skip, NOW())"
            ),
            {
                'id': str(log_id),
                'sid': str(session_id),
                'tid': tenant_id.strip(),
                'ln': layer['layer_number'],
                'lname': layer['layer_name'],
                'result': result,
                'findings': json.dumps([
                    {
                        'severity': f.severity,
                        'message': f.message,
                        'location': f.location,
                        'suggestion': f.suggestion,
                    }
                    for f in findings
                ]),
                'skip': skip_reason or None,
            },
        )
        await session.commit()
    return log_id


def _extract_citations(draft_text: str) -> list[str]:
    """
    Extract case citations from draft text.
    Matches common patterns:
        Smith v. Jones, 123 F.3d 456 (5th Cir. 2020)
        123 U.S. 456
        456 S.W.3d 789
    """
    patterns = [
        # Reporter citations: 123 F.3d 456, 456 U.S. 123, etc.
        r'\d+\s+(?:U\.S\.|F\.\d[a-z]*|S\.W\.\d[a-z]*|S\.E\.\d[a-z]*'
        r'|N\.E\.\d[a-z]*|A\.\d[a-z]*|P\.\d[a-z]*|Cal\.\d[a-z]*'
        r'|Tex\.\d[a-z]*|N\.Y\.\d[a-z]*|F\.Supp\.\d[a-z]*'
        r'|F\.App\'x|L\.Ed\.\d[a-z]*|S\.Ct\.)\s+\d+',
        # v. pattern with reporter
        r'[A-Z][A-Za-z\s]+v\.\s+[A-Z][A-Za-z\s]+,\s+\d+\s+\S+\s+\d+',
    ]
    citations = set()
    for pattern in patterns:
        for match in re.finditer(pattern, draft_text):
            citation = match.group(0).strip()
            if len(citation) > 5:
                citations.add(citation)
    return list(citations)[:20]  # Cap at 20 citations per run


async def _call_ai_layer(
    api_key: str,
    layer_name: str,
    check_type: str,
    draft_text: str,
    matter_context: dict,
    practice_area: str,
) -> tuple[str, list[LayerFinding]]:
    """
    Run an AI-driven sanity check layer.
    Returns (result, findings) where result is 'pass'|'warning'|'critical'.
    """
    prompts = {
        'firm_learning': (
            "Review this legal document draft for consistency with professional "
            "legal drafting standards. Check: (1) Are judge/court references appropriate "
            "for the jurisdiction? (2) Does the document follow standard legal formatting? "
            "(3) Are party references consistent throughout? "
            "Return JSON: {\"result\": \"pass|warning|critical\", "
            "\"findings\": [{\"severity\": \"info|warning|critical\", "
            "\"message\": \"...\", \"location\": \"...\", \"suggestion\": \"...\"}]}"
        ),
        'matter_context': (
            "Review this draft for internal factual consistency. Check: "
            "(1) Are all party names spelled and referenced consistently? "
            "(2) Are cause numbers, court names, and dates internally consistent? "
            "(3) Are exhibit references consistent and do they match what is described? "
            "(4) Are there any contradictory factual assertions? "
            "Return JSON: {\"result\": \"pass|warning|critical\", "
            "\"findings\": [{\"severity\": \"info|warning|critical\", "
            "\"message\": \"...\", \"location\": \"...\", \"suggestion\": \"...\"}]}"
        ),
        'structure_completeness': (
            f"Review this {practice_area} legal document for structural completeness. Check: "
            "(1) Are all required sections present for this document type? "
            "(2) Is a signature block present where required? "
            "(3) Are any required certificates or verifications missing? "
            "(4) Does the document have an appropriate introduction/conclusion? "
            "(5) Are there any [GAP] markers remaining that must be resolved? "
            "Return JSON: {\"result\": \"pass|warning|critical\", "
            "\"findings\": [{\"severity\": \"info|warning|critical\", "
            "\"message\": \"...\", \"location\": \"...\", \"suggestion\": \"...\"}]}"
        ),
        'style_professionalism': (
            "Review this legal document draft for style and professionalism. Check: "
            "(1) Is the tone consistently professional and formal? "
            "(2) Are there ambiguous terms that should be defined? "
            "(3) Are there overstatements or unsupported assertions? "
            "(4) Is the language unnecessarily complex where plain language would serve? "
            "(5) Are there any contradictory arguments or positions? "
            "Return JSON: {\"result\": \"pass|warning|critical\", "
            "\"findings\": [{\"severity\": \"info|warning|critical\", "
            "\"message\": \"...\", \"location\": \"...\", \"suggestion\": \"...\"}]}"
        ),
        'transaction_compliance': (
            f"Review this {practice_area} document for transaction-specific compliance. "
            "Check: (1) Are all required disclosure language elements present? "
            "(2) Are there jurisdiction-specific requirements that appear missing? "
            "(3) For securities documents: are risk factors present and adequate? "
            "(4) For real estate documents: are all required notices included? "
            "(5) Are there any provisions that appear legally unenforceable? "
            "Return JSON: {\"result\": \"pass|warning|critical\", "
            "\"findings\": [{\"severity\": \"info|warning|critical\", "
            "\"message\": \"...\", \"location\": \"...\", \"suggestion\": \"...\"}]}"
        ),
    }

    system_prompt = prompts.get(check_type, prompts['matter_context'])

    # Truncate draft for AI layer checks — 20K is sufficient for structural review
    draft_excerpt = draft_text[:20_000]
    if len(draft_text) > 20_000:
        draft_excerpt += '\n[... document truncated for review ...]'

    content = (
        f"## Document to Review\n\n{draft_excerpt}\n\n"
        f"## Matter Context\n{json.dumps(matter_context)}\n\n"
        f"{system_prompt}\n\n"
        "Return ONLY the JSON object. No preamble, no markdown fences."
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=(
                "You are a precise legal document reviewer. "
                "Return only valid JSON as instructed. "
                "Be specific about locations and actionable in suggestions."
            ),
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip() if response.content else ''
    except Exception as exc:
        logger.error("AI sanity layer %s failed: %s", check_type, exc)
        return 'error', []

    # Parse response
    if raw.startswith('```'):
        lines = raw.split('\n')
        end = -1 if lines[-1].strip() == '```' else len(lines)
        raw = '\n'.join(lines[1:end])
        if raw.startswith('json'):
            raw = raw[4:].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AI layer %s returned non-JSON: %s", check_type, raw[:200])
        return 'error', []

    result = data.get('result', 'pass')
    if result not in ('pass', 'warning', 'critical'):
        result = 'warning'

    findings = []
    for f in data.get('findings', []):
        findings.append(LayerFinding(
            severity=f.get('severity', 'info'),
            message=f.get('message', ''),
            location=f.get('location', ''),
            suggestion=f.get('suggestion', ''),
        ))

    return result, findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_sanity_check(
    session_id: UUID,
    tenant_id: str,
    matter_id: Optional[UUID],
    draft_text: str,
    practice_area: str,
    matter_context: dict,
) -> SanityRunResult:
    """
    Run all active sanity check layers against the draft text.

    Layer execution:
        - AI layers (1,2,6,7,8): run if Anthropic key available
        - Research layers (3,4,5): run if connector configured, skip otherwise
        - Layer 9: only runs if matter has eDiscovery collections

    All results written to sanity_check_log.
    Returns SanityRunResult with per-layer outcomes and aggregate severity.
    """
    tenant_id = tenant_id.strip()

    # Pre-flight checks
    api_key = await _get_anthropic_key(tenant_id)
    matter_has_ediscovery = await _has_ediscovery_collections(tenant_id, matter_id)
    active_layers = await _load_active_layers(matter_has_ediscovery)

    # Update session status
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE drafting_sessions SET status='sanity_running', "
                "updated_at=NOW() WHERE id=:sid AND trim(tenant_id)=:tid"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        await session.commit()

    layer_results: list[LayerResult] = []

    for layer in active_layers:
        ln = layer['layer_number']
        check_type = layer['check_type']
        layer_name = layer['layer_name']
        requires_connector = layer['requires_connector_type']

        # ------------------------------------------------------------------
        # Research connector layers (3, 4, 5)
        # ------------------------------------------------------------------
        if check_type in ('citation_verify', 'legal_research', 'internet_current'):
            try:
                connector = await get_active_connector_for_layer(ln, tenant_id)
            except Exception:
                connector = None

            if connector is None:
                log_id = await _write_layer_result(
                    session_id, tenant_id, layer, 'skipped', [],
                    f"No configured connector for layer {ln} ({check_type})"
                )
                layer_results.append(LayerResult(
                    layer_number=ln, layer_name=layer_name,
                    result='skipped',
                    skip_reason=f"No connector configured",
                    log_id=log_id,
                ))
                continue

            findings: list[LayerFinding] = []
            result = 'pass'

            if check_type == 'citation_verify':
                citations = _extract_citations(draft_text)
                if not citations:
                    result = 'skipped'
                    skip_reason = "No case citations detected in draft"
                else:
                    skip_reason = ''
                    for citation in citations[:10]:  # cap at 10 per run
                        try:
                            cit_result = await connector.check_citation(citation)
                            if cit_result.has_negative_treatment:
                                findings.append(LayerFinding(
                                    severity='critical',
                                    message=f"Negative treatment: {citation}",
                                    location=citation,
                                    suggestion=cit_result.suggested_replacement or
                                               "Review and replace with current authority",
                                ))
                                result = 'critical'
                            elif not cit_result.found:
                                findings.append(LayerFinding(
                                    severity='warning',
                                    message=f"Citation not found: {citation}",
                                    location=citation,
                                    suggestion="Verify citation is correct",
                                ))
                                if result != 'critical':
                                    result = 'warning'
                        except ConnectorUnavailable as exc:
                            logger.warning("Citation check unavailable: %s", exc)
                            result = 'error'
                            break

            elif check_type in ('legal_research', 'internet_current'):
                # Research layers: search for recent developments
                # relevant to the practice area and matter context
                matter_name = matter_context.get('matter_name', '')
                client_name = matter_context.get('client_name', '')
                query = f"{practice_area} {matter_name or client_name}".strip()
                if not query or query == practice_area:
                    result = 'skipped'
                    skip_reason = "Insufficient matter context for research query"
                else:
                    try:
                        research = await connector.search(
                            query=query,
                            max_results=5,
                        )
                        if research.results:
                            findings.append(LayerFinding(
                                severity='info',
                                message=(
                                    f"Found {research.total_found} relevant result(s). "
                                    f"Top result: {research.results[0].get('title', '')}"
                                ),
                                suggestion="Review recent developments for relevance",
                            ))
                    except ConnectorUnavailable as exc:
                        logger.warning("Research layer %d unavailable: %s", ln, exc)
                        result = 'error'

        # ------------------------------------------------------------------
        # Layer 9 — Bates / production consistency (M5iii)
        # ------------------------------------------------------------------
        elif check_type == 'bates_production':
            # Import here to avoid circular import — bates_service imports
            # nothing from sanity_service
            from modules.drafting.bates_service import run_bates_layer
            try:
                result, findings = await run_bates_layer(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    matter_id=matter_id,
                    draft_text=draft_text,
                )
            except Exception as exc:
                logger.error("Bates layer failed: %s", exc)
                result = 'error'
                findings = []

        # ------------------------------------------------------------------
        # AI-driven layers (1, 2, 6, 7, 8)
        # ------------------------------------------------------------------
        else:
            if not api_key:
                log_id = await _write_layer_result(
                    session_id, tenant_id, layer, 'skipped', [],
                    "No Anthropic API key configured"
                )
                layer_results.append(LayerResult(
                    layer_number=ln, layer_name=layer_name,
                    result='skipped',
                    skip_reason="No Anthropic API key",
                    log_id=log_id,
                ))
                continue

            result, findings = await _call_ai_layer(
                api_key=api_key,
                layer_name=layer_name,
                check_type=check_type,
                draft_text=draft_text,
                matter_context=matter_context,
                practice_area=practice_area,
            )

        # Write result to DB
        log_id = await _write_layer_result(
            session_id, tenant_id, layer, result, findings,
            skip_reason if 'skip_reason' in dir() else ''
        )
        layer_results.append(LayerResult(
            layer_number=ln, layer_name=layer_name,
            result=result, findings=findings,
            log_id=log_id,
        ))

    # Compute overall severity
    critical_count = sum(1 for lr in layer_results if lr.result == 'critical')
    warning_count = sum(1 for lr in layer_results if lr.result == 'warning')

    if critical_count > 0:
        overall = 'critical'
    elif warning_count > 0:
        overall = 'warning'
    elif any(lr.result == 'error' for lr in layer_results):
        overall = 'error'
    else:
        overall = 'pass'

    # Update session status
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE drafting_sessions SET status='active', "
                "updated_at=NOW() WHERE id=:sid AND trim(tenant_id)=:tid"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        await session.commit()

    return SanityRunResult(
        session_id=session_id,
        overall=overall,
        layers=layer_results,
        critical_count=critical_count,
        warning_count=warning_count,
    )


async def get_sanity_results(
    session_id: UUID,
    tenant_id: str,
) -> list[dict]:
    """Return all sanity check log rows for a session, ordered by layer."""
    tenant_id = tenant_id.strip()
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT id, layer_number, layer_name, result, "
                "       findings, skip_reason, run_at "
                "FROM sanity_check_log "
                "WHERE session_id=:sid AND trim(tenant_id)=:tid "
                "ORDER BY layer_number"
            ),
            {'sid': str(session_id), 'tid': tenant_id},
        )
        return [
            {
                'id': str(r.id),
                'layer_number': r.layer_number,
                'layer_name': r.layer_name,
                'result': r.result,
                'findings': r.findings or [],
                'skip_reason': r.skip_reason,
                'run_at': r.run_at.isoformat() if r.run_at else None,
            }
            for r in rows.fetchall()
        ]
