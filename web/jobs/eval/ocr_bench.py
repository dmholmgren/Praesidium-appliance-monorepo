"""
ocr_bench.py -- OCR engine evaluation harness (accuracy + throughput).

Measures an OCR engine against born-digital ground truth: a PDF that already has
a real text layer is the known-correct text. We rasterize each page, OCR the
image, and score the OCR output against that page's text-layer text (the §0
canonical, split per page on form-feed). Two metrics, so a reading-order quirk
can't masquerade as an accuracy difference:

  cer_norm       normalized character error rate = Levenshtein(gt, hyp) / len(gt),
                 on whitespace-collapsed text (lower is better)
  content_ratio  rapidfuzz token_sort_ratio in [0,1], order-insensitive content
                 recovery (higher is better)

Per-page OCR wall time (engine call only, rasterization excluded so the engine
comparison is pure) -> pages/sec, chars/sec. Results are written to
eval_ocr_results (durable, queryable) keyed by a run_id, so engine choices are
made on recorded data, not assertion.

Engine-agnostic: --engine tesseract runs anywhere tesseract+pytesseract exist
(the praesidium-web container); --engine surya lazy-imports surya and is meant to
run from an isolated venv with surya-ocr installed. Same code, same ground truth,
same metrics -> a fair, reproducible bake-off.

    python jobs/eval/ocr_bench.py --engine tesseract [--docs id,id] [--dpi 300] [--pages N] [--run-id UUID]
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
import uuid
from urllib.parse import urlparse

import fitz  # PyMuPDF
from PIL import Image
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
GT_DOCS_DEFAULT = [
    "d19cd919-4a6e-4d47-9219-9341f5c21e90",  # Plea/MTD (89p) compound
    "c070cf1c-cd73-4f8e-8f22-7f887c772282",  # Defendants' 2nd Am. Answer (45p)
]
_WS = re.compile(r"\s+")

DDL = """
CREATE TABLE IF NOT EXISTS eval_ocr_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    tenant_id varchar(36) NOT NULL,
    engine text NOT NULL,
    engine_version text,
    device text,
    dpi integer NOT NULL,
    doc_id uuid NOT NULL,
    page_no integer NOT NULL,
    gt_chars integer NOT NULL,
    hyp_chars integer NOT NULL,
    cer_norm double precision NOT NULL,
    content_ratio double precision NOT NULL,
    ocr_secs double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_eval_ocr_run ON eval_ocr_results (run_id);
"""


# ----------------------------------------------------------------- db
def _db_kwargs():
    raw = os.environ.get("DATABASE_URL", "")
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(p):
            raw = "postgresql://" + raw[len(p):]
            break
    q = urlparse(raw)
    return {"dbname": q.path.lstrip("/") or "praesidium", "user": q.username or "praesidium",
            "password": q.password or "", "host": q.hostname or "172.28.0.1",
            "port": str(q.port or 5432)}


def _connect():
    import psycopg2
    c = psycopg2.connect(**_db_kwargs())
    c.autocommit = False
    return c


def _norm(t):
    return _WS.sub(" ", t or "").strip()


# ----------------------------------------------------------------- ground truth
def load_pages(cur, tenant, doc_id):
    """Return (file_path, [per-page ground-truth text])."""
    cur.execute("SELECT canonical_text, page_count FROM doc_geometry "
                "WHERE corpus='dms' AND doc_id=%s::uuid", (doc_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError("no doc_geometry header for %s" % doc_id)
    canonical, page_count = row
    pages = canonical.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    cur.execute("SELECT file_path FROM dms_documents WHERE id=%s::uuid AND TRIM(tenant_id)=%s",
                (doc_id, tenant))
    fp = cur.fetchone()
    if not fp:
        raise ValueError("no dms_documents.file_path for %s" % doc_id)
    return fp[0], pages, page_count


# ----------------------------------------------------------------- engines
def _tess_version():
    import pytesseract
    return "tesseract " + str(pytesseract.get_tesseract_version())


def run_tesseract(img):
    import pytesseract
    return pytesseract.image_to_string(img)


_SURYA = {}


def _surya_version():
    try:
        import importlib.metadata as md
        return "surya-ocr " + md.version("surya-ocr")
    except Exception:
        return "surya-ocr ?"


def run_surya(img):
    """Lazy-load surya once; supports both the >=0.8 predictor API and the 0.6.x
    run_ocr API. Returns recognized text in reading order."""
    if "fn" not in _SURYA:
        try:
            from surya.recognition import RecognitionPredictor
            from surya.detection import DetectionPredictor
            _rec = RecognitionPredictor(); _det = DetectionPredictor()
            def _f(im):
                return _rec([im], det_predictor=_det)[0].text_lines
        except Exception:
            from surya.ocr import run_ocr
            from surya.model.detection.model import load_model as _ldet, load_processor as _ldetp
            from surya.model.recognition.model import load_model as _lrec
            from surya.model.recognition.processor import load_processor as _lrecp
            _dm, _dp, _rm, _rp = _ldet(), _ldetp(), _lrec(), _lrecp()
            def _f(im):
                return run_ocr([im], [["en"]], _dm, _dp, _rm, _rp)[0].text_lines
        _SURYA["fn"] = _f
    lines = _SURYA["fn"](img)
    lines = sorted(lines, key=lambda l: (round(l.bbox[1] / 5.0), l.bbox[0]))
    return "\n".join(l.text for l in lines)


ENGINES = {
    "tesseract": (run_tesseract, _tess_version, "cpu"),
    "surya": (run_surya, _surya_version, None),  # device detected below
}


def _surya_device():
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ----------------------------------------------------------------- run
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=list(ENGINES))
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    ap.add_argument("--docs", default=",".join(GT_DOCS_DEFAULT))
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--pages", type=int, default=0, help="cap pages per doc (0=all)")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--min-gt", type=int, default=20, help="skip pages with < this many gt chars")
    args = ap.parse_args()

    engine_fn, ver_fn, device = ENGINES[args.engine]
    if args.engine == "surya":
        device = _surya_device()
    engine_version = ver_fn()
    run_id = args.run_id or str(uuid.uuid4())
    tenant = args.tenant.strip()
    doc_ids = [d.strip() for d in args.docs.split(",") if d.strip()]

    conn = _connect()
    cur = conn.cursor()
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()

    print(f"run_id={run_id} engine={engine_version} device={device} dpi={args.dpi}")
    rows = []
    t_pages = t_secs = t_chars = 0
    cers = []
    for doc_id in doc_ids:
        fp, pages, page_count = load_pages(cur, tenant, doc_id)
        if not os.path.isfile(fp):
            print(f"  !! file missing: {fp}")
            continue
        doc = fitz.open(fp)
        n = min(len(pages), doc.page_count)
        if args.pages:
            n = min(n, args.pages)
        print(f"  {doc_id} :: {os.path.basename(fp)[:50]} :: scoring {n} pages")
        for i in range(n):
            gt = _norm(pages[i])
            if len(gt) < args.min_gt:
                continue
            pix = doc.load_page(i).get_pixmap(dpi=args.dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            t0 = time.perf_counter()
            hyp_raw = engine_fn(img)
            secs = time.perf_counter() - t0
            hyp = _norm(hyp_raw)
            cer = Levenshtein.distance(gt, hyp) / max(len(gt), 1)
            ratio = fuzz.token_sort_ratio(gt, hyp) / 100.0
            rows.append((run_id, tenant, args.engine, engine_version, device, args.dpi,
                         doc_id, i + 1, len(gt), len(hyp), cer, ratio, secs))
            t_pages += 1
            t_secs += secs
            t_chars += len(gt)
            cers.append(cer)
        doc.close()

    if rows:
        cur.executemany(
            "INSERT INTO eval_ocr_results "
            "(run_id, tenant_id, engine, engine_version, device, dpi, doc_id, page_no, "
            " gt_chars, hyp_chars, cer_norm, content_ratio, ocr_secs) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s)", rows)
        conn.commit()
    conn.close()

    cers.sort()
    mean_cer = sum(cers) / len(cers) if cers else 0.0
    med_cer = cers[len(cers) // 2] if cers else 0.0
    print(f"\n=== {args.engine} ({engine_version}, {device}) ===")
    print(f"  pages scored : {t_pages}")
    print(f"  mean CER     : {mean_cer:.4f}   median CER: {med_cer:.4f}")
    print(f"  pages/sec    : {t_pages / t_secs:.2f}" if t_secs else "  pages/sec    : n/a")
    print(f"  chars/sec    : {t_chars / t_secs:.0f}" if t_secs else "  chars/sec    : n/a")
    print(f"  run_id       : {run_id}")


if __name__ == "__main__":
    main()
