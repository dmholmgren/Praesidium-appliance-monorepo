#!/usr/bin/env python3
"""Load the SALI/FOLIO Area-of-Law branch into sali_concepts (canonical, Postgres).

Re-download (not Redis-lift): the canonical codes come fresh from the FOLIO
ontology via folio-python, which loads the OWL from GitHub and normalizes legacy
lmss.sali.org / soli: IRIs automatically. Postgres is the system of record; the
Redis HNSW store and the deferred pgvector HNSW index are regenerable projections.

Scope: branch='area_of_law' only. FOLIO's get_areas_of_law() returns the COMPLETE
area-of-law subtree (verified: 174 returned, 161 unique IRIs, children close back
into the set), so no recursive traversal is needed -- dedupe by IRI and load.
Other branches (document_type, services, player roles) can be added later by
extending BRANCH_LOADERS; the doctype branch is intentionally NOT loaded here
(the existing doctype classifier still uses taxonomy-redis).

This loads ROWS ONLY. `embedding`/`embedded_at` are left untouched -- the separate
embedding pass (voyage-law-2, 1024-dim) fills them and then builds the HNSW index
recorded in migration 0043 (DEFERRED_HNSW). Idempotent: ON CONFLICT (iri) refreshes
content + bumps updated_at WITHOUT clearing embeddings, so the embedding pass can
re-embed only rows whose updated_at > embedded_at.

Field mapping (folio-python OWLClass -> sali_concepts):
    iri           <- c.iri
    sali_code     <- c.identifier            (e.g. 'CIVR'; may be None)
    pref_label    <- c.label                 (NOTE: c.preferred_label is None)
    alt_labels    <- c.alternative_labels    (deduped, pref_label removed)
    definition    <- c.definition
    parent_iris   <- c.sub_class_of          (broader concepts)
    branch        <- 'area_of_law'
    depth         <- computed within-set via sub_class_of (cycle-guarded)
    attributes    <- {"children_iris": c.parent_class_of}

Run (in the praesidium-web container, which has DATABASE_URL + asyncpg):
    docker exec praesidium-web pip install folio-python --break-system-packages
    docker exec praesidium-web python3 /app/jobs/load_sali_folio.py --dry-run
    docker exec praesidium-web python3 /app/jobs/load_sali_folio.py

(folio-python base deps: pydantic, lxml, httpx. Add to requirements.txt for
persistence across image rebuilds; the loaded DATA persists in Postgres regardless.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Job scripts run via `docker exec ... python3 /app/jobs/x.py`; ensure the app
# package root is importable regardless of CWD.
sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from core.db.base import AsyncSessionLocal  # noqa: E402

SOURCE_VERSION = "FOLIO-2.0.0"
BRANCH = "area_of_law"

UPSERT_SQL = text(
    """
    INSERT INTO sali_concepts
        (iri, sali_code, pref_label, alt_labels, definition,
         branch, parent_iris, depth, is_active, source_version, attributes)
    VALUES
        (:iri, :sali_code, :pref_label, :alt_labels, :definition,
         :branch, :parent_iris, :depth, true, :source_version,
         CAST(:attributes AS jsonb))
    ON CONFLICT (iri) DO UPDATE SET
        sali_code      = EXCLUDED.sali_code,
        pref_label     = EXCLUDED.pref_label,
        alt_labels     = EXCLUDED.alt_labels,
        definition     = EXCLUDED.definition,
        branch         = EXCLUDED.branch,
        parent_iris    = EXCLUDED.parent_iris,
        depth          = EXCLUDED.depth,
        source_version = EXCLUDED.source_version,
        attributes     = EXCLUDED.attributes,
        updated_at     = now()
        -- embedding / embedded_at intentionally NOT touched: a re-download must
        -- not wipe vectors. Staleness is handled by the embedding pass comparing
        -- updated_at > embedded_at.
    """
)


def _depth_map(by_iri: dict) -> dict[str, int]:
    """Compute depth = hops to a root (no in-set parent), cycle-guarded."""
    memo: dict[str, int] = {}

    def depth(iri: str, stack: frozenset) -> int:
        if iri in memo:
            return memo[iri]
        if iri in stack:            # cycle: treat as root-ish
            return 0
        c = by_iri.get(iri)
        parents = [p for p in (getattr(c, "sub_class_of", None) or []) if p in by_iri]
        d = 0 if not parents else 1 + min(depth(p, stack | {iri}) for p in parents)
        memo[iri] = d
        return d

    return {iri: depth(iri, frozenset()) for iri in by_iri}


def build_rows() -> list[dict]:
    from folio import FOLIO

    f = FOLIO()
    concepts = f.get_areas_of_law()

    # Dedupe by IRI (helper returns a few dupes).
    by_iri = {c.iri: c for c in concepts}
    depths = _depth_map(by_iri)

    rows: list[dict] = []
    for iri, c in by_iri.items():
        pref = c.label
        alts = [a for a in (getattr(c, "alternative_labels", None) or [])
                if a and a != pref]
        # dedupe alt_labels preserving order
        seen = set()
        alts = [a for a in alts if not (a in seen or seen.add(a))]
        rows.append(
            {
                "iri": iri,
                "sali_code": getattr(c, "identifier", None),
                "pref_label": pref,
                "alt_labels": alts or None,
                "definition": getattr(c, "definition", None),
                "branch": BRANCH,
                "parent_iris": list(getattr(c, "sub_class_of", None) or []) or None,
                "depth": depths.get(iri),
                "source_version": SOURCE_VERSION,
                "attributes": json.dumps(
                    {"children_iris": list(getattr(c, "parent_class_of", None) or [])}
                ),
            }
        )
    return rows


async def load(rows: list[dict]) -> None:
    async with AsyncSessionLocal() as session:
        for r in rows:
            await session.execute(UPSERT_SQL, r)
        await session.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Load SALI/FOLIO area-of-law into sali_concepts")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build rows and print a summary; write nothing.")
    ap.add_argument("--sample", type=int, default=5,
                    help="How many rows to print in the summary.")
    args = ap.parse_args()

    rows = build_rows()
    print(f"FOLIO {SOURCE_VERSION}: built {len(rows)} '{BRANCH}' concepts "
          f"({sum(1 for r in rows if r['definition'])} with definition, "
          f"{sum(1 for r in rows if r['sali_code'])} with code).")
    for r in rows[: args.sample]:
        print(f"  [{r['sali_code'] or '----'}] {r['pref_label']} "
              f"(depth={r['depth']}, parents={len(r['parent_iris'] or [])}) "
              f"{r['iri']}")

    if args.dry_run:
        print("DRY RUN -- nothing written.")
        return

    asyncio.run(load(rows))
    print(f"Upserted {len(rows)} rows into sali_concepts (branch='{BRANCH}'). "
          f"Embeddings unchanged; run the embedding pass next.")


if __name__ == "__main__":
    main()
