"""
modules/ediscovery/ai_search_service.py
========================================

AI-assisted eDiscovery search — server-side Claude call with MCP access
to Praesidium's data layer.

Flow:
    1. User types natural language query with Ask AI mode
    2. This service calls Claude via Anthropic API with MCP server config
    3. Claude uses MCP tools (search_elasticsearch, query_documents,
       query_ediscovery_documents, run_readonly_query) to find documents
    4. Claude returns structured results + natural language summary
    5. Results are rendered in the existing search UI

Patent Pending - Series 2/3 - D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("praesidium.ediscovery.ai_search")

# MCP server URL — the appliance MCP server, accessible from inside the Docker network
MCP_SERVER_URL = os.environ.get(
    "PRAESIDIUM_MCP_URL",
    "https://mcp.praesidium-legal.com/mcp"
)

AI_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 4096


@dataclass
class AISearchResult:
    """Structured result from AI-assisted search."""
    summary: str = ""
    reasoning: str = ""
    documents: List[Dict[str, Any]] = field(default_factory=list)
    suggested_filters: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_used: str = ""


async def _get_api_key(tenant_id: str) -> str:
    """Resolve Anthropic API key -- env var first (plaintext, known working),
    vault as future fallback when decryption layer is wired."""
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key

    # Vault stores Fernet-encrypted keys -- decryption not yet wired.
    # When the decryption layer is built, uncomment this block.
    # from sqlalchemy import text
    # from core.db.base import AsyncSessionLocal
    # async with AsyncSessionLocal() as session:
    #     row = await session.execute(...)
    #     if rec and rec.encrypted_key:
    #         return decrypt(rec.encrypted_key)

    raise ValueError(f"No Anthropic API key for tenant '{tenant_id}'")


def _build_system_prompt(
    tenant_id: str,
    matter_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Build system prompt with matter context for the AI search."""
    parts = [
        "You are an eDiscovery search assistant for a law firm. "
        "You have access to the firm's document management system and eDiscovery "
        "collections through MCP tools.",
        "",
        "Available tools:",
        "- query_documents: Search DMS documents by filename/content. Params: search, matter_id, tenant, limit",
        "- query_ediscovery_documents: Search eDiscovery documents. Params: search, collection_id, tenant, limit",
        "- search_elasticsearch: Full-text search an ES index. Params: index, query, size",
        "- run_readonly_query: Execute read-only SQL against the database for metadata queries",
        "",
        f"Tenant ID: {tenant_id}",
    ]

    if matter_context:
        parts.append("")
        parts.append("Current matter context:")
        if matter_context.get("matter_name"):
            parts.append(f"  Matter: {matter_context['matter_name']}")
        if matter_context.get("matter_number"):
            parts.append(f"  Number: {matter_context['matter_number']}")
        if matter_context.get("client_name"):
            parts.append(f"  Client: {matter_context['client_name']}")

    parts.extend([
        "",
        "Instructions:",
        "1. Use the MCP tools to search for documents matching the user's query",
        "2. Try multiple search strategies if the first doesn't return good results",
        "3. For DMS searches, use query_documents with the search param",
        "4. For eDiscovery documents, use query_ediscovery_documents",
        "5. For metadata queries (custodians, date ranges, tags), use run_readonly_query with SQL",
        "6. After searching, return your findings as a JSON object",
        "",
        "Response format -- return ONLY a JSON object, no markdown fences:",
        '{',
        '  "summary": "Natural language summary of what you found",',
        '  "reasoning": "Brief explanation of your search strategy",',
        '  "documents": [',
        '    {',
        '      "id": "document UUID",',
        '      "filename": "document filename",',
        '      "relevance_note": "why this document is relevant",',
        '      "score": 0.0 to 1.0',
        '    }',
        '  ],',
        '  "suggested_filters": {',
        '    "custodians": ["list of relevant custodians if identified"],',
        '    "date_range": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},',
        '    "doc_types": ["relevant document types"]',
        '  }',
        '}',
    ])

    return "\n".join(parts)


def _extract_json(raw: str) -> dict:
    """Extract JSON from Claude's response, handling preamble and markdown fences."""
    cleaned = raw.strip()

    # Strip markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        end = -1 if lines[-1].strip() == "```" else len(lines)
        cleaned = "\n".join(lines[1:end])
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Bracket-balanced extraction
    brace_start = cleaned.find("{")
    if brace_start == -1:
        return {"summary": cleaned, "documents": [], "reasoning": ""}

    depth = 0
    end_idx = -1
    for i in range(brace_start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break

    if end_idx > brace_start:
        try:
            return json.loads(cleaned[brace_start:end_idx])
        except json.JSONDecodeError:
            pass

    return {"summary": cleaned, "documents": [], "reasoning": "Parse failed"}


async def ai_search(
    query: str,
    tenant_id: str,
    matter_id: Optional[str] = None,
    matter_context: Optional[Dict[str, Any]] = None,
) -> AISearchResult:
    """
    Execute AI-assisted search using Claude with MCP access to Praesidium data.
    """
    tenant_id = tenant_id.strip()

    try:
        api_key = await _get_api_key(tenant_id)
    except ValueError as e:
        return AISearchResult(error=str(e))

    system_prompt = _build_system_prompt(tenant_id, matter_context)

    user_message = query
    if matter_id:
        user_message += f"\n\n(Scope search to matter_id: {matter_id})"

    request_body = {
        "model": AI_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message}
        ],
        "mcp_servers": [
            {
                "type": "url",
                "url": MCP_SERVER_URL,
                "name": "praesidium"
            }
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "mcp-client-2025-04-04",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=request_body,
                headers=headers,
            )

            if resp.status_code != 200:
                error_text = resp.text[:500]
                logger.error(
                    "Anthropic API error %d for AI search: %s",
                    resp.status_code, error_text,
                )
                return AISearchResult(
                    error=f"API error {resp.status_code}: {error_text}",
                )

            data = resp.json()

    except httpx.TimeoutException:
        logger.error("Anthropic API timeout for AI search")
        return AISearchResult(error="AI search timed out (120s)")
    except Exception as exc:
        logger.error("Anthropic API call failed: %s", exc)
        return AISearchResult(error=f"API call failed: {str(exc)[:200]}")

    # Extract text from response content blocks
    raw_text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            raw_text += block.get("text", "")

    usage = data.get("usage", {})
    model_used = data.get("model", AI_MODEL)

    parsed = _extract_json(raw_text)

    return AISearchResult(
        summary=parsed.get("summary", ""),
        reasoning=parsed.get("reasoning", ""),
        documents=parsed.get("documents", []),
        suggested_filters=parsed.get("suggested_filters", {}),
        prompt_tokens=usage.get("input_tokens", 0),
        completion_tokens=usage.get("output_tokens", 0),
        model_used=model_used,
    )
