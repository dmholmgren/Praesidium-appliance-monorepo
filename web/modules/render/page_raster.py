"""
Page-Raster Service — render core (Spec v1.0, Slice PR-1).

One primitive, three consumers: trial-presentation display, eDiscovery review
viewer, review-set prefetch. A raster is a Layer-3 *regenerable derivative* of the
document bytes (like an embedding): a cache/projection, never the system of
record. It can be deleted and rebuilt at any time -> zero provenance risk.

Source bytes are resolved corpus-aware by REUSING the geometry-service resolvers
(_resolve_dms_file / _resolve_ediscovery_file) so render and geometry agree on
"which file backs this doc." Deterministic render (PyMuPDF); no model in this path.

Cache key: (tenant, corpus, doc_id, rendition, version, page, width). `version`
is a marker of the source bytes (mtime+size) -> re-OCR / new production changes
the file -> new version -> new key -> stale rasters are never served. There is no
in-place overwrite; invalidation is purely by key.
"""

import io
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import fitz  # PyMuPDF
import psycopg2

from modules.ediscovery.services.geometry_service import (
    _conn_kwargs,
    _resolve_dms_file,
    _resolve_ediscovery_file,
)

CACHE_ROOT = os.environ.get("PAGE_RASTER_CACHE", "/datapool/cache/page_raster")
# Soft size budget for the LRU sweep (regenerable cache; safe to evict).
CACHE_MAX_BYTES = int(os.environ.get("PAGE_RASTER_MAX_GB", "20")) * (1024 ** 3)
GC_MIN_BUDGET_BYTES = 1024 ** 3  # eviction floor — never sweep below ~1 GB

DEFAULT_WIDTH = 1600
# Snap requested widths to a small tier set to bound cache cardinality.
# 1600 = display/review tier; 3000 = zoom/fine-print tier (PR-4 consumer).
ALLOWED_WIDTHS = (1200, 1600, 2000, 3000)
WEBP_QUALITY = 82

SUPPORTED_CORPORA = ("dms", "ediscovery")


@dataclass
class RasterResult:
    image_bytes: bytes
    width: int
    height: int
    page: int
    page_count: Optional[int]
    cached: bool


def clamp_width(width) -> int:
    try:
        w = int(width)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    if w <= 0:
        return DEFAULT_WIDTH
    return min(ALLOWED_WIDTHS, key=lambda t: abs(t - w))


def _resolve_source(tenant_id: str, corpus: str, doc_id: str):
    """Return (abs_path, doc_type, version). version = source-bytes marker.

    Tenant scoping is enforced inside the reused resolvers (TRIM(tenant_id)=tid),
    so a doc owned by another tenant resolves to (None, ...) -> caller 404s.
    """
    conn = psycopg2.connect(**_conn_kwargs())
    try:
        with conn.cursor() as cur:
            if corpus == "ediscovery":
                abs_path, doc_type, _ = _resolve_ediscovery_file(cur, tenant_id, doc_id)
            elif corpus == "dms":
                abs_path, doc_type, _ = _resolve_dms_file(cur, tenant_id, doc_id)
            else:
                return None, None, None
    finally:
        conn.close()
    if not abs_path or not os.path.exists(abs_path):
        return None, doc_type, None
    st = os.stat(abs_path)
    version = "%x-%x" % (int(st.st_mtime), st.st_size)
    return abs_path, doc_type, version


def _version_dir(tenant_id, corpus, doc_id, rendition, version) -> str:
    return os.path.join(CACHE_ROOT, tenant_id.strip(), corpus,
                        str(doc_id), rendition, version)


def _cache_path(tenant_id, corpus, doc_id, rendition, version, page, width) -> str:
    return os.path.join(
        _version_dir(tenant_id, corpus, doc_id, rendition, version),
        "p%d_w%d.webp" % (page, width),
    )


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _read_meta(vdir: str) -> Optional[int]:
    try:
        with open(os.path.join(vdir, "meta.txt")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_meta(vdir: str, page_count: int) -> None:
    try:
        _atomic_write(os.path.join(vdir, "meta.txt"), str(page_count).encode())
    except OSError:
        pass


def _webp_dims(data: bytes) -> Tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.width, im.height



def _under_cache_root(path) -> bool:
    rp = os.path.realpath(path)
    root = os.path.realpath(CACHE_ROOT)
    return rp == root or rp.startswith(root + os.sep)


def _rmtree_files(d) -> int:
    """Remove a cache subtree (files then dirs); returns files removed."""
    n = 0
    for root, _dirs, files in os.walk(d, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(root, f)); n += 1
            except OSError:
                pass
        try:
            os.rmdir(root)
        except OSError:
            pass
    return n


def prune_stale_versions(tenant_id, corpus, doc_id, rendition, keep_version) -> int:
    """Drop cached renders for OLDER versions of this doc (the source changed ->
    new version key). Bounds per-doc growth on re-OCR / re-production. Only ever
    touches dirs under this doc's rendition dir, keeping keep_version."""
    rdir = os.path.join(CACHE_ROOT, tenant_id.strip(), corpus, str(doc_id), rendition)
    if not _under_cache_root(rdir) or not os.path.isdir(rdir):
        return 0
    removed = 0
    try:
        for name in os.listdir(rdir):
            if name == keep_version:
                continue
            vdir = os.path.join(rdir, name)
            if os.path.isdir(vdir) and _under_cache_root(vdir):
                removed += _rmtree_files(vdir)
    except OSError:
        pass
    return removed


def gc_sweep(max_bytes=None) -> dict:
    """Size-budgeted LRU eviction across the whole cache, by access time. Deletes
    the least-recently-accessed files until total <= max_bytes, then prunes empty
    dirs. The cache is a regenerable derivative, so eviction is always safe."""
    if max_bytes is None:
        max_bytes = CACHE_MAX_BYTES
    max_bytes = max(int(max_bytes), GC_MIN_BUDGET_BYTES)
    root = os.path.realpath(CACHE_ROOT)
    if not os.path.isdir(root):
        return {"total_bytes": 0, "max_bytes": max_bytes, "deleted_files": 0,
                "freed_bytes": 0, "total_after": 0}
    entries, total = [], 0
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            fp = os.path.join(dp, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            total += st.st_size
            entries.append((st.st_atime, st.st_size, fp))
    freed = deleted = 0
    if total > max_bytes:
        entries.sort(key=lambda e: e[0])  # oldest access first
        need = total - max_bytes
        for _atime, size, fp in entries:
            if freed >= need:
                break
            try:
                os.remove(fp); freed += size; deleted += 1
            except OSError:
                pass
        for dp, _dns, _fns in os.walk(root, topdown=False):
            if os.path.realpath(dp) != root:
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
    return {"total_bytes": total, "max_bytes": max_bytes, "deleted_files": deleted,
            "freed_bytes": freed, "total_after": total - freed}


def render_page(tenant_id: str, corpus: str, doc_id: str, page: int,
                rendition: str = "native_pdf",
                width: int = DEFAULT_WIDTH) -> Optional[RasterResult]:
    """Render (or serve cached) one page as WebP. SYNC -- call via run_in_threadpool.

    Returns RasterResult, or None if the document/file can't be resolved (-> 404).
    Raises ValueError for an out-of-range page (caller -> 400/404).
    """
    if corpus not in SUPPORTED_CORPORA:
        return None
    width = clamp_width(width)
    abs_path, doc_type, version = _resolve_source(tenant_id, corpus, doc_id)
    if not abs_path or not version:
        return None

    vdir = _version_dir(tenant_id, corpus, doc_id, rendition, version)
    cache_fp = _cache_path(tenant_id, corpus, doc_id, rendition, version, page, width)

    if os.path.isfile(cache_fp):
        with open(cache_fp, "rb") as f:
            data = f.read()
        w, h = _webp_dims(data)
        return RasterResult(data, w, h, page, _read_meta(vdir), True)

    doc = fitz.open(abs_path)
    try:
        page_count = doc.page_count
        if page < 1 or page > page_count:
            raise ValueError("page %d out of range 1..%d" % (page, page_count))
        pg = doc.load_page(page - 1)
        rect = pg.rect
        zoom = (width / rect.width) if rect.width else 1.0
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                            colorspace=fitz.csRGB, alpha=False)
        from PIL import Image
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=4)
        data = buf.getvalue()
        w, h = pix.width, pix.height
    finally:
        doc.close()

    _atomic_write(cache_fp, data)
    _write_meta(vdir, page_count)
    try:
        prune_stale_versions(tenant_id, corpus, doc_id, rendition, version)
    except Exception:
        pass
    return RasterResult(data, w, h, page, page_count, False)
