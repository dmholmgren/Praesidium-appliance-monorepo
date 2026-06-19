#!/usr/bin/env python3
"""
parity_check.py — verify RunPod burst embeddings match the V100 production
service (same CourtListener vector space). Run INSIDE praesidium-web:

    docker exec -it praesidium-web python /app/jobs/parity_check.py

Pulls N Marcus docs, embeds each on BOTH the local /embed service and the
RunPod endpoint, reports per-doc cosine. Pass = all >= 0.9999. Stdlib only.
"""
import os
import sys
import json
import math
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from praesidium_vault import _get_db_conn, get_api_key  # noqa: E402

TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
MARCUS = "d389d889-4fe9-41c9-b74e-622d93d244f8"
N = int(os.getenv("N", "20"))
CAP = 32000
THRESH = 0.9999

LOCAL_URL = os.getenv("EMBED_URL", "http://praesidium-embed:8000/embed")
EP = os.getenv("RUNPOD_ENDPOINT_ID", "dimazls6zqghjk")
KEY = os.getenv("RUNPOD_API_KEY") or get_api_key("runpod", TENANT)
if not KEY:
    print("no RunPod key (vault provider='runpod' or RUNPOD_API_KEY env)")
    sys.exit(1)
RP = f"https://api.runpod.ai/v2/{EP}"
TEXT = "COALESCE(NULLIF(normalized_text,''), extracted_text, ocr_text)"


def _post(url, payload, headers=None, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, headers=None, timeout=600):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_docs():
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT d.id, LEFT({TEXT}, %s) FROM ediscovery_documents d "
        f"WHERE d.collection_id IN (SELECT id FROM ediscovery_collections WHERE matter_id=%s) "
        f"AND {TEXT} IS NOT NULL ORDER BY d.id LIMIT %s",
        (CAP, MARCUS, N))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def local_embed(texts):
    return _post(LOCAL_URL, {"texts": texts, "input_type": "document"})["embeddings"]


def runpod_embed(texts):
    auth = {"Authorization": f"Bearer {KEY}"}
    job = _post(f"{RP}/run", {"input": {"texts": texts, "input_type": "document"}}, auth)["id"]
    while True:
        s = _get(f"{RP}/status/{job}", auth)
        st = s.get("status")
        if st == "COMPLETED":
            return s["output"]["embeddings"]
        if st in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise RuntimeError(f"runpod {st}: {s.get('error')}")
        time.sleep(2)


def cosine(u, v):
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


def main():
    rows = fetch_docs()
    if not rows:
        print("no Marcus docs with text found")
        return
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    print(f"comparing {len(texts)} Marcus docs (V100 vs RunPod)...")
    A = local_embed(texts)
    B = runpod_embed(texts)
    cos = [cosine(A[i], B[i]) for i in range(len(texts))]
    worst, mean, best = min(cos), sum(cos) / len(cos), max(cos)
    print(f"cosine  min={worst:.6f}  mean={mean:.6f}  max={best:.6f}")
    fails = [i for i, c in enumerate(cos) if c < THRESH]
    print(f"threshold {THRESH}: {len(cos) - len(fails)}/{len(cos)} pass, {len(fails)} fail")
    for i in sorted(range(len(cos)), key=lambda k: cos[k])[:5]:
        print(f"  {ids[i]}  cos={cos[i]:.6f}")
    print("PARITY OK — clear to drain" if not fails else "PARITY MISMATCH — investigate before drain")


if __name__ == "__main__":
    main()
