"""
purge_redline_cache.py — Nightly cleanup of redline staging area.

Scans /mnt/praesidium/{tenant_id}/.tmp/redlines/ across all tenants
and deletes files older than the configured retention period (default
24 hours). This prevents the staging area from growing unbounded when
users generate redlines but don't click "Save to DMS."

Intended to be scheduled as a nightly RQ job via rqscheduler or cron:

    # cron (on host):
    0 2 * * * docker exec praesidium-proc-worker-1 \
        python -m jobs.purge_redline_cache

    # Or as RQ scheduled job:
    scheduler.schedule(
        scheduled_time=datetime.utcnow(),
        func='jobs.purge_redline_cache.run_purge',
        interval=86400,
        repeat=None,
    )

The purge is safe to run concurrently — it only deletes files based on
mtime and never touches the DMS-committed copies (those live in the
matter folder tree, not .tmp/).

Also cleans up:
  - .cache/compares/ (legacy location from compare_worker v1)
  - Empty directories left behind after file deletion
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Retention: files older than this are deleted
DEFAULT_MAX_AGE_HOURS = 24

# Directories to purge (relative to /mnt/praesidium/{tenant_id}/)
PURGE_PATHS = [
    ".tmp/redlines",       # v2 staging area (current)
    ".cache/compares",     # v1 legacy location
]


def _storage_root() -> Path:
    return Path(os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium"))


def _discover_tenants() -> list[str]:
    """Find tenant directories under the storage root.

    Tenant dirs are UUID-shaped subdirectories. Skip dot-prefixed dirs,
    files, and anything that isn't a directory.
    """
    root = _storage_root()
    if not root.exists():
        return []
    tenants = []
    for entry in root.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            tenants.append(entry.name)
    return tenants


def _purge_directory(
    directory: Path,
    max_age_seconds: float,
    now: float,
    dry_run: bool = False,
) -> dict:
    """Delete files older than max_age_seconds from directory.

    Returns stats dict with counts and bytes freed.
    """
    stats = {"files_deleted": 0, "bytes_freed": 0, "errors": 0, "skipped": 0}

    if not directory.exists():
        return stats

    for entry in directory.iterdir():
        if not entry.is_file():
            stats["skipped"] += 1
            continue

        try:
            age_seconds = now - entry.stat().st_mtime
            if age_seconds > max_age_seconds:
                size = entry.stat().st_size
                if dry_run:
                    logger.info(
                        "[redline-purge] DRY RUN would delete: %s "
                        "(age=%.1fh, size=%d)",
                        entry, age_seconds / 3600, size,
                    )
                else:
                    entry.unlink()
                    logger.debug(
                        "[redline-purge] deleted: %s (age=%.1fh, size=%d)",
                        entry, age_seconds / 3600, size,
                    )
                stats["files_deleted"] += 1
                stats["bytes_freed"] += size
            else:
                stats["skipped"] += 1
        except Exception as exc:
            logger.warning(
                "[redline-purge] error processing %s: %s", entry, exc
            )
            stats["errors"] += 1

    # Remove the directory itself if now empty (but don't fail if not)
    if not dry_run:
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except Exception:
            pass

    return stats


def run_purge(
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    dry_run: bool = False,
) -> dict:
    """Main entrypoint. Scans all tenants, purges expired redline staging files.

    Returns summary dict suitable for RQ job result / logging.
    """
    started = time.monotonic()
    now = time.time()
    max_age_seconds = max_age_hours * 3600

    tenants = _discover_tenants()
    logger.info(
        "[redline-purge] starting purge: %d tenants, max_age=%dh, dry_run=%s",
        len(tenants), max_age_hours, dry_run,
    )

    total = {"tenants_scanned": len(tenants), "files_deleted": 0,
             "bytes_freed": 0, "errors": 0}

    for tenant_id in tenants:
        tenant_root = _storage_root() / tenant_id
        for rel_path in PURGE_PATHS:
            purge_dir = tenant_root / rel_path
            if not purge_dir.exists():
                continue

            stats = _purge_directory(purge_dir, max_age_seconds, now, dry_run)
            total["files_deleted"] += stats["files_deleted"]
            total["bytes_freed"] += stats["bytes_freed"]
            total["errors"] += stats["errors"]

            if stats["files_deleted"] > 0:
                logger.info(
                    "[redline-purge] tenant=%s path=%s deleted=%d freed=%d",
                    tenant_id, rel_path,
                    stats["files_deleted"], stats["bytes_freed"],
                )

    total["duration_seconds"] = round(time.monotonic() - started, 3)
    total["completed_at"] = datetime.now(timezone.utc).isoformat()
    total["dry_run"] = dry_run

    logger.info(
        "[redline-purge] complete: deleted=%d files, freed=%s, "
        "errors=%d, duration=%.2fs",
        total["files_deleted"],
        _human_size(total["bytes_freed"]),
        total["errors"],
        total["duration_seconds"],
    )
    return total


def _human_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# ═════════════════════════════════════════════════════════════════════════
# CLI entrypoint
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Purge expired redline staging files"
    )
    parser.add_argument(
        "--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS,
        help=f"Delete files older than N hours (default: {DEFAULT_MAX_AGE_HOURS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    result = run_purge(
        max_age_hours=args.max_age_hours,
        dry_run=args.dry_run,
    )
    print(f"\nPurge complete: {result}")
    sys.exit(1 if result["errors"] > 0 else 0)
