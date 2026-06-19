#!/usr/bin/env python3
"""
drain_marcus.py — embed Marcus docs via the RunPod burst endpoint and write
768-d vectors back to ediscovery_documents.embedding. Run INSIDE praesidium-web.

Multi-hour job but fully RESUMABLE: selection is `embedding IS NULL`, so a
restart resumes exactly where it stopped. Run it detached (nohup/tmux).

    nohup docker exec praesidium-web python /app/jobs/drain_marcus.py \
        --concurrency 10 > /tmp/drain_marcus.log 2>&1 &
    tail -f /tmp/drain_marcus.log

Selection: Marcus collections, has text, embedding IS NULL, NOT is_duplicate.
Run dedup FIRST so is_duplicate drops the ~37% exact-dup emails.
Parity: sends RAW text; the handler applies prefix + L2 (verified 0.999991+).
"""
import os
import sys
import json
import time
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from praesidium_vault import _get_db_conn, get_api_key  # noqa: E402

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
MARCUS = "d389d889-4fe9-41c9-b74e-622d93d244f8"
CAP = 32000
EP = os.getenv("RUNPOD_ENDPOINT_ID", "dimazls6zqghjk")
KEY = os.getenv("RUNPOD_API_KEY") or get_api_key("runpod", TENANT)
RP = f"https://api.runpod.ai/v2/{EP}"
AUTH = {"Authorization": f"Bearer {KEY}"} if KEY else {}
TEXT = "COALESCE(NULLIF(normalized_text,''), extracted_text, ocr_text)"

WHERE = (f"WHERE d.collection_id IN (SELECT id FROM ediscovery_collections WHERE matter_id=%s) "
         f"AND d.embedding IS NULL AND NOT COALESCE(d.is_duplicate, FALSE) AND {TEXT} IS NOT NULL")
SELECT = f"SELECT d.id, LEFT({TEXT}, {CAP}) FROM ediscovery_documents d {WHERE} ORDER BY d.id LIMIT %s"
COUNT = f"SELECT COUNT(*) FROM ediscovery_documents d {WHERE}"
UPDATE = "UPDATE ediscovery_documents SET embedding=%s, updated_at=NOW() WHERE id=%s"


def _post(url, payload, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **AUTH})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, timeout=600):
    req = urllib.request.Request(url, headers=AUTH)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def embed_batch(rows):
    ids = [r[0] for r in rows]
    job = _post(f"{RP}/run", {"input": {"texts": [r[1] for r in rows], "input_type": "document"}})["id"]
    while True:
        s = _get(f"{RP}/status/{job}")
        st = s.get("status")
        if st == "COMPLETED":
            vecs = s["output"]["embeddings"]
            if len(vecs) != len(ids):
                raise RuntimeError(f"count mismatch {len(ids)}->{len(vecs)}")
            return list(zip(ids, vecs))
        if st in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise RuntimeError(f"job {st}: {s.get('error')}")
        time.sleep(2)


def main():
    if not KEY:
        print("no RunPod key (vault provider='runpod' or RUNPOD_API_KEY)")
        sys.exit(1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args()
    page = args.batch * args.concurrency

    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute(COUNT, (MARCUS,))
    print(f"pending: {cur.fetchone()[0]:,}", flush=True)

    done = 0
    t0 = time.time()
    while True:
        cur.execute(SELECT, (MARCUS, page))
        rows = cur.fetchall()
        if not rows:
            break
        batches = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(embed_batch, b): b for b in batches}
            for fut in as_completed(futs):
                try:
                    pairs = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"batch failed (retry next pass): {e}", flush=True)
                    continue
                for did, vec in pairs:
                    cur.execute(UPDATE, (json.dumps(vec), did))
                conn.commit()
                done += len(pairs)
        print(f"embedded {done:,}  ({done / max(time.time() - t0, 1e-6):.0f}/s)", flush=True)
    print(f"DONE embedded {done:,} in {(time.time() - t0) / 60:.1f} min", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
