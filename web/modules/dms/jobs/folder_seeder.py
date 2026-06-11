"""
modules/dms/jobs/folder_seeder.py

Standard folder structure seeder per matter type. Creates the canonical
directory tree under /mnt/praesidium/{tenant_id}/matters/{matter_id}/ when
a matter has no folder tree yet (e.g., historical matters brought over via
reconciliation, matters created outside the new-matter-intake flow).

Folder structures from HJMM_Technical_Requirements §9.1:
  - litigation
  - transactional_loan
  - transactional_securities
  - ogc_retainer

Defaults to litigation if matter_type is unknown or NULL.

Runs on PROC-01. Idempotent — creates missing folders, leaves existing ones
and their contents alone.

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)

PRAESIDIUM_ROOT = Path(os.environ.get("CIFS_PRAESIDIUM_MOUNT", "/mnt/praesidium"))

# PROC-01 hostname / IP for direct-write enforcement.
# Override via env var if the PROC-01 hostname differs in your environment.
PROC01_HOSTNAMES = set(
    h.strip().lower()
    for h in os.environ.get("PROC01_HOSTNAMES", "proc-01,main-prd-proc-01,praesidium-proc-01").split(",")
    if h.strip()
)


def _get_db_conn():
    """psycopg2 connection — rfind('@') password pattern."""
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


# ─── Standard folder structures ───────────────────────────────────────────────
#
# Per HJMM_Technical_Requirements §9.1. Each structure is a flat list of
# numbered top-level folders. Matter_type column on matters is varchar(15),
# so we normalize values to the set of keys below.

FOLDER_STRUCTURES: dict[str, list[str]] = {
    "litigation": [
        "01-Client Documents",
        "02-Pleadings",
        "03-Discovery",
        "04-Correspondence",
        "05-Research",
        "06-Court Filings",
        "07-Depositions",
        "08-Experts",
        "09-Mediation",
        "10-Orders",
        "11-Working Docs",
        "12-eDiscovery",
        "13-Billing",
        "14-Trial Preparation",
        "15-Email",
    ],
    "transactional_loan": [
        "01-Client Documents",
        "02-Loan Documents",
        "03-Title",
        "04-Appraisal",
        "05-Borrower Documents",
        "06-Property",
        "07-Insurance",
        "08-Environmental",
        "09-Closing",
        "10-Post-Closing",
        "11-Correspondence",
        "12-Billing",
        "13-Email",
    ],
    "transactional_securities": [
        "01-Client Documents",
        "02-Offering Documents",
        "03-SEC Filings",
        "04-Investor Materials",
        "05-Research",
        "06-Correspondence",
        "07-Billing",
        "08-Email",
    ],
    "transactional": [
        # Generic transactional fallback
        "01-Client Documents",
        "02-Transaction Documents",
        "03-Due Diligence",
        "04-Correspondence",
        "05-Research",
        "06-Closing",
        "07-Post-Closing",
        "08-Billing",
        "09-Email",
    ],
    "ogc_retainer": [
        "01-Client Documents",
        "02-Governance",
        "03-Contracts",
        "04-Regulatory",
        "05-Sub-Matters",
        "06-Correspondence",
        "07-Research",
        "08-Billing",
        "09-Email",
    ],
}


def _sanitize_fs_name(name: str) -> str:
    """Make a string safe for use as a filesystem directory name.

    Replaces characters that are illegal or fragile across Linux/macOS/Windows
    with '-', collapses internal whitespace, and strips leading/trailing dots
    and spaces (Windows doesn't allow trailing dots/spaces on dir names).
    Returns empty string for None/empty input."""
    if not name:
        return ""
    s = str(name)
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, "-")
    # Collapse repeated whitespace into single space
    s = " ".join(s.split())
    # Strip trailing dots/spaces (Windows filesystem compatibility)
    s = s.rstrip(". ")
    # Strip leading dots/spaces as well
    s = s.lstrip(". ")
    return s or "_unnamed_"


def _matter_dest_path(
    tenant_id: str,
    client_name: str,
    matter_name: str,
    matter_number: str | None = None,
) -> Path:
    """Build the canonical /mnt/praesidium destination path for a matter using
    human-readable client + matter names sanitized for the filesystem.

    Shape: /mnt/praesidium/{tenant_id}/matters/{client_name}/{matter_name_or_number}/

    Prefers matter_name for human readability. Caller can pass matter_number
    if matter_name is blank."""
    tid = (tenant_id or "").strip()
    client_safe = _sanitize_fs_name(client_name)
    matter_safe = _sanitize_fs_name(matter_name) or _sanitize_fs_name(matter_number) or "_unnamed_matter_"
    return PRAESIDIUM_ROOT / tid / "matters" / client_safe / matter_safe


def _normalize_matter_type(raw: str | None) -> str:
    """Normalize a matter_type value to one of the FOLDER_STRUCTURES keys.
    Defaults to 'litigation' if unknown."""
    if not raw:
        return "litigation"
    v = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if v in FOLDER_STRUCTURES:
        return v
    # Common aliases
    if v in ("lit", "litig"):
        return "litigation"
    if v in ("trans_loan", "loan"):
        return "transactional_loan"
    if v in ("trans_securities", "securities"):
        return "transactional_securities"
    if v in ("trans", "transactional", "transaction"):
        return "transactional"
    if v in ("ogc", "retainer"):
        return "ogc_retainer"
    log.info("folder_seeder unknown matter_type=%r — defaulting to litigation", raw)
    return "litigation"


def _assert_proc01_or_warn() -> None:
    """Soft check that we're running on PROC-01. Logs a warning if not, but
    does not fail — the task is idempotent and won't cause corruption if it
    runs on the wrong host."""
    try:
        host = socket.gethostname().lower()
    except Exception:
        host = ""
    if PROC01_HOSTNAMES and host not in PROC01_HOSTNAMES:
        short = host.split(".")[0] if "." in host else host
        if short not in PROC01_HOSTNAMES:
            log.warning(
                "folder_seeder/matter_sync running on host=%r — expected PROC-01 "
                "(hostnames: %s). Job continues but may not have direct mount access.",
                host, sorted(PROC01_HOSTNAMES),
            )


def seed_matter_folders(
    tenant_id: str,
    matter_id: str,
    matter_type: str | None = None,
    client_name: str | None = None,
    matter_name: str | None = None,
    matter_number: str | None = None,
    force: bool = False,
) -> dict:
    """Create the standard folder structure for a single matter under
    /mnt/praesidium/{tenant_id}/matters/{client_name}/{matter_name}/.

    Uses human-readable client and matter names (sanitized for the filesystem).
    If any of matter_type/client_name/matter_name are None, looks them up from
    matters joined with clients.

    Returns stats: {matter_id, client_name, matter_name, matter_number,
    matter_type_resolved, created, existing, errors, path}
    """
    _assert_proc01_or_warn()

    tid = (tenant_id or "").strip()
    mid = (matter_id or "").strip()

    if not tid or not mid:
        return {"error": "tenant_id and matter_id required"}

    # Look up fields from DB if any are missing
    resolved_type = matter_type
    resolved_client = client_name
    resolved_matter = matter_name
    resolved_number = matter_number

    if resolved_type is None or not resolved_client or not resolved_matter:
        conn = _get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT m.matter_type, m.matter_name, m.matter_number,
                          c.client_name
                     FROM matters m
                     LEFT JOIN clients c ON c.id = m.client_id
                    WHERE m.id = %s::uuid AND TRIM(m.tenant_id) = %s""",
                (mid, tid),
            )
            row = cur.fetchone()
            if row:
                if resolved_type is None:
                    resolved_type = row[0]
                if not resolved_matter:
                    resolved_matter = row[1]
                if not resolved_number:
                    resolved_number = row[2]
                if not resolved_client:
                    resolved_client = row[3]
        finally:
            conn.close()

    if not resolved_client:
        log.warning(
            "folder_seeder: matter=%s has no client_name — cannot build human path",
            mid,
        )
        return {
            "matter_id": mid,
            "created": 0,
            "existing": 0,
            "errors": 1,
            "error_message": "Matter has no client — cannot build human path",
            "path": None,
        }

    normalized = _normalize_matter_type(resolved_type)
    structure = FOLDER_STRUCTURES[normalized]

    matter_root = _matter_dest_path(
        tid, resolved_client, resolved_matter or "", resolved_number,
    )

    stats = {
        "matter_id": mid,
        "client_name": resolved_client,
        "matter_name": resolved_matter,
        "matter_number": resolved_number,
        "matter_type_raw": resolved_type,
        "matter_type_resolved": normalized,
        "created": 0,
        "existing": 0,
        "errors": 0,
        "path": str(matter_root),
        "folders": [],
    }

    try:
        matter_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.error("folder_seeder: cannot create matter root %s: %s", matter_root, exc)
        stats["errors"] += 1
        stats["error_message"] = str(exc)
        return stats

    for folder_name in structure:
        dest = matter_root / folder_name
        try:
            if dest.exists():
                stats["existing"] += 1
            else:
                dest.mkdir(parents=True, exist_ok=False)
                stats["created"] += 1
            stats["folders"].append(folder_name)
        except FileExistsError:
            stats["existing"] += 1
        except Exception as exc:
            log.warning("folder_seeder: failed to create %s: %s", dest, exc)
            stats["errors"] += 1

    log.info(
        "folder_seeder tenant=%s matter=%s client=%r name=%r type=%s "
        "created=%d existing=%d errors=%d",
        tid, mid, resolved_client, resolved_matter, normalized,
        stats["created"], stats["existing"], stats["errors"],
    )
    return stats


def seed_tenant_folders(tenant_id: str) -> dict:
    """Seed standard folder structures for EVERY active matter in the tenant.
    Idempotent — matters that already have their folders get no changes.
    Runs on 'migration' queue on PROC-01.

    Returns aggregate stats with a per_matter breakdown."""
    _assert_proc01_or_warn()

    tid = (tenant_id or "").strip()
    if not tid:
        return {"error": "tenant_id required"}

    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT m.id::text, m.matter_number, m.matter_name, m.matter_type,
                      c.client_name
                 FROM matters m
                 LEFT JOIN clients c ON c.id = m.client_id
                WHERE TRIM(m.tenant_id) = %s
                  AND m.status = 'active'
                ORDER BY c.client_name NULLS LAST, m.matter_number""",
            (tid,),
        )
        matters = cur.fetchall()
    finally:
        conn.close()

    if not matters:
        return {
            "tenant_id": tid,
            "matters_processed": 0,
            "total_created": 0,
            "total_existing": 0,
            "total_errors": 0,
            "message": "No active matters in this tenant",
            "per_matter": [],
        }

    agg = {
        "tenant_id": tid,
        "matters_processed": 0,
        "total_created": 0,
        "total_existing": 0,
        "total_errors": 0,
        "matters_skipped_no_client": 0,
        "per_matter": [],
    }

    for mid, mnum, mname, mtype, cname in matters:
        try:
            result = seed_matter_folders(
                tid, mid,
                matter_type=mtype,
                client_name=cname,
                matter_name=mname,
                matter_number=mnum,
            )
            agg["matters_processed"] += 1
            agg["total_created"] += result.get("created", 0)
            agg["total_existing"] += result.get("existing", 0)
            agg["total_errors"] += result.get("errors", 0)
            if result.get("error_message") == "Matter has no client — cannot build human path":
                agg["matters_skipped_no_client"] += 1
            agg["per_matter"].append({
                "matter_id": mid,
                "matter_number": mnum,
                "matter_name": mname,
                "client_name": cname,
                "matter_type": result.get("matter_type_resolved"),
                "created": result.get("created", 0),
                "existing": result.get("existing", 0),
                "errors": result.get("errors", 0),
                "path": result.get("path"),
            })
        except Exception as exc:
            log.warning("seed_tenant_folders matter=%s failed: %s", mid, exc)
            agg["total_errors"] += 1

    log.info(
        "folder_seeder tenant=%s matters=%d created=%d existing=%d errors=%d",
        tid, agg["matters_processed"], agg["total_created"],
        agg["total_existing"], agg["total_errors"],
    )
    return agg


def seed_client_folders(tenant_id: str, client_id: str) -> dict:
    """Seed standard folder structures for every matter belonging to a client.

    Iterates over matters for the client, calls seed_matter_folders on each.
    Idempotent — matters that already have their folders get no changes.

    Returns aggregate stats.
    """
    _assert_proc01_or_warn()

    tid = (tenant_id or "").strip()
    cid = (client_id or "").strip()

    if not tid or not cid:
        return {"error": "tenant_id and client_id required"}

    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id::text, matter_number, matter_name, matter_type
               FROM matters
               WHERE client_id = %s::uuid
                 AND TRIM(tenant_id) = %s
                 AND status = 'active'
               ORDER BY matter_number""",
            (cid, tid),
        )
        matters = cur.fetchall()
    finally:
        conn.close()

    if not matters:
        return {
            "client_id": cid,
            "matters_processed": 0,
            "total_created": 0,
            "total_existing": 0,
            "total_errors": 0,
            "message": "No active matters found for this client",
            "per_matter": [],
        }

    agg = {
        "client_id": cid,
        "matters_processed": 0,
        "total_created": 0,
        "total_existing": 0,
        "total_errors": 0,
        "per_matter": [],
    }

    for mid, mnum, mname, mtype in matters:
        result = seed_matter_folders(tid, mid, matter_type=mtype)
        agg["matters_processed"] += 1
        agg["total_created"] += result.get("created", 0)
        agg["total_existing"] += result.get("existing", 0)
        agg["total_errors"] += result.get("errors", 0)
        agg["per_matter"].append({
            "matter_id": mid,
            "matter_number": mnum,
            "matter_name": mname,
            "matter_type": result.get("matter_type_resolved"),
            "created": result.get("created", 0),
            "existing": result.get("existing", 0),
            "errors": result.get("errors", 0),
        })

    log.info(
        "folder_seeder client=%s matters=%d created=%d existing=%d errors=%d",
        cid, agg["matters_processed"], agg["total_created"],
        agg["total_existing"], agg["total_errors"],
    )
    return agg
