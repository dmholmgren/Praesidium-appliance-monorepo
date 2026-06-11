#!/usr/bin/env python3
"""jobs/write_case_seed.py v2 -- persist consolidated case seed with a §0 in-bounds
span assertion. Builder v4 derives spans by locating allegation_text in the section
(in-bounds by construction); this writer enforces it: every span bound-checked against
the source doc's max(section.char_end); invalid/unlocated spans reported in the dry-run
plan and SKIPPED on --write. DRY-RUN BY DEFAULT. Supersede/provenance/party-side preserved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from core.db.base import AsyncSessionLocal  # noqa: E402

DEFAULT_TENANT = os.environ.get("TENANT_ID", "")


async def resolve_section(session, tenant, doc_id, section_index):
    """Resolve the section row for (doc, section_index): id + char bounds, or None."""
    if doc_id is None or section_index is None:
        return None
    row = (await session.execute(text("""
        SELECT id::text AS id, char_start, char_end FROM document_sections
        WHERE TRIM(tenant_id)=:t AND dms_document_id=CAST(:d AS uuid)
          AND section_index=:idx AND superseded_by_run_id IS NULL
        ORDER BY id LIMIT 1
    """), {"t": tenant, "d": doc_id, "idx": section_index})).mappings().first()
    return dict(row) if row else None


def span_ok(cs, ce, sec_cs, sec_ce):
    """§0 section-level: span present, ordered, and within the LINKED section bounds."""
    if cs is None or ce is None or sec_cs is None or sec_ce is None:
        return False
    try:
        cs = int(cs); ce = int(ce); sec_cs = int(sec_cs); sec_ce = int(sec_ce)
    except (TypeError, ValueError):
        return False
    return sec_cs <= cs < ce <= sec_ce


async def main_async(args):
    tenant = (args.tenant or DEFAULT_TENANT).strip()
    if not tenant:
        print("ERROR: no tenant"); sys.exit(1)
    matter_id = args.matter_id
    with open(args.consolidated) as f:
        seed = json.load(f)
    topics = seed.get("case_topics", [])
    allegs = seed.get("allegations", [])
    if not topics:
        print("ERROR: no case_topics in consolidated file"); sys.exit(1)
    seed_run_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        mrow = (await session.execute(text("""
            SELECT matter_name, matter_number FROM matters
            WHERE id=CAST(:m AS uuid) AND TRIM(tenant_id)=:t
        """), {"m": matter_id, "t": tenant})).first()
        if not mrow:
            print(f"ERROR: matter {matter_id} not found for tenant"); sys.exit(1)

        menu_iris = {r[0] for r in (await session.execute(text(
            "SELECT iri FROM sali_concepts WHERE branch='area_of_law'"))).all()}
        bad_topics = [t for t in topics if t.get("sali_iri") not in menu_iris]

        resolved = unresolved = 0
        sec_cache = {}
        for a in allegs:
            src = a.get("source", {}) or {}
            doc_id = src.get("doc_id")
            key = (doc_id, src.get("section_index"))
            if key not in sec_cache:
                sec_cache[key] = await resolve_section(
                    session, tenant, doc_id, src.get("section_index"))
            sec = sec_cache[key]
            a["_section_id"] = sec["id"] if sec else None
            resolved += 1 if a["_section_id"] else 0
            unresolved += 0 if a["_section_id"] else 1
            cs, ce = src.get("char_start"), src.get("char_end")
            a["_span_ok"] = span_ok(cs, ce,
                                    sec["char_start"] if sec else None,
                                    sec["char_end"] if sec else None)

        span_valid = sum(1 for a in allegs if a.get("_span_ok"))
        span_bad = len(allegs) - span_valid

        ex_t = (await session.execute(text(
            "SELECT count(*) FROM case_topics WHERE matter_id=CAST(:m AS uuid) "
            "AND superseded_by_run_id IS NULL"), {"m": matter_id})).scalar()
        ex_a = (await session.execute(text(
            "SELECT count(*) FROM allegations WHERE matter_id=CAST(:m AS uuid) "
            "AND superseded_by_run_id IS NULL"), {"m": matter_id})).scalar()
        ex_t_acc = (await session.execute(text(
            "SELECT count(*) FROM case_topics WHERE matter_id=CAST(:m AS uuid) "
            "AND superseded_by_run_id IS NULL AND status='accepted'"), {"m": matter_id})).scalar()
        ex_a_acc = (await session.execute(text(
            "SELECT count(*) FROM allegations WHERE matter_id=CAST(:m AS uuid) "
            "AND superseded_by_run_id IS NULL AND status='accepted'"), {"m": matter_id})).scalar()

        byside = {}
        for a in allegs:
            byside[a.get("party_side", "?")] = byside.get(a.get("party_side", "?"), 0) + 1
        grounded = sum(1 for a in allegs if (a.get("sali_iris") or []))

        print(f"=== WRITE PLAN  seed_run_id={seed_run_id} ===")
        print(f"matter: {mrow[0]} ({mrow[1]})  id={matter_id}")
        print(f"topics: {len(topics)}  ({len(bad_topics)} IRIs NOT in SALI menu)")
        print(f"allegations: {len(allegs)}  | grounded(has-topic)={grounded} "
              f"ungrounded={len(allegs)-grounded}")
        print(f"\u00a70 spans: {span_valid} valid, {span_bad} invalid "
              f"(invalid spans are SKIPPED on --write, not written)")
        print(f"section_id resolved={resolved}  unresolved={unresolved} "
              f"(unresolved write with NULL section_id)")
        print("party_side (as-pled, stored in attributes):",
              ", ".join(f"{k}={v}" for k, v in sorted(byside.items())))
        print(f"existing LIVE rows for matter: {ex_t} topics ({ex_t_acc} accepted), "
              f"{ex_a} allegations ({ex_a_acc} accepted)")
        print(f"  -> on --write, the {ex_t-ex_t_acc} non-accepted topics and "
              f"{ex_a-ex_a_acc} non-accepted allegations would be SUPERSEDED (not deleted)")
        if bad_topics:
            print("  WARNING: topic IRIs not in menu:",
                  [t.get("sali_code") or t.get("topic_label") for t in bad_topics])
        if span_bad:
            print(f"  NOTE: {span_bad} allegations have invalid/unlocated spans and will be skipped.")

        if not args.write:
            print("\nDRY RUN -- nothing written. Re-run with --write to commit.")
            return

        await session.execute(text("""
            UPDATE case_topics SET superseded_by_run_id=CAST(:r AS uuid), updated_at=now()
            WHERE matter_id=CAST(:m AS uuid) AND superseded_by_run_id IS NULL
              AND status<>'accepted'"""), {"r": seed_run_id, "m": matter_id})
        await session.execute(text("""
            UPDATE allegations SET superseded_by_run_id=CAST(:r AS uuid), updated_at=now()
            WHERE matter_id=CAST(:m AS uuid) AND superseded_by_run_id IS NULL
              AND status<>'accepted'"""), {"r": seed_run_id, "m": matter_id})

        iri2id = {}
        for t in topics:
            attrs = json.dumps({"sali_iri": t.get("sali_iri"),
                                "sali_code": t.get("sali_code"),
                                "rationale": t.get("rationale")})
            tid = (await session.execute(text("""
                INSERT INTO case_topics
                  (tenant_id, matter_id, topic_label, status, confidence, attribution,
                   seed_run_id, attributes)
                VALUES (:tn, CAST(:m AS uuid), :lbl, 'proposed', :conf, 'frontier',
                        CAST(:r AS uuid), CAST(:attrs AS jsonb))
                RETURNING id::text"""),
                {"tn": tenant, "m": matter_id, "lbl": t.get("topic_label"),
                 "conf": t.get("confidence"), "r": seed_run_id, "attrs": attrs})).scalar()
            iri2id[t.get("sali_iri")] = tid

        written = skipped = 0
        for a in allegs:
            if not a.get("_span_ok"):
                skipped += 1
                continue
            iris = a.get("sali_iris") or []
            primary = iris[0] if iris else None
            ctid = iri2id.get(primary)
            src = a.get("source", {}) or {}
            attrs = json.dumps({"party_side_as_pled": a.get("party_side"),
                                "grounding": a.get("status"),
                                "sali_iris": iris,
                                "source_filename": a.get("_filename")})
            await session.execute(text("""
                INSERT INTO allegations
                  (tenant_id, matter_id, case_topic_id, allegation_text, allegation_type,
                   source_doc_id, section_id, source_char_start, source_char_end,
                   status, confidence, attribution, seed_run_id, attributes)
                VALUES (:tn, CAST(:m AS uuid), CAST(:ctid AS uuid), :txt, :atype,
                        CAST(:sd AS uuid), CAST(:secid AS uuid), :cs, :ce,
                        'proposed', :conf, 'frontier', CAST(:r AS uuid), CAST(:attrs AS jsonb))
            """), {"tn": tenant, "m": matter_id, "ctid": ctid,
                   "txt": a.get("allegation_text"), "atype": a.get("allegation_type"),
                   "sd": src.get("doc_id"), "secid": a.get("_section_id"),
                   "cs": src.get("char_start"), "ce": src.get("char_end"),
                   "conf": a.get("confidence"), "r": seed_run_id, "attrs": attrs})
            written += 1

        await session.commit()
        print(f"\nWROTE {len(topics)} topics + {written} allegations "
              f"(skipped {skipped} with invalid/unlocated spans) "
              f"under seed_run_id={seed_run_id}")
        print("All rows status='proposed' (awaiting review). party_side stored as-pled; "
              "run the realignment resolver next to set authoritative side from the roster.")


def main():
    ap = argparse.ArgumentParser(description="Persist consolidated case seed (dry-run by default)")
    ap.add_argument("--matter-id", required=True, help="Praesidium matters.id to stamp rows with")
    ap.add_argument("--consolidated", default="/tmp/case_seed_consolidated.json")
    ap.add_argument("--tenant", default=None)
    ap.add_argument("--write", action="store_true", help="Actually commit (default is dry-run)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
