#!/usr/bin/env python3
"""jobs/build_case_seed.py -- Job A v4 (PRINT-ONLY): resolve-not-transcribe case taxonomy.
v4: model never types a machine value. SALI menu numbered -> model returns menu_number ->
code resolves iri/sali_code/label. Allegations return topic_numbers + section_index +
allegation_text (NO char offsets); code resolves sali_iris and DERIVES char_start/char_end
by locating allegation_text in the section content (offset by section.char_start; section-
span fallback). v3 batching/caching/truncation preserved. Consolidated JSON shape unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from core.db.base import AsyncSessionLocal  # noqa: E402
from modules.intelligence import anthropic_adapter as ai  # noqa: E402
from modules.intelligence.anthropic_adapter import (  # noqa: E402
    AICallContext, strip_markdown_fences,
)

DEFAULT_TENANT = os.environ.get("TENANT_ID", "")
RAW_DIR = "/tmp"

SYSTEM_TOPICS = """You are a senior litigation analyst building the CASE TAXONOMY \
(issue spine) for a matter, grounded in a fixed, NUMBERED SALI area-of-law menu.

You are given (1) a NUMBERED SALI menu (each line: "<N>. [CODE] label: definition") and \
(2) the matter's operative pleadings and disclosures -- BOTH SIDES -- verbatim, with \
source markers [DOC <doc_id> | S<idx> @<start>-<end>].

Identify every area of law actually in dispute across ALL provided documents -- \
plaintiff claims, defenses, counterclaims, third-party claims, and jurisdictional \
theories alike. Output STRICT JSON only:
{
  "matter_summary": "<=3 neutral sentences naming the dispute and both sides' posture",
  "case_topics": [
    {"menu_number": <integer N from the numbered menu>,
     "topic_label": "<the menu label for that number, for sanity-check>",
     "rationale": "<why in dispute, citing which side>",
     "confidence": 0.0-1.0}
  ]
}
Rules: menu_number MUST be an integer that appears in the numbered menu above. NEVER \
write an IRI or invent a number. Include a topic if EITHER side puts it in play. Select \
what is genuinely in dispute -- typically 8-15, not the whole menu."""

SYSTEM_ALLEGATIONS = """You are a litigation analyst extracting MATTER-SPECIFIC \
allegations from a slice of ONE document, grounding each to a fixed, NUMBERED set of \
case topics.

You are given (1) the matter's CASE TOPICS as a NUMBERED list ("<N>. [CODE] label") and \
(2) a section range of ONE document, verbatim, with markers \
[DOC <doc_id> | S<idx> @<start>-<end>].

Extract every discrete, MATTER-SPECIFIC allegation, contention, claim, or defense the \
text makes. Be exhaustive on substance -- if a section states three distinct \
allegations, emit three. Quote allegation_text as CLOSELY to the source wording as \
possible (verbatim where you can) so it can be located in the section.

DO NOT extract, and emit nothing for:
  - generic recitations of legal standards (due-process / minimum-contacts / \
purposeful-availment / long-arm boilerplate, elements-of-a-cause recitations divorced \
from this matter's facts);
  - stock procedural or disclosure responses (Rule 194/192 "none known at this time", \
"the parties are named correctly", boilerplate attorney-fee requests, \
medical-records-not-applicable);
  - pure citation strings or case-law quotations.
If a section contains ONLY such boilerplate, return no allegations for it.

Output STRICT JSON only:
{
  "allegations": [
    {"allegation_text": "<one discrete, matter-specific allegation, quoted as closely \
to the source as possible>",
     "topic_numbers": [<integer N(s) from the numbered CASE-TOPICS list>],
     "section_index": <integer S<idx> of the section this came from, from its marker>,
     "allegation_type": "factual|legal|defense|counterclaim",
     "party_side": "plaintiff|defendant|third-party|counter-plaintiff|counter-defendant|neutral",
     "confidence": 0.0-1.0}
  ]
}
Rules: topic_numbers are integers from the numbered CASE-TOPICS list only; use [] if none \
fits. section_index MUST be the S<idx> of a real [DOC ...] marker in the slice. NEVER \
output character offsets -- only the section_index. party_side reflects who asserts it \
(infer from the document's role)."""


async def fetch_sali_menu(session):
    rows = (await session.execute(text("""
        SELECT iri, sali_code, pref_label, definition
        FROM sali_concepts WHERE branch='area_of_law' AND is_active
        ORDER BY sali_code NULLS LAST, pref_label
    """))).mappings().all()
    return [dict(r) for r in rows]


async def fetch_doc(session, tenant, doc_id):
    meta = (await session.execute(text("""
        SELECT id::text AS id, regexp_replace(file_path,'^.*/','') AS filename
        FROM dms_documents WHERE TRIM(tenant_id)=:t AND id=CAST(:d AS uuid)
    """), {"t": tenant, "d": doc_id})).mappings().first()
    if not meta:
        return None
    secs = (await session.execute(text("""
        SELECT section_index, char_start, char_end, content
        FROM document_sections
        WHERE TRIM(tenant_id)=:t AND dms_document_id=CAST(:d AS uuid)
          AND superseded_by_run_id IS NULL
        ORDER BY section_index
    """), {"t": tenant, "d": doc_id})).mappings().all()
    return {"id": meta["id"], "filename": meta["filename"],
            "sections": [dict(s) for s in secs]}


def render_menu(menu):
    out = []
    for i, m in enumerate(menu):
        code = m["sali_code"] or "----"
        defn = (m["definition"] or "").replace("\n", " ").strip()
        out.append(f"{i}. [{code}] {m['pref_label']}: {defn}")
    return "\n".join(out)


def render_sections(doc_id, filename, sections):
    out = [f"\n===== DOC doc_id={doc_id} file={filename} ====="]
    for s in sections:
        marker = f"[DOC {doc_id} | S{s['section_index']} @{s['char_start']}-{s['char_end']}]"
        out.append(f"{marker}\n{(s['content'] or '').strip()}")
    return "\n".join(out)


def render_whole_doc(d, budget=None):
    total = 0
    keep = []
    for s in d["sections"]:
        total += len(s["content"] or "")
        keep.append(s)
        if budget and total > budget:
            break
    return render_sections(d["id"], d["filename"], keep), total


def render_topics_closed_set(topics):
    return "\n".join(
        f"{i}. [{t.get('sali_code') or '----'}] {t.get('topic_label')}"
        for i, t in enumerate(topics))


def resolve_topics(topics_raw, menu):
    topics, bad = [], []
    n = len(menu)
    for t in topics_raw:
        num = t.get("menu_number")
        if not isinstance(num, int) or num < 0 or num >= n:
            bad.append(t)
            continue
        m = menu[num]
        topics.append({
            "menu_number": num,
            "sali_iri": m["iri"],
            "sali_code": m["sali_code"],
            "topic_label": m["pref_label"],
            "rationale": t.get("rationale"),
            "confidence": t.get("confidence"),
        })
    return topics, bad


def _norm_map(s):
    """Lowercase + collapse every run of non-alphanumeric to one space. Returns
    (norm, idx_map) where idx_map[i] is the raw index in s of normalized char i."""
    norm = []
    idx_map = []
    prev_space = True
    for i, ch in enumerate(s):
        if ch.isalnum():
            norm.append(ch.lower())
            idx_map.append(i)
            prev_space = False
        elif not prev_space:
            norm.append(" ")
            idx_map.append(i)
            prev_space = True
    return "".join(norm), idx_map


def locate_span(section_content, sec_start, sec_end, alleg_text):
    """Derive doc-level [char_start, char_end] for an allegation by locating its text in
    the section, tolerant of case/punctuation/whitespace and of a dropped leading word.
    Offsets index the canonical text. Falls back to the section span. Always in-bounds."""
    content = section_content or ""
    needle = (alleg_text or "").strip()
    if not content or not needle:
        return sec_start, sec_end
    idx = content.find(needle)
    if idx >= 0:
        start = sec_start + idx
        return start, min(start + len(needle), sec_end)
    cnorm, cmap = _norm_map(content)
    nnorm, _ = _norm_map(needle)
    if not nnorm:
        return sec_start, sec_end
    pos = cnorm.find(nnorm)
    if pos >= 0:
        raw_start = sec_start + cmap[pos]
        raw_end = sec_start + cmap[pos + len(nnorm) - 1] + 1
        return raw_start, min(max(raw_end, raw_start + 1), sec_end)
    toks = nnorm.split(" ")
    if len(toks) >= 3:
        anchor = " ".join(toks[:8])
        pos = cnorm.find(anchor)
        if pos >= 0:
            raw_start = sec_start + cmap[pos]
            return raw_start, min(raw_start + len(needle), sec_end)
    return sec_start, sec_end


def resolve_allegations(batch, d, topics):
    sec_by_idx = {s["section_index"]: s for s in d["sections"]}
    n_topics = len(topics)
    out = []
    for a in batch:
        nums = [x for x in (a.get("topic_numbers") or [])
                if isinstance(x, int) and 0 <= x < n_topics]
        iris = [topics[x]["sali_iri"] for x in nums]
        sidx = a.get("section_index")
        sec = sec_by_idx.get(sidx)
        if sec is not None:
            cs, ce = locate_span(sec.get("content"), sec["char_start"],
                                  sec["char_end"], a.get("allegation_text"))
        else:
            cs = ce = None
        a["sali_iris"] = iris
        a["status"] = "grounded" if iris else "proposed"
        a["source"] = {"doc_id": d["id"], "section_index": sidx,
                       "char_start": cs, "char_end": ce}
        a["_doc_id"] = d["id"]
        a["_filename"] = d["filename"]
        out.append(a)
    return out


async def run_topics(tenant, menu, topic_docs, args):
    menu_block = render_menu(menu)
    parts, total, used = [], 0, []
    for d in topic_docs:
        block, _ = render_whole_doc(d, budget=max(0, args.topic_budget_chars - total))
        parts.append(block); total += len(block); used.append(d)
        if total >= args.topic_budget_chars:
            break
    user = (f"SALI AREA-OF-LAW MENU ({len(menu)} concepts, numbered):\n{menu_block}\n\n"
            f"OPERATIVE DOCUMENTS ({len(used)} of {len(topic_docs)}, {total} chars):\n"
            f"{''.join(parts)}\n\nReturn the case taxonomy as STRICT JSON now.")
    print(f"\n[PASS 1 topics] {len(used)} docs, ~{(len(SYSTEM_TOPICS)+len(user))//4} input tokens")
    if args.dry_run:
        print("  DRY RUN -- topic pass not called."); return None
    ctx = AICallContext(tenant_id=tenant, module="intelligence",
                        purpose=args.topic_purpose, matter_id=args.matter_id)
    result = await ai.call(ctx, raw_system_prompt=SYSTEM_TOPICS, raw_user_prompt=user,
                           max_tokens_override=args.topic_max_tokens,
                           http_timeout_override=args.http_timeout)
    print(f"  model={result.model_used} tokens={result.total_tokens} "
          f"cost=${result.cost_usd:.4f} out={result.output_tokens}")
    with open(f"{RAW_DIR}/case_seed_topics.json", "w") as f:
        f.write(result.text)
    if result.output_tokens >= args.topic_max_tokens:
        print("  WARNING: topic output hit the cap -- raise --topic-max-tokens.")
    try:
        return json.loads(strip_markdown_fences(result.text))
    except json.JSONDecodeError as e:
        print(f"  ERROR parsing topics ({e}); raw -> {RAW_DIR}/case_seed_topics.json"); sys.exit(1)


async def run_allegations(tenant, topics, docs, args):
    topic_block = render_topics_closed_set(topics)
    all_allegs, total_cost, truncations = [], 0.0, []
    for i, d in enumerate(docs, 1):
        secs = d["sections"]
        batches = [secs[j:j + args.alleg_batch_sections]
                   for j in range(0, len(secs), args.alleg_batch_sections)] or [[]]
        doc_count = 0
        print(f"[PASS 2 alleg {i}/{len(docs)}] {d['filename'][:55]} "
              f"({len(secs)} sec -> {len(batches)} batch)")
        for bi, batch_secs in enumerate(batches):
            if not batch_secs:
                continue
            doc_block = render_sections(d["id"], d["filename"], batch_secs)
            user = (f"CASE TOPICS (numbered, closed grounding set):\n{topic_block}\n\n"
                    f"DOCUMENT SLICE (sections {batch_secs[0]['section_index']}-"
                    f"{batch_secs[-1]['section_index']}):\n{doc_block}\n\n"
                    f"Extract matter-specific allegations as STRICT JSON now.")
            if args.dry_run:
                print(f"    batch {bi} S{batch_secs[0]['section_index']}-"
                      f"{batch_secs[-1]['section_index']}: ~{(len(SYSTEM_ALLEGATIONS)+len(user))//4} in")
                continue
            raw_path = f"{RAW_DIR}/case_seed_alleg_{d['id'][:8]}_b{bi}.json"
            if os.path.exists(raw_path):
                try:
                    cached = json.loads(strip_markdown_fences(open(raw_path).read()))
                    cba = resolve_allegations(cached.get("allegations", []), d, topics)
                    all_allegs.extend(cba); doc_count += len(cba)
                    print(f"    batch {bi}: reused {len(cba)} from cache")
                    continue
                except Exception:
                    pass
            ctx = AICallContext(tenant_id=tenant, module="intelligence",
                                purpose=args.alleg_purpose, matter_id=args.matter_id)
            try:
                result = await ai.call(ctx, raw_system_prompt=SYSTEM_ALLEGATIONS,
                                       raw_user_prompt=user,
                                       max_tokens_override=args.alleg_max_tokens,
                                       http_timeout_override=args.http_timeout)
            except Exception as e:
                print(f"    batch {bi}: call failed: {e}"); truncations.append((d['filename'], bi, 'call_failed')); continue
            total_cost += float(result.cost_usd)
            with open(raw_path, "w") as f:
                f.write(result.text)
            if result.output_tokens >= args.alleg_max_tokens:
                print(f"    batch {bi}: !! OUTPUT HIT CAP ({result.output_tokens}) -- "
                      f"lower --alleg-batch-sections; this batch may be incomplete")
                truncations.append((d['filename'], bi, 'truncated'))
            try:
                parsed = json.loads(strip_markdown_fences(result.text))
            except json.JSONDecodeError as e:
                print(f"    batch {bi}: ERROR parsing ({e}); raw saved")
                truncations.append((d['filename'], bi, 'parse_error')); continue
            ba = resolve_allegations(parsed.get("allegations", []), d, topics)
            all_allegs.extend(ba); doc_count += len(ba)
        if not args.dry_run:
            print(f"    -> {doc_count} allegations from {d['filename'][:45]}")
    return all_allegs, total_cost, truncations


async def main_async(args):
    tenant = (args.tenant or DEFAULT_TENANT).strip()
    if not tenant:
        print("ERROR: no tenant"); sys.exit(1)
    alleg_ids = list(args.doc_id or [])
    topic_ids = list(args.topic_doc_id or []) or alleg_ids
    if not alleg_ids:
        print("ERROR: pass --doc-id (repeatable)"); sys.exit(1)

    async with AsyncSessionLocal() as session:
        menu = await fetch_sali_menu(session)
        if not menu:
            print("ERROR: sali_concepts area_of_law empty"); sys.exit(1)
        topic_docs = [d for d in [await fetch_doc(session, tenant, x) for x in topic_ids] if d]
        alleg_docs = [d for d in [await fetch_doc(session, tenant, x) for x in alleg_ids] if d]
    if not alleg_docs:
        print("ERROR: no allegation docs with sections"); sys.exit(1)

    if args.topics_from:
        with open(args.topics_from) as f:
            topics_obj = json.load(f)
        topics_raw = topics_obj.get("case_topics", [])
        if topics_raw and "sali_iri" in (topics_raw[0] or {}):
            topics, bad_topics = topics_raw, []
        else:
            topics, bad_topics = resolve_topics(topics_raw, menu)
        summary = topics_obj.get("matter_summary", "")
        print(f"\n[PASS 1 topics] loaded {len(topics)} topics from {args.topics_from} "
              f"-- Opus pass skipped")
    else:
        topics_obj = await run_topics(tenant, menu, topic_docs, args)
        if args.dry_run:
            await run_allegations(tenant, [], alleg_docs, args)
            print("\nDRY RUN complete -- prompts assembled, no frontier calls.")
            return
        topics, bad_topics = resolve_topics(topics_obj.get("case_topics", []), menu)
        summary = topics_obj.get("matter_summary", "")

    if args.dry_run:
        await run_allegations(tenant, [], alleg_docs, args)
        print("\nDRY RUN complete -- prompts assembled, no frontier calls.")
        return

    print("\n========== CONSOLIDATED CASE TAXONOMY (print-only) ==========")
    print("matter_summary:", (summary or "").strip())
    print(f"\ncase_topics ({len(topics)} resolved, {len(bad_topics)} dropped bad-number):")
    for t in topics:
        print(f"  #{t.get('menu_number','-')} [{t.get('sali_code') or '----'}] {t.get('topic_label')} "
              f"(conf={t.get('confidence')})")
    if bad_topics:
        print("  DROPPED (menu_number out of range/missing):",
              [t.get("topic_label") or t.get("menu_number") for t in bad_topics])

    allegs, alleg_cost, truncations = await run_allegations(
        tenant, topics, alleg_docs, args)

    grounded = sum(1 for a in allegs if a.get("status") == "grounded")
    proposed = sum(1 for a in allegs if a.get("status") == "proposed")
    located = sum(1 for a in allegs if (a.get("source") or {}).get("char_start") is not None)
    by_side, by_doc = {}, {}
    for a in allegs:
        by_side[a.get("party_side", "?")] = by_side.get(a.get("party_side", "?"), 0) + 1
        by_doc[a.get("_filename", "?")] = by_doc.get(a.get("_filename", "?"), 0) + 1

    print(f"\nallegations: {len(allegs)} total ({grounded} grounded, {proposed} proposed) "
          f"across {len(alleg_docs)} docs | spans located={located}")
    print("  by side:", ", ".join(f"{k}={v}" for k, v in sorted(by_side.items())))
    print("  by doc:")
    for fn, n in sorted(by_doc.items(), key=lambda kv: -kv[1]):
        print(f"     {n:4d}  {fn[:60]}")
    if truncations:
        print(f"\n!! {len(truncations)} batch issue(s) -- NOT silently dropped:")
        for fn, bi, why in truncations:
            print(f"     {why}  batch {bi}  {fn[:50]}")

    consolidated = {"matter_summary": summary,
                    "case_topics": topics, "allegations": allegs}
    with open(f"{RAW_DIR}/case_seed_consolidated.json", "w") as f:
        json.dump(consolidated, f, indent=2)
    print(f"\nPRINT-ONLY. Consolidated -> {RAW_DIR}/case_seed_consolidated.json "
          f"| allegation-pass cost ${alleg_cost:.4f}")


def main():
    ap = argparse.ArgumentParser(description="Job A v4: resolve-not-transcribe case taxonomy")
    ap.add_argument("--doc-id", action="append")
    ap.add_argument("--topic-doc-id", action="append")
    ap.add_argument("--topic-budget-chars", type=int, default=600000)
    ap.add_argument("--topic-purpose", default="case_seed")
    ap.add_argument("--alleg-purpose", default="analysis")
    ap.add_argument("--alleg-batch-sections", type=int, default=60,
                    help="Sections per allegation call (lower if batches hit the cap)")
    ap.add_argument("--topics-from", default=None,
                    help="Load case_topics from a saved JSON (skip the Opus topic pass)")
    ap.add_argument("--topic-max-tokens", type=int, default=16000)
    ap.add_argument("--alleg-max-tokens", type=int, default=16000)
    ap.add_argument("--http-timeout", type=float, default=600)
    ap.add_argument("--tenant", default=None)
    ap.add_argument("--matter-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
