"""
Image-production PDF rendition builder.

Relativity-style image productions deliver one TIFF/JPG per page plus an
OPT page map. Browsers can't render TIFF (and can only show one JPG at a
time), so the review viewer falls back to "download". This module stitches
each document's page images into a multi-page PDF rendition at
    renditions/image_stitch/{doc_id}.pdf
(relative to the collection storage_path) and stamps
    ediscovery_documents.rendition_path / rendition_kind / page_count.

The review viewer (routes/review.py::document_file) already prefers
rendition_path, so stitched docs render inline immediately.

Provenance: the rendition is a derived artifact. The forensic originals
(page images under originals/unpacked/) are never modified.

Usage (inside praesidium-web container):
    python -m modules.ediscovery.jobs.image_rendition <collection_id>
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger("image_rendition")

IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}

try:
    import img2pdf  # lossless JPEG passthrough, low memory
    _HAVE_IMG2PDF = True
except ImportError:
    _HAVE_IMG2PDF = False


def _connect():
    import psycopg2
    raw = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    if not raw:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(raw)


def _build_image_index(unpacked: str) -> dict:
    """STEM (upper) -> absolute path for every page image under unpacked/."""
    idx = {}
    for root, _dirs, files in os.walk(unpacked):
        for fname in files:
            p = Path(fname)
            if p.suffix.lower() in IMAGE_EXTS:
                idx.setdefault(p.stem.upper(), os.path.join(root, fname))
    return idx


def _parse_opt_page_map(unpacked: str) -> dict:
    """
    Parse Opticon (.opt) files found under unpacked/.
    Returns {first_page_bates_upper: [page_bates, page_bates, ...]} in page order.
    OPT line: BATES,VOLUME,PATH,DOCBREAK(Y/blank),FOLDERBREAK,BOXBREAK,PAGECOUNT
    Path claims are NOT trusted -- pages resolve via the on-disk stem index.
    """
    import glob
    page_map = {}
    opt_files = glob.glob(os.path.join(unpacked, "**", "*.opt"), recursive=True)
    for opt in opt_files:
        current_key = None
        try:
            with open(opt, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.rstrip("\r\n").split(",")
                    if len(parts) < 4 or not parts[0].strip():
                        continue
                    bates = parts[0].strip()
                    is_break = parts[3].strip().upper() == "Y"
                    if is_break or current_key is None:
                        current_key = bates.upper()
                        page_map.setdefault(current_key, [])
                    page_map[current_key].append(bates)
        except Exception as exc:
            logger.warning("OPT parse failed for %s: %s", opt, exc)
    return page_map


def _bates_range_pages(bates_begin: str, bates_end: str) -> list:
    """Fallback: enumerate page bates numerically, preserving zero padding."""
    m1 = re.match(r"^(.*?)(\d+)\s*$", bates_begin or "")
    m2 = re.match(r"^(.*?)(\d+)\s*$", bates_end or bates_begin or "")
    if not m1 or not m2 or m1.group(1) != m2.group(1):
        return [bates_begin] if bates_begin else []
    prefix, d1 = m1.group(1), m1.group(2)
    d2 = m2.group(2)
    width = len(d1)
    start, end = int(d1), int(d2)
    if end < start or (end - start) > 20000:
        return [bates_begin]
    return [f"{prefix}{n:0{width}d}" for n in range(start, end + 1)]


def _stitch_pil(page_paths: list, out_path: str) -> int:
    """PIL fallback stitcher. Returns page count written."""
    from PIL import Image, ImageSequence
    frames = []
    for p in page_paths:
        with Image.open(p) as im:
            for fr in ImageSequence.Iterator(im):
                if fr.mode in ("1", "L", "RGB"):
                    frames.append(fr.copy())
                else:
                    frames.append(fr.convert("RGB"))
    if not frames:
        return 0
    frames[0].save(out_path, "PDF", save_all=True,
                   append_images=frames[1:], resolution=200.0)
    n = len(frames)
    for f in frames:
        f.close()
    return n


def _stitch_img2pdf(page_paths: list, out_path: str) -> int:
    """img2pdf stitcher -- JPEG passthrough, streams TIFFs. Returns page count."""
    with open(out_path, "wb") as fh:
        fh.write(img2pdf.convert(page_paths))
    return len(page_paths)


def stitch_collection_images(tenant_id: str, collection_id: str,
                             storage_path: str = None,
                             only_missing: bool = True) -> dict:
    """
    Build multi-page PDF renditions for every image-format document in a
    collection. Idempotent: skips docs that already have a rendition when
    only_missing=True.
    """
    stats = {"candidates": 0, "stitched": 0, "skipped": 0, "errors": 0}
    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if not storage_path:
                cur.execute(
                    "SELECT storage_path FROM ediscovery_collections "
                    "WHERE id = CAST(%s AS uuid)", (collection_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    raise RuntimeError(f"collection {collection_id} has no storage_path")
                storage_path = row[0]

            unpacked = os.path.join(storage_path, "originals", "unpacked")
            if not os.path.isdir(unpacked):
                logger.warning("no unpacked dir at %s", unpacked)
                return stats

            img_idx = _build_image_index(unpacked)
            opt_map = _parse_opt_page_map(unpacked)

            cond_missing = "AND ed.rendition_path IS NULL" if only_missing else ""
            cur.execute(f"""
                SELECT ed.id::text, ed.bates_begin, ed.bates_end, ed.file_path
                FROM ediscovery_documents ed
                WHERE ed.collection_id = CAST(%s AS uuid)
                  AND trim(ed.tenant_id::text) = trim(%s)
                  AND (ed.mime_type LIKE 'image/%%')
                  {cond_missing}
                ORDER BY ed.bates_begin
            """, (collection_id, tenant_id))
            docs = cur.fetchall()
            stats["candidates"] = len(docs)

            rend_dir = os.path.join(storage_path, "renditions", "image_stitch")
            os.makedirs(rend_dir, exist_ok=True)

            for doc_id, b_begin, b_end, file_path in docs:
                try:
                    pages = opt_map.get((b_begin or "").upper())
                    if not pages:
                        pages = _bates_range_pages(b_begin, b_end)
                    page_paths = [img_idx[p.upper()] for p in pages
                                  if p.upper() in img_idx]
                    if not page_paths and file_path:
                        # last resort: the doc's own primary image
                        abs_fp = os.path.join(storage_path, file_path)
                        if os.path.exists(abs_fp):
                            page_paths = [abs_fp]
                    if not page_paths:
                        stats["skipped"] += 1
                        continue

                    out_abs = os.path.join(rend_dir, f"{doc_id}.pdf")
                    if _HAVE_IMG2PDF:
                        try:
                            n_pages = _stitch_img2pdf(page_paths, out_abs)
                        except Exception:
                            n_pages = _stitch_pil(page_paths, out_abs)
                    else:
                        n_pages = _stitch_pil(page_paths, out_abs)

                    if not n_pages or not os.path.exists(out_abs):
                        stats["errors"] += 1
                        continue

                    rel = os.path.join("renditions", "image_stitch", f"{doc_id}.pdf")
                    cur.execute("""
                        UPDATE ediscovery_documents
                        SET rendition_path = %s,
                            rendition_kind = 'image_stitch',
                            page_count = COALESCE(page_count, %s)
                        WHERE id = CAST(%s AS uuid)
                    """, (rel, n_pages, doc_id))
                    stats["stitched"] += 1
                    if stats["stitched"] % 50 == 0:
                        conn.commit()
                        logger.info("stitched %d/%d", stats["stitched"], len(docs))
                except Exception as exc:
                    stats["errors"] += 1
                    logger.warning("stitch failed for doc %s (%s): %s",
                                   doc_id, b_begin, exc)
            conn.commit()
    finally:
        conn.close()
    logger.info("stitch_collection_images %s: %s", collection_id, stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m modules.ediscovery.jobs.image_rendition <collection_id> [--all]")
        sys.exit(1)
    cid = sys.argv[1]
    only_missing = "--all" not in sys.argv[2:]
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT trim(tenant_id::text), storage_path "
                    "FROM ediscovery_collections WHERE id = CAST(%s AS uuid)", (cid,))
        row = cur.fetchone()
    conn.close()
    if not row:
        print(f"collection {cid} not found")
        sys.exit(2)
    out = stitch_collection_images(row[0], cid, row[1], only_missing=only_missing)
    print(out)
