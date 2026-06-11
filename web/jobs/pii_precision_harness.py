#!/usr/bin/env python3
"""
pii_precision_harness.py  (v17.5)

Inline PII precision pass. Samples DMS-only, non-eDiscovery docs with real
text, runs the extraction engine (which now emits _extract_pii rows into
document_entities), then reports the per-type distribution and the
lowest-confidence matches per type -- that's where false positives hide.

Run INSIDE a container (imports the jobs package + has DB env):
    sudo cp /tmp/pii_precision_harness.py /opt/praesidium-web/jobs/
    docker exec praesidium-proc-worker-1 python -m jobs.pii_precision_harness 50

Arg: sample size (default 50). Read-only except the extraction rows it
creates under one fresh extraction_run (fully superseded/auditable).
"""
import sys
import uuid

import psycopg2.extras

from jobs.run_extraction import get_db_conn, classify_text
from jobs.extraction_template_engine import run_extraction_template

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# eDiscovery folders that must NOT be fed to extraction (onboarding gap:
# dms_documents holds both DMS working docs and eDiscovery production files).
EDISCOVERY_PATH_RE = r"(/01-|/12-ediscovery/|/production/|/load[_-]?files/|/natives/|/images/)"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    run_id = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO extraction_runs
           (id, tenant_id, run_type, source_type, status, started_at)
           VALUES (%s, %s, 'pii_precision_pass', 'legacy_inventory',
                   'processing', NOW())""",
        (run_id, TENANT),
    )
    print(f"extraction_run: {run_id}")

    cur.execute(
        f"""SELECT id, file_path, content_text
            FROM dms_documents
            WHERE TRIM(tenant_id) = %s
              AND content_text IS NOT NULL
              AND length(content_text) > 200
              AND file_path !~* %s
            ORDER BY id
            LIMIT %s""",
        (TENANT, EDISCOVERY_PATH_RE, n),
    )
    docs = cur.fetchall()
    print(f"sampled {len(docs)} DMS (non-eDiscovery) docs\n")

    total_pii = 0
    by_type_docs = {}
    for i, d in enumerate(docs):
        text = d["content_text"] or ""
        fpath = d["file_path"] or ""
        doc_type, _conf = classify_text(text, fpath)
        by_type_docs[doc_type] = by_type_docs.get(doc_type, 0) + 1
        try:
            res = run_extraction_template(
                tenant_id=TENANT,
                document_id=str(d["id"]),
                run_id=run_id,
                content_text=text,
                document_type_code=doc_type,
                source_table="dms",
                file_path=fpath,
            )
            pii = res.get("pii", 0)
            total_pii += pii
            if pii:
                print(f"  [{i+1}/{len(docs)}] {doc_type:18s} pii={pii:<3d} "
                      f"{fpath.split('/')[-1][:55]}")
        except Exception as exc:
            print(f"  [{i+1}/{len(docs)}] ERROR {exc}")

    print(f"\n=== total PII rows: {total_pii} ===")
    print("doc types in sample:", dict(sorted(by_type_docs.items(),
                                              key=lambda kv: -kv[1])))

    print("\n=== distribution by pii_type ===")
    cur.execute(
        """SELECT pii_type, count(*) hits, round(avg(confidence),2) avg_conf,
                  count(DISTINCT normalized_value) distinct_vals
           FROM document_entities
           WHERE extraction_run_id = %s AND is_pii
           GROUP BY pii_type ORDER BY hits DESC""",
        (run_id,),
    )
    for r in cur.fetchall():
        print(f"  {r['pii_type']:16s} hits={r['hits']:<5d} "
              f"avg_conf={r['avg_conf']}  distinct={r['distinct_vals']}")

    print("\n=== lowest-confidence sample per type (false-positive hunt) ===")
    cur.execute(
        """SELECT pii_type, entity_text, normalized_value, confidence,
                  page_number
           FROM (
             SELECT *, row_number() OVER (
                        PARTITION BY pii_type ORDER BY confidence, entity_text
                      ) rn
             FROM document_entities
             WHERE extraction_run_id = %s AND is_pii
           ) s
           WHERE rn <= 8
           ORDER BY pii_type, confidence""",
        (run_id,),
    )
    for r in cur.fetchall():
        txt = (r["entity_text"] or "")[:40]
        nv = (r["normalized_value"] or "")[:24]
        print(f"  {r['pii_type']:16s} conf={r['confidence']} "
              f"p{r['page_number']:<4} '{txt}'  -> '{nv}'")

    cur.execute(
        "UPDATE extraction_runs SET status='completed', completed_at=NOW() "
        "WHERE id=%s", (run_id,))
    print(f"\nrun_id for follow-up queries: {run_id}")


if __name__ == "__main__":
    main()
