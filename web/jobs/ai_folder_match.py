#!/usr/bin/env python3
"""
jobs/ai_folder_match.py
AI-assisted folder matching — resolves the unmatched folders that the
basic string-matching pass couldn't handle.

Tier 1 (SQL): Match against ts_clients.ts_name (richer naming variants)
  - Catches: abbreviations→full names (ASLIC→American Savings), typos,
    DBA names, and company name variants stored in Timeslips
Tier 2 (Claude): For remaining unmatched, sends batches to Claude with
  the full client list for semantic matching
  - Catches: co-counsel prefixes (SN-), personal names, case-name folders,
    completely different naming conventions

Usage:
    sudo docker exec praesidium-web python3 /app/jobs/ai_folder_match.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f

    # Tier 1 only (no AI calls):
    --tier1-only

    # Dry run:
    --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("ai_folder_match")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def tier1_ts_name_matching(conn, tid: str, dry_run: bool) -> int:
    """
    Tier 1: Match unmatched root folders against ts_clients.ts_name.
    The ts_clients table has the full company names that Timeslips stores,
    which are often very different from the canonical client_name abbreviation.
    """
    log.info("=== Tier 1: ts_clients.ts_name matching ===")
    matched = 0

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get unmatched folders
        cur.execute("""
            SELECT DISTINCT root_folder FROM file_inventory
            WHERE tenant_id = %s AND match_method IS NULL
              AND root_folder IS NOT NULL
            ORDER BY root_folder
        """, (tid,))
        unmatched = [r["root_folder"] for r in cur.fetchall()]
        log.info("  %d unmatched folders to process", len(unmatched))

        # Load ts_clients name variants → canonical client mapping
        cur.execute("""
            SELECT DISTINCT tc.ts_name, tc.client_code,
                   tc.praesidium_client_id, c.client_name
            FROM ts_clients tc
            LEFT JOIN clients c ON c.id::text = tc.praesidium_client_id
            WHERE TRIM(tc.tenant_id) = %s
              AND tc.ts_name IS NOT NULL
              AND tc.praesidium_client_id IS NOT NULL
        """, (tid,))
        ts_names = cur.fetchall()

        # Build lookup: lowercase ts_name → client info
        ts_lookup = {}
        for r in ts_names:
            key = (r["ts_name"] or "").strip().lower()
            if key and key not in ts_lookup:
                ts_lookup[key] = r

        log.info("  %d distinct ts_name variants loaded", len(ts_lookup))

        for folder in unmatched:
            folder_lower = folder.strip().lower()
            best = None
            method = None
            confidence = 0.0

            # Strategy 1: Exact ts_name match
            if folder_lower in ts_lookup:
                best = ts_lookup[folder_lower]
                method = "ts_name_exact"
                confidence = 0.95

            # Strategy 2: ts_name contains folder name
            if not best:
                for ts_name_lower, info in ts_lookup.items():
                    if len(folder_lower) >= 4 and folder_lower in ts_name_lower:
                        best = info
                        method = "ts_name_contains_folder"
                        confidence = 0.75
                        break

            # Strategy 3: Folder name contains ts_name
            if not best:
                for ts_name_lower, info in ts_lookup.items():
                    if len(ts_name_lower) >= 4 and ts_name_lower in folder_lower:
                        best = info
                        method = "folder_contains_ts_name"
                        confidence = 0.75
                        break

            # Strategy 4: client_code in folder name
            if not best:
                for ts_name_lower, info in ts_lookup.items():
                    code = (info.get("client_code") or "").strip().lower()
                    if code and len(code) >= 3 and code in folder_lower:
                        best = info
                        method = "client_code_in_folder"
                        confidence = 0.7
                        break

            # Strategy 5: Folder name starts with a word from ts_name
            if not best:
                folder_words = set(folder_lower.replace("-", " ").replace("_", " ").split())
                for ts_name_lower, info in ts_lookup.items():
                    ts_words = set(ts_name_lower.replace("-", " ").replace("_", " ").split())
                    # At least 2 words overlap, or 1 word that's 5+ chars
                    overlap = folder_words & ts_words
                    long_overlap = [w for w in overlap if len(w) >= 5]
                    if len(overlap) >= 2 or len(long_overlap) >= 1:
                        best = info
                        method = "word_overlap_ts_name"
                        confidence = 0.65
                        break

            if best:
                if dry_run:
                    log.info("  [T1-DRY] %s → %s (%s, conf=%.2f, method=%s)",
                             folder, best.get("client_name") or best.get("ts_name"),
                             best.get("client_code"), confidence, method)
                else:
                    cur.execute("""
                        UPDATE file_inventory
                        SET proposed_client_id = %s::uuid,
                            proposed_client_name = %s,
                            match_method = %s,
                            match_confidence = %s,
                            match_status = 'pending'
                        WHERE tenant_id = %s AND root_folder = %s
                          AND match_method IS NULL
                    """, (
                        best["praesidium_client_id"],
                        best.get("client_name") or best.get("ts_name"),
                        method, confidence,
                        tid, folder,
                    ))
                matched += 1

    if not dry_run:
        conn.commit()

    log.info("  Tier 1 matched: %d folders", matched)
    return matched


def tier2_ai_matching(conn, tid: str, dry_run: bool) -> int:
    """
    Tier 2: Use Claude to match remaining unmatched folders.
    Sends the folder names + full client list to Claude for semantic matching.
    """
    log.info("=== Tier 2: Claude AI matching ===")

    # Check for API key
    api_key = None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT encrypted_key FROM credentials_vault
            WHERE TRIM(tenant_id) = %s AND provider = 'anthropic'
              AND key_type = 'api_key'
        """, (tid,))
        row = cur.fetchone()
        if row:
            val = row["encrypted_key"]
            if val and val.startswith("gAAAAA"):
                try:
                    from cryptography.fernet import Fernet
                    import base64
                    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
                    key_bytes = (secret[:32]).encode().ljust(32, b"0")
                    fernet_key = base64.urlsafe_b64encode(key_bytes)
                    f = Fernet(fernet_key)
                    api_key = f.decrypt(val.encode()).decode()
                except Exception as e:
                    log.warning("Failed to decrypt Anthropic key: %s", e)
            else:
                api_key = val

    if not api_key:
        # Try env var
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        log.warning("  No Anthropic API key available — skipping Tier 2")
        return 0

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get remaining unmatched
        cur.execute("""
            SELECT DISTINCT root_folder FROM file_inventory
            WHERE tenant_id = %s AND match_method IS NULL
              AND root_folder IS NOT NULL
            ORDER BY root_folder
        """, (tid,))
        unmatched = [r["root_folder"] for r in cur.fetchall()]

        if not unmatched:
            log.info("  No unmatched folders remaining")
            return 0

        # Get client list for context
        cur.execute("""
            SELECT c.id, c.client_name, c.client_number,
                   (SELECT string_agg(DISTINCT tc.ts_name, '; ')
                    FROM ts_clients tc
                    WHERE tc.praesidium_client_id = c.id::text
                      AND TRIM(tc.tenant_id) = %s
                      AND tc.ts_name IS NOT NULL
                    LIMIT 3) AS ts_aliases
            FROM clients c
            WHERE TRIM(c.tenant_id) = %s
            ORDER BY c.client_name
        """, (tid, tid))
        clients = cur.fetchall()

    # Build client context for Claude
    client_lines = []
    for c in clients:
        line = f"- ID:{c['id']} | {c['client_name']}"
        if c.get("client_number"):
            line += f" | #{c['client_number']}"
        if c.get("ts_aliases"):
            line += f" | AKA: {c['ts_aliases'][:100]}"
        client_lines.append(line)

    client_context = "\n".join(client_lines)

    # Build folder list
    folder_list = "\n".join(f"- {f}" for f in unmatched)

    prompt = f"""You are matching legacy file server folder names to law firm clients.

Here are the canonical clients with their IDs and known aliases:
{client_context}

Here are the unmatched folder names from the file server:
{folder_list}

For each folder, determine if it matches a client. Consider:
- Abbreviations (ASLIC = American Savings Life Insurance Company)
- DBA names and trade names
- "SN -" prefix likely means Shamoun & Norman (co-counsel) matters
- Personal names may be parties in litigation, not clients
- Administrative folders (Legal Research, Status Reports, etc.) should be marked as "admin" not matched to a client
- Some folders are for potential/prospective clients that may not be in the system

Respond with ONLY a JSON array. For each folder, output:
{{"folder": "folder name", "client_id": "uuid or null", "client_name": "name or null", "confidence": 0.0-1.0, "reason": "brief explanation"}}

If a folder doesn't match any client, set client_id to null.
If it's an administrative folder, set reason to "administrative".
"""

    log.info("  Sending %d folders to Claude for matching...", len(unmatched))

    try:
        import httpx
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )

        if resp.status_code != 200:
            log.error("  Claude API error: %d %s", resp.status_code, resp.text[:200])
            return 0

        data = resp.json()
        text = data["content"][0]["text"]

        # Parse JSON from response
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if not json_match:
            log.error("  Could not parse JSON from Claude response")
            return 0

        matches = json.loads(json_match.group())
        matched = 0

        with conn.cursor() as cur:
            for m in matches:
                folder = m.get("folder")
                client_id = m.get("client_id")
                confidence = m.get("confidence", 0)

                if not folder or not client_id or confidence < 0.5:
                    continue

                if dry_run:
                    log.info("  [T2-DRY] %s → %s (conf=%.2f, %s)",
                             folder, m.get("client_name"), confidence,
                             m.get("reason", ""))
                else:
                    cur.execute("""
                        UPDATE file_inventory
                        SET proposed_client_id = %s::uuid,
                            proposed_client_name = %s,
                            match_method = %s,
                            match_confidence = %s,
                            match_status = 'pending'
                        WHERE tenant_id = %s AND root_folder = %s
                          AND match_method IS NULL
                    """, (
                        client_id,
                        m.get("client_name"),
                        "ai_claude_semantic",
                        confidence,
                        tid, folder,
                    ))
                matched += 1

        if not dry_run:
            conn.commit()

        log.info("  Tier 2 matched: %d folders", matched)
        return matched

    except Exception as e:
        log.error("  Tier 2 failed: %s", e)
        return 0


def main():
    parser = argparse.ArgumentParser(description="AI folder matching")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--tier1-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tid = args.tenant.strip()
    conn = psycopg2.connect(get_db_url())

    try:
        t1 = tier1_ts_name_matching(conn, tid, args.dry_run)
        t2 = 0
        if not args.tier1_only:
            t2 = tier2_ai_matching(conn, tid, args.dry_run)

        log.info("=== Final Summary ===")
        log.info("  Tier 1 (ts_name):  %d matched", t1)
        log.info("  Tier 2 (Claude):   %d matched", t2)
        log.info("  Total:             %d matched", t1 + t2)

        # Show remaining unmatched
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT root_folder FROM file_inventory
                WHERE tenant_id = %s AND match_method IS NULL
                  AND root_folder IS NOT NULL
                ORDER BY root_folder
            """, (tid,))
            still_unmatched = [r["root_folder"] for r in cur.fetchall()]
            if still_unmatched:
                log.info("  Still unmatched (%d):", len(still_unmatched))
                for f in still_unmatched:
                    log.info("    - %s", f)
    finally:
        conn.close()

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
