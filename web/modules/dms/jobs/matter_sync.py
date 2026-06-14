"""
modules/dms/jobs/matter_sync.py

Matter-scoped file sync with two-tier parallelism:

  Tier 1 — COORDINATOR (sync_matter_files):
    Seeds folder structure, enumerates accepted matter_folders mappings,
    fans out one sync_mapping_files sub-job per mapping onto the 'migration'
    queue. Records parent job_id in Redis so status polling can aggregate.

  Tier 2 — WORKER (sync_mapping_files):
    Copies one mapping's files using a ThreadPoolExecutor with 4 threads.
    Each thread uses its own psycopg2 connection — no shared-conn contention.
    Writes incremental stats to a Redis hash keyed by parent job_id.

Queue name: 'migration' — ONLY PROC-01 workers consume this queue. Web-side
workers must NOT register 'migration' in their queue list. PROC-01 should
have 3-4 workers listening on 'migration' (adjust docker-compose replica
count or --queue flags accordingly).

    from rq import Queue
    from redis import Redis
    q = Queue("migration", connection=Redis.from_url(REDIS_URL))
    job = q.enqueue(
        "modules.dms.jobs.matter_sync.sync_matter_files",
        tenant_id, matter_id,
        job_timeout=3600,
    )

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rq import Queue, get_current_job
from redis import Redis

from modules.dms.jobs.folder_seeder import (
    seed_matter_folders,
    _matter_dest_path,
    PROC01_HOSTNAMES,
)

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Parallelism knob — threads per mapping worker. 4 is a good balance for
# CIFS read latency without overwhelming the DB connection pool.
SYNC_THREAD_POOL_SIZE = int(os.environ.get("MATTER_SYNC_THREADS", "4"))

# Redis TTL for parent-job stats aggregation (24 hours)
PARENT_STATS_TTL_SECONDS = 86400

# Mount roots
CLIENTS_ROOT    = Path(os.environ.get("CIFS_CLIENTS_MOUNT",    "/mnt/clients"))
DOCSEND_ROOT    = Path(os.environ.get("CIFS_DOCSEND_MOUNT",    "/mnt/docsend"))
PRAESIDIUM_ROOT = Path(os.environ.get("CIFS_PRAESIDIUM_MOUNT", "/mnt/praesidium"))

DEFAULT_SUPPORTED_EXTENSIONS = {
    ".docx", ".doc", ".pdf", ".xlsx", ".xls",
    ".msg", ".eml", ".jpg", ".jpeg", ".png",
    ".tiff", ".tif", ".txt", ".rtf", ".csv",
}
OCR_EXTENSIONS = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}

# ─── Legacy folder name → standard tree mapping (Option A) ───────────────────
#
# When copying from legacy shares (/mnt/clients, /mnt/docsend), the source
# matter's top-level folders often use variable naming conventions accumulated
# over 24 years of practice ("Pleadings and Motion Practice", "Discovery
# Requests", "Work Product"). The sync redirects recognized legacy folder
# names into their standard-tree equivalents so the destination tree comes
# out clean.
#
# Match is on the FIRST PATH COMPONENT only (case-insensitive, whitespace
# normalized). If the first component doesn't match any known alias, the
# original folder name is preserved. Deeper subfolder structure is always
# preserved under the mapped (or unmapped) top-level folder.
#
# Example:
#   Source: /mnt/clients/Williams/AA Trading/Pleadings and Motion Practice/MSJ.pdf
#   Dest:   /mnt/praesidium/hjmm-prod/matters/Williams/AA Trading/02-Pleadings/MSJ.pdf
#
# Override via env var LEGACY_FOLDER_MAP_JSON if a tenant needs a different
# mapping (JSON object, lowercase keys).

_DEFAULT_LEGACY_FOLDER_MAP = {
    # Client-provided materials
    "client documents":             "01-Client Documents",
    "client docs":                  "01-Client Documents",
    "client materials":             "01-Client Documents",
    "client":                       "01-Client Documents",
    "intake":                       "01-Client Documents",
    "engagement":                   "01-Client Documents",

    # Pleadings — variations
    "pleadings":                    "02-Pleadings",
    "pleadings and motion practice": "02-Pleadings",
    "pleadings and motions":        "02-Pleadings",
    "motions":                      "02-Pleadings",
    "motion practice":              "02-Pleadings",

    # Discovery — variations
    "discovery":                    "03-Discovery",
    "discovery requests":           "03-Discovery",
    "discovery responses":          "03-Discovery",
    "discovery requests and responses": "03-Discovery",

    # Correspondence — opposing counsel / third party / court
    "correspondence":               "04-Correspondence",
    "letters":                      "04-Correspondence",
    "emails":                       "04-Correspondence",
    "email":                        "04-Correspondence",

    # Research
    "research":                     "05-Research",
    "legal research":               "05-Research",

    # Court filings
    "court filings":                "06-Court Filings",
    "filings":                      "06-Court Filings",
    "filed documents":              "06-Court Filings",
    "court":                        "06-Court Filings",

    # Depositions
    "depositions":                  "07-Depositions",
    "depos":                        "07-Depositions",
    "deposition transcripts":       "07-Depositions",
    "transcripts":                  "07-Depositions",

    # Experts
    "experts":                      "08-Experts",
    "expert":                       "08-Experts",
    "expert reports":               "08-Experts",

    # Mediation / Settlement
    "mediation":                    "09-Mediation",
    "settlement":                   "09-Mediation",
    "settlement documents":         "09-Mediation",

    # Orders
    "orders":                       "10-Orders",
    "court orders":                 "10-Orders",

    # Working docs / work product
    "working docs":                 "11-Working Docs",
    "work product":                 "11-Working Docs",
    "drafts":                       "11-Working Docs",
    "working drafts":               "11-Working Docs",

    # eDiscovery / productions
    "ediscovery":                   "12-eDiscovery",
    "e-discovery":                  "12-eDiscovery",
    "document production":          "12-eDiscovery",
    "document productions":         "12-eDiscovery",
    "productions":                  "12-eDiscovery",
    "production":                   "12-eDiscovery",
    "productions received":         "12-eDiscovery",
    "produced documents":           "12-eDiscovery",

    # Billing
    "billing":                      "13-Billing",
    "invoices":                     "13-Billing",
    "bills":                        "13-Billing",

    # Trial preparation (includes common typo variants seen in legacy data)
    "trial preparation":            "14-Trial Preparation",
    "trial prep":                   "14-Trial Preparation",
    "trial prepartion":             "14-Trial Preparation",  # known typo
    "trial preparations":           "14-Trial Preparation",
    "trial":                        "14-Trial Preparation",
    "exhibits":                     "14-Trial Preparation",
    "trial exhibits":               "14-Trial Preparation",
}


def _load_legacy_folder_map() -> dict:
    """Load the legacy folder mapping, optionally overridden by an env-var
    JSON payload. Keys are normalized lowercase, values are kept verbatim."""
    override = os.environ.get("LEGACY_FOLDER_MAP_JSON", "").strip()
    if override:
        try:
            import json as _json
            data = _json.loads(override)
            if isinstance(data, dict):
                return {k.strip().lower(): v for k, v in data.items()}
        except Exception as exc:
            log.warning("matter_sync: LEGACY_FOLDER_MAP_JSON parse failed: %s", exc)
    return _DEFAULT_LEGACY_FOLDER_MAP


LEGACY_FOLDER_MAP = _load_legacy_folder_map()


# REMAP_PREFIX_V1 — prefix matching for cause-suffixed legacy folder names.
#
# Exact alias matching misses folders like "Pleadings Tarrant County
# 017-361909-25" or "Pleading Hood County C2024348", leaving them stranded at
# the matter root. When the first path component STARTS WITH one of these keys
# (followed by a space or dash), it maps into the standard tree and the
# remainder is preserved as a subfolder:
#   "Pleadings Tarrant County 017-361909-25"
#       -> "02-Pleadings/Tarrant County 017-361909-25"
# Keys are deliberately conservative — generic words (client, email, letters,
# research, court) are excluded to avoid false positives. Checked in list
# order (longest/most-specific first).
LEGACY_FOLDER_PREFIX_MAP = [
    ("pleadings and motion practice", "02-Pleadings"),
    ("pleadings and motions",         "02-Pleadings"),
    ("document productions",          "12-eDiscovery"),
    ("document production",           "12-eDiscovery"),
    ("productions received",          "12-eDiscovery"),
    ("discovery responses",           "03-Discovery"),
    ("discovery requests",            "03-Discovery"),
    ("motion practice",               "02-Pleadings"),
    ("court filings",                 "06-Court Filings"),
    ("working docs",                  "11-Working Docs"),
    ("work product",                  "11-Working Docs"),
    ("depositions",                   "07-Depositions"),
    ("deposition",                    "07-Depositions"),
    ("productions",                   "12-eDiscovery"),
    ("production",                    "12-eDiscovery"),
    ("settlement",                    "09-Mediation"),
    ("pleadings",                     "02-Pleadings"),
    ("pleading",                      "02-Pleadings"),
    ("discovery",                     "03-Discovery"),
    ("mediation",                     "09-Mediation"),
    ("correspondence",                "04-Correspondence"),
    ("invoices",                      "13-Billing"),
    ("experts",                       "08-Experts"),
    ("billing",                       "13-Billing"),
    ("motions",                       "02-Pleadings"),
    ("expert",                        "08-Experts"),
    ("orders",                        "10-Orders"),
    ("drafts",                        "11-Working Docs"),
    ("trial",                         "14-Trial Preparation"),
]


def _match_legacy_component(component: str):
    """Resolve one top-level legacy folder name to (mapped_target, remainder).

    Exact alias match -> (target, ""). Prefix match (key followed by a space
    or dash) -> (target, remainder with original casing). No match ->
    (None, "")."""
    norm_orig = " ".join((component or "").strip().split())
    low = norm_orig.lower()
    if not low:
        return None, ""
    exact = LEGACY_FOLDER_MAP.get(low)
    if exact:
        return exact, ""
    for key, target in LEGACY_FOLDER_PREFIX_MAP:
        if low.startswith(key + " ") or low.startswith(key + "-"):
            remainder = norm_orig[len(key):].strip(" -\u2013\u2014").strip()
            return target, remainder
    return None, ""


# ─── C1 onboarding-cluster exclusion fixture ──────────────────────────────────
# Productions, client documents, and mailstores are NOT DMS working files.
# They are partitioned out of matter_sync and recorded as onboarding_clusters
# rows — the skip record, the exclusions-UI data source, and the observation
# substrate for the AI-guided onboarding session (Reasoning Contract v1.0).
# Detection: top-folder alias map (cheap) + load-file sniff (ground truth:
# a folder containing a load file IS a production regardless of its name)
# + mailstore/forensic extensions anywhere.
# Invariant per mapping: kept + sum(cluster.file_count) == enumerated.
import re as _onb_re
import json as _onb_json

LOAD_FILE_EXTS = {".dat", ".opt", ".lfp", ".dii"}
MAILSTORE_EXTS = {".pst", ".ost"}
FORENSIC_EXTS = {".e01", ".ex01", ".l01", ".ad1"}
EXCLUDED_DEST_FOLDERS = {"12-eDiscovery", "01-Client Documents"}
_BATES_RE = _onb_re.compile(r"^(?!IMG|DSC|PXL|MVI|SCAN|PIC)[A-Za-z]{2,8}[ _-]?\d{4,}", _onb_re.IGNORECASE)


def _looks_like_load_file(path) -> bool:
    """Cheap CSV sniff: Concordance thorn/DC4 delimiters or Bates header."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2048)
        low = head.lower()
        # Concordance: thorn (0xFE) as field wrapper appears repeatedly per
        # line — require >=4 occurrences past any UTF-16 BOM so a BOM alone
        # (FF FE / FE FF) never classifies an ordinary CSV as a load file.
        body = head[2:] if head[:2] in (b"\xff\xfe", b"\xfe\xff") else head
        return (body.count(b"\xfe") >= 4 or body.count(b"\x14") >= 4
                or body.count(b"\xc3\xbe") >= 4
                or b"begbates" in low or b"beg bates" in low
                or b"bates_begin" in low or b"begdoc" in low)
    except Exception:
        return False


def _partition_onboarding_clusters(source_dir, files):
    """Partition enumerated files into (kept_for_dms, clusters)."""
    clusters = {}

    def _get(root, ctype, how, prefix=True):
        key = str(root)
        c = clusters.get(key)
        if c is None:
            c = {"root_path": key, "cluster_type": ctype, "detected_by": how,
                 "file_count": 0, "total_bytes": 0, "prefix": prefix,
                 "evidence": {"load_files": [], "ext_histogram": {},
                              "sample_files": [], "bates_sample": None}}
            clusters[key] = c
        return c

    # (a) alias top-level folders
    try:
        children = [c for c in source_dir.iterdir() if c.is_dir()]
    except Exception:
        children = []
    for child in children:
        mapped, _rem = _match_legacy_component(child.name)  # REMAP_PREFIX_V1
        if mapped in EXCLUDED_DEST_FOLDERS:
            ctype = "client_documents" if mapped.startswith("01-") else "production"
            _get(child, ctype, "alias")

    # (b) load-file sniff at any depth
    prefix_roots = [Path(k) for k, c in clusters.items() if c.get("prefix")]
    for f in files:
        ext = f.suffix.lower()
        if ext in LOAD_FILE_EXTS or (ext == ".csv" and _looks_like_load_file(f)):
            covered = False
            for rp in prefix_roots:
                try:
                    f.relative_to(rp)
                    covered = True
                    break
                except ValueError:
                    pass
            if not covered:
                _get(f.parent, "production", "load_file_sniff")
                prefix_roots.append(f.parent)

    # (c) assign every enumerated file exactly once
    kept = []
    for f in files:
        ext = f.suffix.lower()
        owner = None
        for rp in prefix_roots:
            try:
                f.relative_to(rp)
                owner = clusters[str(rp)]
                break
            except ValueError:
                pass
        if owner is None and ext in MAILSTORE_EXTS:
            owner = _get(f.parent, "pst_mailstore", "mailstore", prefix=False)
        elif owner is None and ext in FORENSIC_EXTS:
            owner = _get(f.parent, "forensic_image", "mailstore", prefix=False)
        if owner is None:
            kept.append(f)
            continue
        try:
            sz = f.stat().st_size
        except OSError:
            sz = 0
        owner["file_count"] += 1
        owner["total_bytes"] += sz
        ev = owner["evidence"]
        key = ext or "(none)"
        ev["ext_histogram"][key] = ev["ext_histogram"].get(key, 0) + 1
        if ext in LOAD_FILE_EXTS and len(ev["load_files"]) < 10:
            ev["load_files"].append(f.name)
        elif len(ev["sample_files"]) < 5:
            ev["sample_files"].append(f.name)
        if ev["bates_sample"] is None and _BATES_RE.match(f.stem):
            ev["bates_sample"] = f.name
    clusters = {k: c for k, c in clusters.items() if c["file_count"] > 0}
    return kept, clusters


def _upsert_onboarding_clusters(tid, mid, clusters):
    """One row per cluster. Never clobbers status/collection_id on re-sync."""
    if not clusters:
        return
    try:
        conn = _get_db_conn()
        conn.autocommit = True
        cur = conn.cursor()
        for c in clusters.values():
            cur.execute(
                """INSERT INTO onboarding_clusters
                       (tenant_id, matter_id, root_path, cluster_type,
                        detected_by, evidence, file_count, total_bytes)
                   VALUES (%s, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (tenant_id, root_path) DO UPDATE
                   SET matter_id = EXCLUDED.matter_id,
                       cluster_type = EXCLUDED.cluster_type,
                       detected_by = EXCLUDED.detected_by,
                       evidence = EXCLUDED.evidence,
                       file_count = EXCLUDED.file_count,
                       total_bytes = EXCLUDED.total_bytes,
                       updated_at = NOW()""",
                (tid, mid, c["root_path"], c["cluster_type"], c["detected_by"],
                 _onb_json.dumps(c["evidence"]), c["file_count"], c["total_bytes"]),
            )
        cur.close()
        conn.close()
    except Exception as exc:
        log.error("onboarding_clusters upsert failed: %s", exc)
# ─── end C1 fixture ───────────────────────────────────────────────────────────


def _remap_legacy_folder(rel_path: str) -> str:
    """Given a source-relative path, return a new relative path where the
    first component has been mapped to its standard-tree equivalent if a
    mapping exists. Otherwise returns the original path unchanged.

    Case-insensitive matching on the first path segment only. Deeper
    subdirectories are preserved verbatim. Loose files at the source root
    (no leading folder) are also preserved — they stay at the matter root."""
    if not rel_path:
        return rel_path
    rel_path = rel_path.replace("\\", "/")
    parts = rel_path.split("/", 1)
    mapped, remainder = _match_legacy_component(parts[0])
    if not mapped:
        return rel_path
    head = f"{mapped}/{remainder}" if remainder else mapped
    if len(parts) == 1:
        # The source-relative path IS a bare folder — unlikely for files but handle it
        return head
    return f"{head}/{parts[1]}"

# Path tokens that, when present anywhere in the source path (case-insensitive),
# mark the file with ocr_status='skipped_production' instead of queuing OCR.
# Tokens are substring-matched against the full source file path, so a token
# like "document production" matches any folder or parent folder containing
# that string. Override via env var OCR_SKIP_PATH_TOKENS (comma-separated).
OCR_SKIP_PATH_TOKENS = [
    t.strip().lower()
    for t in os.environ.get(
        "OCR_SKIP_PATH_TOKENS",
        "document production,productions received,produced documents,relativity export",
    ).split(",")
    if t.strip()
]


def _should_skip_ocr_by_path(src_path: Path) -> bool:
    """True if any OCR_SKIP_PATH_TOKENS substring appears in the source path.
    Case-insensitive. Use for Relativity productions and similar already-OCR'd
    exports where re-OCR is wasteful and can degrade text quality."""
    if not OCR_SKIP_PATH_TOKENS:
        return False
    low = str(src_path).lower()
    return any(tok in low for tok in OCR_SKIP_PATH_TOKENS)


# ─── Host enforcement ────────────────────────────────────────────────────────

def _assert_proc01_or_raise() -> None:
    """On appliance, all mounts are shared. Skip hostname check."""
    pass


# ─── DB helper ───────────────────────────────────────────────────────────────

def _get_db_conn():
    """psycopg2 connection using rfind('@') to handle @ in password."""
    import psycopg2
    raw = os.environ.get("DATABASE_URL", "")
    url = raw.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(
        host=host, port=int(port), dbname=dbname, user=user, password=password,
    )


# ─── Source path resolution (FIX for disk_root destination-path bug) ─────────
#
# After the matter_folders.disk_root normalization, disk_root contains the
# DESTINATION path (/mnt/praesidium/...), not the source. The actual legacy
# source path is stored in matters.folder_path as a share-relative path like
# "Rhod Williams/SummitBridge" or "American Savings/2625 E. Carson Street (CA)".
#
# _resolve_source_dir() takes that legacy relative path and finds the actual
# directory on the worker's CIFS mounts. It tries /mnt/clients first, then
# /mnt/docsend. Returns the Path if found, None if neither exists.

def _resolve_source_dir(legacy_folder_path: str) -> Path | None:
    """Resolve a matters.folder_path value to the actual source directory
    on the worker's CIFS mounts.

    Args:
        legacy_folder_path: Share-relative path from matters.folder_path,
            e.g. "Rhod Williams/SummitBridge" or "American Savings/2625 E. Carson Street (CA)"

    Returns:
        Path to the source directory, or None if not found on any mount.
    """
    if not legacy_folder_path:
        return None

    # Normalize: forward slashes, strip leading/trailing
    fp = legacy_folder_path.replace("\\", "/").strip().strip("/")
    if not fp:
        return None

    # Try /mnt/clients first (primary share), then /mnt/docsend
    for mount_root in (CLIENTS_ROOT, DOCSEND_ROOT):
        candidate = mount_root / fp
        if candidate.exists() and candidate.is_dir():
            return candidate

    # Not found on either mount
    return None


def _resolve_source_root(disk_root: str):
    """DEPRECATED — kept for backward compatibility with any callers that
    still pass disk_root values. New code should use _resolve_source_dir()
    with matters.folder_path instead.

    Translate a matter_folders.disk_root value into the actual Linux mount
    path on PROC-01."""
    if not disk_root:
        return CLIENTS_ROOT  # historical default for empty values
    # Normalize: lowercase, forward-slashes, strip drive letters, strip leading/trailing slashes
    dr = disk_root.replace("\\", "/").strip().lower()
    # Strip Windows drive letter (e.g. 'd:/public/clients' → 'public/clients')
    if len(dr) >= 2 and dr[1] == ":":
        dr = dr[2:]
    dr = dr.strip("/")

    # Clients share — matches 'clients', 'public/clients', or any path ending in 'clients'
    if dr == "clients" or dr.endswith("/clients") or "clients" in dr.split("/"):
        return CLIENTS_ROOT
    # Docsend share
    if dr == "docsend" or dr.endswith("/docsend") or "docsend" in dr.split("/"):
        return DOCSEND_ROOT
    # Absolute Linux path — pass through if it exists
    if disk_root.startswith("/"):
        p = Path(disk_root)
        return p if p.exists() else None
    return None


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Redis-backed stats aggregation ──────────────────────────────────────────
#
# Parent job records: matter_sync:parent:{job_id} → hash
#   tenant_id, matter_id, status, mapping_count, started_at,
#   seeded, existing_folders
# Per-mapping stats: matter_sync:mapping:{parent_job_id}:{mapping_idx} → hash
#   copied, skipped_existing, skipped_unsupported, errors, ocr_queued,
#   total_bytes, status, started_at, ended_at

class _DummyRedis:
    """Fallback when Redis is unavailable — silent no-ops for status tracking."""
    def hset(self, *a, **kw): pass
    def hincrby(self, *a, **kw): pass
    def hget(self, *a, **kw): return None
    def hgetall(self, *a, **kw): return {}
    def expire(self, *a, **kw): pass
    def exists(self, *a, **kw): return False

def _redis_client():
    try:
        r = Redis.from_url(REDIS_URL, socket_timeout=2)
        r.ping()
        return r
    except Exception:
        return _DummyRedis()


def _parent_key(parent_job_id: str) -> str:
    return f"matter_sync:parent:{parent_job_id}"


def _mapping_key(parent_job_id: str, mapping_idx: int) -> str:
    return f"matter_sync:mapping:{parent_job_id}:{mapping_idx}"


def _increment_mapping_stats(r: Redis, key: str, deltas: dict) -> None:
    """Atomically increment integer counters on a mapping's stats hash."""
    pipe = r.pipeline()
    for field, delta in deltas.items():
        if delta:
            pipe.hincrby(key, field, int(delta))
    pipe.expire(key, PARENT_STATS_TTL_SECONDS)
    pipe.execute()


# ─── Coordinator: one RQ job per matter ─────────────────────────────────────

def sync_matter_files(tenant_id: str, matter_id: str) -> dict:
    """Coordinator. Seeds folders, fans mappings out as sub-jobs, returns
    descriptor pointing at Redis-tracked progress.

    Runs on the 'migration' queue (PROC-01 only). Each mapping becomes its own
    sub-job on 'migration', so with 3-4 PROC-01 workers they run in parallel
    across mappings. Each sub-job additionally uses a 4-thread pool internally.
    """
    _assert_proc01_or_raise()

    tid = (tenant_id or "").strip()
    mid = (matter_id or "").strip()
    if not tid or not mid:
        return {"error": "tenant_id and matter_id required"}

    parent_job = get_current_job()
    parent_job_id = parent_job.id if parent_job else f"local-{uuid.uuid4()}"

    log.info(
        "matter_sync coordinator start tenant=%s matter=%s job=%s host=%s threads_per_worker=%d",
        tid, mid, parent_job_id, socket.gethostname(), SYNC_THREAD_POOL_SIZE,
    )

    r = _redis_client()

    # Initialize parent record in Redis
    r.hset(_parent_key(parent_job_id), mapping={
        "tenant_id": tid,
        "matter_id": mid,
        "status": "seeding",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mapping_count": "0",
        "seeded": "0",
        "existing_folders": "0",
    })
    r.expire(_parent_key(parent_job_id), PARENT_STATS_TTL_SECONDS)

    # ── Step 1: seed folder structure ───────────────────────────────────────
    try:
        seed_result = seed_matter_folders(tid, mid)
        r.hset(_parent_key(parent_job_id), mapping={
            "seeded": str(seed_result.get("created", 0)),
            "existing_folders": str(seed_result.get("existing", 0)),
            "matter_type_resolved": seed_result.get("matter_type_resolved") or "",
        })
    except Exception as exc:
        log.error("coordinator seed failed tenant=%s matter=%s: %s", tid, mid, exc)
        r.hset(_parent_key(parent_job_id), "seed_error", str(exc))

    # ── Step 2: enumerate accepted mappings ─────────────────────────────────
    # FIX: Join matters.folder_path to get the legacy source path.
    # matter_folders.disk_root is the DESTINATION (after normalization),
    # matters.folder_path is the legacy CIFS-relative SOURCE path.
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT mf.folder_path, mf.disk_root, m.folder_path AS legacy_source_path
               FROM matter_folders mf
               JOIN matters m
                 ON m.id = mf.matter_id
                AND TRIM(m.tenant_id) = TRIM(mf.tenant_id)
               WHERE TRIM(mf.tenant_id) = %s
                 AND mf.matter_id = %s::uuid
                 AND mf.disk_root IS NOT NULL
               ORDER BY mf.folder_path""",
            (tid, mid),
        )
        mappings = cur.fetchall()
    finally:
        conn.close()

    r.hset(_parent_key(parent_job_id), "mapping_count", str(len(mappings)))

    if not mappings:
        r.hset(_parent_key(parent_job_id), mapping={
            "status": "finished",
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "message": "Folder tree seeded. No accepted mappings — no files to copy.",
        })
        return {
            "parent_job_id": parent_job_id,
            "tenant_id": tid,
            "matter_id": mid,
            "mapping_count": 0,
            "message": "Folder tree seeded. No accepted mappings.",
        }

    # ── Step 3: fan out one sub-job per mapping onto 'migration' queue ──────
    q = Queue("migration", connection=r)
    sub_jobs = []
    for idx, (folder_path, disk_root, legacy_source_path) in enumerate(mappings):
        # Initialize this mapping's stats hash
        r.hset(_mapping_key(parent_job_id, idx), mapping={
            "folder_path": folder_path or "",
            "disk_root": disk_root or "",
            "legacy_source_path": legacy_source_path or "",
            "status": "queued",
            "copied": "0",
            "skipped_existing": "0",
            "skipped_unsupported": "0",
            "errors": "0",
            "ocr_queued": "0",
            "total_bytes": "0",
        })
        r.expire(_mapping_key(parent_job_id, idx), PARENT_STATS_TTL_SECONDS)

        # FIX: Pass legacy_source_path to the worker so it can find the
        # actual source directory on /mnt/clients or /mnt/docsend.
        sub = q.enqueue(
            "modules.dms.jobs.matter_sync.sync_mapping_files",
            tid, mid, folder_path, disk_root,
            parent_job_id, idx,
            legacy_source_path or "",
            job_timeout=3600,
            result_ttl=PARENT_STATS_TTL_SECONDS,
        )
        sub_jobs.append(sub.id)
        r.hset(_mapping_key(parent_job_id, idx), "sub_job_id", sub.id)

    r.hset(_parent_key(parent_job_id), mapping={
        "status": "running",
        "sub_job_ids": json.dumps(sub_jobs),
    })

    log.info(
        "matter_sync coordinator fanned %d mappings for matter=%s parent_job=%s",
        len(mappings), mid, parent_job_id,
    )

    return {
        "parent_job_id": parent_job_id,
        "tenant_id": tid,
        "matter_id": mid,
        "mapping_count": len(mappings),
        "sub_job_ids": sub_jobs,
        "message": f"{len(mappings)} mapping(s) queued for parallel sync.",
    }


# ─── Worker: one RQ job per mapping, 4 threads internally ───────────────────

def sync_mapping_files(
    tenant_id: str,
    matter_id: str,
    folder_path: str,
    disk_root: str,
    parent_job_id: str,
    mapping_idx: int,
    legacy_source_path: str = "",
    skip_remap: bool = False,
) -> dict:
    """Sync a single mapping. Runs on 'migration' queue on PROC-01. Uses a
    ThreadPoolExecutor to parallelize hash+copy+index across files within the
    mapping. Each thread opens its own psycopg2 connection.

    Args:
        legacy_source_path: The matters.folder_path value — a CIFS-relative
            path like "Rhod Williams/SummitBridge". This is the PRIMARY source
            for locating files on legacy mounts. If empty, falls back to the
            old _resolve_source_root(disk_root) logic for backward compat.
    """
    _assert_proc01_or_raise()

    tid = tenant_id.strip()
    mid = matter_id.strip()
    r = _redis_client()
    mkey = _mapping_key(parent_job_id, mapping_idx)

    r.hset(mkey, mapping={
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    # ── FIX: Resolve source directory using legacy_source_path first ────────
    # legacy_source_path = matters.folder_path (e.g. "Rhod Williams/SummitBridge")
    # This is the correct path to the files on /mnt/clients or /mnt/docsend.
    # disk_root = matter_folders.disk_root (e.g. "/mnt/praesidium/...") = DESTINATION.
    source_dir = None
    source_resolution_method = None

    if legacy_source_path and legacy_source_path.strip():
        source_dir = _resolve_source_dir(legacy_source_path.strip())
        if source_dir:
            source_resolution_method = "legacy_source_path"
            log.info(
                "sync_mapping %s: resolved source via legacy_source_path=%r → %s",
                mkey, legacy_source_path, source_dir,
            )

    # Fallback: try old _resolve_source_root logic for backward compat
    # (only relevant if legacy_source_path was empty AND disk_root happens
    # to point at something resolvable — unlikely after normalization, but
    # keeps existing synced matters working if re-run)
    if source_dir is None:
        source_root = _resolve_source_root(disk_root)
        if source_root is not None and source_root.exists():
            # Old path: folder_path from matter_folders was used as subdirectory
            fp_clean = (folder_path or "").replace("\\", "/").strip().strip("/")
            for prefix in ("clients/", "docsend/"):
                if fp_clean.lower().startswith(prefix):
                    fp_clean = fp_clean[len(prefix):]
                    break
            source_dir = source_root / fp_clean if fp_clean else source_root
            if source_dir.exists() and source_dir.is_dir():
                source_resolution_method = "disk_root_fallback"
                log.info(
                    "sync_mapping %s: resolved source via disk_root fallback → %s",
                    mkey, source_dir,
                )
            else:
                source_dir = None

    if source_dir is None or not source_dir.exists():
        msg = (
            f"source dir not found: legacy_source_path={legacy_source_path!r}, "
            f"disk_root={disk_root!r}. Checked /mnt/clients and /mnt/docsend."
        )
        log.warning("sync_mapping %s: %s", mkey, msg)
        r.hset(mkey, mapping={
            "status": "failed",
            "error": msg,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "failed", "error": msg}

    r.hset(mkey, mapping={
        "source_dir": str(source_dir),
        "source_resolution": source_resolution_method or "unknown",
    })

    # Resolve human-readable destination path from client + matter names.
    # Falls back to matter_id UUID leaf only if client_name is missing.
    try:
        lookup_conn = _get_db_conn()
        try:
            lc = lookup_conn.cursor()
            lc.execute(
                """SELECT m.matter_name, m.matter_number, c.client_name
                     FROM matters m
                     LEFT JOIN clients c ON c.id = m.client_id
                    WHERE m.id = %s::uuid AND TRIM(m.tenant_id) = %s""",
                (mid, tid),
            )
            row = lc.fetchone()
        finally:
            lookup_conn.close()
    except Exception as exc:
        log.error("sync_mapping %s: client/matter name lookup failed: %s", mkey, exc)
        row = None

    if row and row[2]:  # client_name present
        matter_name, matter_number, client_name = row
        matter_dest = _matter_dest_path(tid, client_name, matter_name or "", matter_number)
    else:
        log.warning(
            "sync_mapping %s: no client_name — falling back to UUID path for matter=%s",
            mkey, mid,
        )
        matter_dest = PRAESIDIUM_ROOT / tid / "matters" / mid

    # Ensure destination tree exists (seed may have been skipped earlier).
    try:
        matter_dest.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("sync_mapping %s: cannot create matter_dest %s: %s", mkey, matter_dest, exc)

    # OCR queue — shared across threads; RQ Queue + Redis client are thread-safe
    try:
        ocr_queue = Queue("ocr", connection=r)
    except Exception as exc:
        log.warning("sync_mapping %s OCR queue unavailable: %s", mkey, exc)
        ocr_queue = None

    # Enumerate files up-front so threads can chew through the list
    files_enumerated = [p for p in source_dir.rglob("*") if p.is_file()]
    files_to_process, _onb_clusters = _partition_onboarding_clusters(source_dir, files_enumerated)
    _upsert_onboarding_clusters(tid, mid, _onb_clusters)
    _excl_files = sum(c["file_count"] for c in _onb_clusters.values())
    r.hset(mkey, mapping={
        "enumerated_files": len(files_enumerated),
        "excluded_files": _excl_files,
        "excluded_bytes": sum(c["total_bytes"] for c in _onb_clusters.values()),
        "cluster_count": len(_onb_clusters),
    })
    log.info("sync_mapping %s: %d enumerated -> %d to process, %d excluded in %d clusters (%s)",
             mkey, len(files_enumerated), len(files_to_process), _excl_files,
             len(_onb_clusters), source_dir)

    # Progress-flush throttling — one Redis write per ~25 files per thread
    local = threading.local()

    def _flush_stats(force: bool = False):
        """Flush thread-local stats to Redis. Throttled unless force=True."""
        if not hasattr(local, "buf"):
            return
        buf = local.buf
        counter = getattr(local, "flush_counter", 0) + 1
        local.flush_counter = counter
        if force or counter % 25 == 0:
            nonzero = {k: v for k, v in buf.items() if v}
            if nonzero:
                _increment_mapping_stats(r, mkey, nonzero)
                for k in list(buf.keys()):
                    buf[k] = 0

    def _process_one(src_file: Path) -> None:
        """Worker function — runs in thread pool. Opens its own DB connection
        on first call per thread, reuses thereafter."""
        if not hasattr(local, "conn"):
            local.conn = _get_db_conn()
            local.conn.autocommit = True
            local.buf = {
                "copied": 0, "skipped_existing": 0, "skipped_unsupported": 0,
                "errors": 0, "ocr_queued": 0, "total_bytes": 0,
            }
            local.flush_counter = 0

        conn = local.conn
        buf = local.buf

        try:
            ext = src_file.suffix.lower()
            if ext and ext not in DEFAULT_SUPPORTED_EXTENSIONS:
                buf["skipped_unsupported"] += 1
                _flush_stats()
                return

            try:
                rel = src_file.relative_to(source_dir)
            except ValueError:
                buf["errors"] += 1
                _flush_stats()
                return

            try:
                checksum = _hash_file(src_file)
                file_size = src_file.stat().st_size
                mtime = datetime.fromtimestamp(src_file.stat().st_mtime, tz=timezone.utc)
            except Exception as exc:
                log.warning("sync_mapping %s hash failed %s: %s", mkey, src_file, exc)
                buf["errors"] += 1
                _flush_stats()
                return

            cur = conn.cursor()
            cur.execute(
                """SELECT id, file_path FROM dms_documents
                   WHERE TRIM(tenant_id) = %s AND file_hash = %s
                   LIMIT 1""",
                (tid, checksum),
            )
            existing = cur.fetchone()
            # Apply legacy folder name mapping so 'Pleadings and Motion Practice' etc.
            # land in their standard-tree equivalents (02-Pleadings, etc.)
            # skip_remap=True → raw copy mode, preserve original folder structure
            remapped_rel = str(rel) if skip_remap else _remap_legacy_folder(str(rel))
            dest_file = matter_dest / remapped_rel

            if existing is not None:
                existing_id, existing_path = existing
                if not existing_path or not existing_path.startswith(str(PRAESIDIUM_ROOT)):
                    try:
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_file.exists():
                            shutil.copy2(src_file, dest_file)
                        cur.execute(
                            """UPDATE dms_documents
                               SET file_path = %s, folder_root = %s, updated_at = NOW()
                               WHERE id = %s AND TRIM(tenant_id) = %s""",
                            (str(dest_file), str(matter_dest), existing_id, tid),
                        )
                        buf["copied"] += 1
                        buf["total_bytes"] += file_size
                    except Exception as exc:
                        log.warning("sync_mapping %s copy-update failed %s → %s: %s",
                                    mkey, src_file, dest_file, exc)
                        buf["errors"] += 1
                else:
                    buf["skipped_existing"] += 1
                cur.close()
                _flush_stats()
                return

            # Net-new document — copy, then insert
            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
            except Exception as exc:
                log.warning("sync_mapping %s copy failed %s → %s: %s",
                            mkey, src_file, dest_file, exc)
                buf["errors"] += 1
                cur.close()
                _flush_stats()
                return

            doc_id = str(uuid.uuid4())
            if ext in OCR_EXTENSIONS:
                if _should_skip_ocr_by_path(src_file):
                    ocr_status = "skipped_production"
                else:
                    ocr_status = "ocr_pending"
            else:
                ocr_status = "text_native"

            try:
                cur.execute(
                    """INSERT INTO dms_documents
                       (id, tenant_id, file_path, folder_root, file_hash,
                        file_size_bytes, modified_at, ocr_status,
                        extraction_status, source, indexed_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                               'pending', 'matter_sync', NOW(), NOW())
                       ON CONFLICT (tenant_id, file_path) DO UPDATE
                       SET file_hash = EXCLUDED.file_hash,
                           folder_root = EXCLUDED.folder_root,
                           file_size_bytes = EXCLUDED.file_size_bytes,
                           modified_at = EXCLUDED.modified_at,
                           source = EXCLUDED.source,
                           updated_at = NOW()
                       RETURNING id""",
                    (doc_id, tid, str(dest_file), str(matter_dest),
                     checksum, file_size, mtime, ocr_status),
                )
                result = cur.fetchone()
                if result:
                    doc_id = result[0]

                buf["copied"] += 1
                buf["total_bytes"] += file_size

                if ocr_status == "ocr_pending" and ocr_queue is not None:
                    try:
                        ocr_queue.enqueue(
                            "modules.dms.jobs.ocr_pipeline.ocr_document",
                            tid, str(doc_id),
                            job_timeout=3600,
                        )
                        buf["ocr_queued"] += 1
                    except Exception as exc:
                        log.warning("sync_mapping %s OCR enqueue failed %s: %s",
                                    mkey, doc_id, exc)
            except Exception as exc:
                log.warning("sync_mapping %s insert failed %s: %s",
                            mkey, dest_file, exc)
                buf["errors"] += 1
            finally:
                cur.close()

            _flush_stats()

        except Exception as exc:
            # Catch-all to keep a single bad file from killing the thread
            log.exception("sync_mapping %s unexpected error on %s: %s",
                          mkey, src_file, exc)
            if hasattr(local, "buf"):
                local.buf["errors"] += 1
                _flush_stats()

    # ── Run the thread pool ────────────────────────────────────────────────
    with ThreadPoolExecutor(
        max_workers=SYNC_THREAD_POOL_SIZE,
        thread_name_prefix=f"sync_{mapping_idx}",
    ) as pool:
        futures = [pool.submit(_process_one, f) for f in files_to_process]
        for f in as_completed(futures):
            exc = f.exception()
            if exc is not None:
                log.exception("sync_mapping %s future raised: %s", mkey, exc)

        # Final flush — push any remaining buffered stats to Redis.
        # Each thread's buffer may hold <25 files worth of un-flushed counters.
        # Submit one flush sentinel per pool thread; each runs on its owning
        # thread's local storage so it drains that thread's buffer.
        def _flush_sentinel():
            _flush_stats(force=True)

        flush_futures = [pool.submit(_flush_sentinel) for _ in range(SYNC_THREAD_POOL_SIZE)]
        for f in as_completed(flush_futures):
            pass  # ignore errors — best-effort stats flush

    # Pull final ground-truth stats from the DB for this mapping.
    try:
        conn2 = _get_db_conn()
        cur = conn2.cursor()
        cur.execute(
            """SELECT COUNT(*), COALESCE(SUM(file_size_bytes), 0)
               FROM dms_documents
               WHERE TRIM(tenant_id) = %s
                 AND source = 'matter_sync'
                 AND folder_root = %s""",
            (tid, str(matter_dest)),
        )
        count, total_bytes = cur.fetchone()
        cur.close()
        conn2.close()
    except Exception as exc:
        log.warning("sync_mapping %s final stats query failed: %s", mkey, exc)
        count, total_bytes = None, None

    final_status = {
        "status": "finished",
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }
    if count is not None:
        final_status["final_db_doc_count"] = str(count)
        final_status["final_db_total_bytes"] = str(total_bytes or 0)
    r.hset(mkey, mapping=final_status)

    # Return final Redis-aggregated stats for RQ job result
    raw = r.hgetall(mkey)
    out = {}
    for k, v in raw.items():
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        out[k] = v

    log.info(
        "sync_mapping %s complete: copied=%s skipped=%s errors=%s ocr=%s bytes=%s",
        mkey,
        out.get("copied", "0"),
        out.get("skipped_existing", "0"),
        out.get("errors", "0"),
        out.get("ocr_queued", "0"),
        out.get("total_bytes", "0"),
    )
    return out
