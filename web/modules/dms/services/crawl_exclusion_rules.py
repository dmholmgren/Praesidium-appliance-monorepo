from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
"""
Crawl Exclusion Rules — Admin API
Manage per-tenant rules that control what the file crawler skips.
Accessible from admin portal at /admin/crawl-rules/.

Rule types:
  exclude_extension  — skip files with this extension (e.g. ".pst")
  include_extension  — whitelist mode: only crawl these extensions
  max_file_size      — skip files larger than N bytes
  min_file_size      — skip files smaller than N bytes
  exclude_path       — skip paths matching this prefix/glob
  exclude_pattern    — skip filenames matching this regex
"""

import os
import re
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/crawl-rules", tags=["admin-crawl-rules"])

VALID_RULE_TYPES = {
    "exclude_extension",
    "include_extension",
    "max_file_size",
    "min_file_size",
    "exclude_path",
    "exclude_pattern",
}


class CreateRuleRequest(BaseModel):
    rule_type: str
    value: str
    description: str = ""


class UpdateRuleRequest(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


# ── CRUD Endpoints ─────────────────────────────────────────

@router.get("")
async def list_rules(request: Request, active_only: bool = False):
    """List all crawl exclusion rules for the current tenant."""
    from core.db.base import TenantSession, get_session_factory

    tenant_id = request.state.tenant_id
    session = TenantSession(get_session_factory()(), tenant_id)

    query = "SELECT * FROM crawl_exclusion_rules WHERE tenant_id = :tid"
    params = {"tid": tenant_id}

    if active_only:
        query += " AND session_end IS NULL"

    query += " ORDER BY rule_type, value"
    rules = session.execute(query, params).fetchall()

    return {
        "rules": [dict(r) for r in rules],
        "rule_types": sorted(VALID_RULE_TYPES),
    }


@router.post("")
async def create_rule(body: CreateRuleRequest, request: Request):
    """Create a new crawl exclusion rule."""
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    if body.rule_type not in VALID_RULE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid rule_type. Must be one of: {', '.join(sorted(VALID_RULE_TYPES))}",
        )

    # Validate the value based on rule type
    _validate_rule_value(body.rule_type, body.value)

    rule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    session.execute(
        sa_text("""INSERT INTO crawl_exclusion_rules
        (id, tenant_id, rule_type, value, description,
          created_at, created_by, updated_at)
        VALUES (:id, :tid, :rtype, :val, :desc,
                1, :now, :by, :now)"""),
        {
            "id": rule_id, "tid": tenant_id,
            "rtype": body.rule_type, "val": body.value,
            "desc": body.description,
            "now": now, "by": user_id,
        },
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="create", module="dms",
        table_name="crawl_exclusion_rules",
        record_id=rule_id,
        new_value={
            "rule_type": body.rule_type,
            "value": body.value,
            "description": body.description,
        },
        source="admin_crawl_rules",
    )
    session.commit()

    # Invalidate cached rules
    _invalidate_rule_cache(tenant_id)

    return {"id": rule_id, "rule_type": body.rule_type, "value": body.value}


@router.put("/{rule_id}")
async def update_rule(rule_id: str, body: UpdateRuleRequest, request: Request):
    """Update an existing crawl exclusion rule."""
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    existing = session.execute(
        sa_text("SELECT * FROM crawl_exclusion_rules WHERE id = :id AND tenant_id = :tid"),
        {"id": rule_id, "tid": tenant_id},
    ).fetchone()

    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")

    updates = {}
    if body.value is not None:
        _validate_rule_value(existing["rule_type"], body.value)
        updates["value"] = body.value
    if body.description is not None:
        updates["description"] = body.description
    if body.is_active is not None:
        updates["is_active"] = 1 if body.is_active else 0

    if not updates:
        return {"id": rule_id, "updated": False}

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = rule_id
    updates["tid"] = tenant_id

    session.execute(
        f"UPDATE crawl_exclusion_rules SET {set_clause} WHERE id = :id AND tenant_id = :tid",
        updates,
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="update", module="dms",
        table_name="crawl_exclusion_rules",
        record_id=rule_id,
        old_value={k: existing[k] for k in body.dict(exclude_none=True)},
        new_value=body.dict(exclude_none=True),
        source="admin_crawl_rules",
    )
    session.commit()

    _invalidate_rule_cache(tenant_id)
    return {"id": rule_id, "updated": True}


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str, request: Request):
    """Delete a crawl exclusion rule."""
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    existing = session.execute(
        sa_text("SELECT * FROM crawl_exclusion_rules WHERE id = :id AND tenant_id = :tid"),
        {"id": rule_id, "tid": tenant_id},
    ).fetchone()

    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")

    session.execute(
        sa_text("DELETE FROM crawl_exclusion_rules WHERE id = :id AND tenant_id = :tid"),
        {"id": rule_id, "tid": tenant_id},
    )

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="delete", module="dms",
        table_name="crawl_exclusion_rules",
        record_id=rule_id,
        old_value=dict(existing),
        source="admin_crawl_rules",
    )
    session.commit()

    _invalidate_rule_cache(tenant_id)
    return {"id": rule_id, "deleted": True}


# ── Bulk Presets ───────────────────────────────────────────

@router.post("/presets/ediscovery-safe")
async def apply_ediscovery_preset(request: Request):
    """
    Apply a preset that excludes common eDiscovery raw data file types
    and sets a sensible max file size. Designed for firms with existing
    raw eDiscovery in their file shares.
    """
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)
    now = datetime.now(timezone.utc).isoformat()

    preset_rules = [
        # Large container formats — these are raw collections, not work product
        ("exclude_extension", ".pst", "Outlook data files — raw eDiscovery collections"),
        ("exclude_extension", ".ost", "Offline Outlook data — raw collections"),
        ("exclude_extension", ".nsf", "Lotus Notes archives — raw collections"),
        ("exclude_extension", ".mbox", "Unix mailbox archives — raw collections"),
        # Database dumps
        ("exclude_extension", ".sql", "Database dumps — not document content"),
        ("exclude_extension", ".bak", "Database backups"),
        ("exclude_extension", ".mdb", "Access databases — raw data"),
        ("exclude_extension", ".accdb", "Access databases — raw data"),
        # Disk images and archives
        ("exclude_extension", ".iso", "Disk images"),
        ("exclude_extension", ".vmdk", "Virtual disk images"),
        ("exclude_extension", ".vhd", "Virtual hard disk"),
        ("exclude_extension", ".tar", "Archive files"),
        ("exclude_extension", ".gz", "Compressed archives"),
        ("exclude_extension", ".zip", "Zip archives — crawl extracted contents instead"),
        ("exclude_extension", ".7z", "7-Zip archives"),
        ("exclude_extension", ".rar", "RAR archives"),
        # System and temp files
        ("exclude_extension", ".tmp", "Temporary files"),
        ("exclude_extension", ".bak", "Backup files"),
        ("exclude_extension", ".log", "Log files"),
        ("exclude_extension", ".dat", "Generic data files"),
        ("exclude_extension", ".db", "Database files"),
        ("exclude_extension", ".lnk", "Windows shortcuts"),
        ("exclude_extension", ".thumbs.db", "Windows thumbnail cache"),
        # Multimedia — typically not OCR-relevant
        ("exclude_extension", ".mp3", "Audio files"),
        ("exclude_extension", ".mp4", "Video files"),
        ("exclude_extension", ".avi", "Video files"),
        ("exclude_extension", ".mov", "Video files"),
        ("exclude_extension", ".wmv", "Video files"),
        ("exclude_extension", ".wav", "Audio files"),
        # Max file size — 100 MB
        ("max_file_size", "104857600", "Skip files over 100 MB — likely raw data, not documents"),
        # Common eDiscovery raw data paths
        ("exclude_path", "eDiscovery/Raw/", "Raw eDiscovery collection data"),
        ("exclude_path", "eDiscovery/Native/", "Native file collections pre-processing"),
        ("exclude_path", "eDiscovery/Exports/", "Relativity/Concordance export sets"),
        ("exclude_path", "_ediscovery/", "Legacy eDiscovery data folder"),
        # Word temp files
        ("exclude_pattern", r"^~\$", "Word/Excel temp lock files"),
        ("exclude_pattern", r"^~.*\.tmp$", "Office temp files"),
        ("exclude_pattern", r"^Thumbs\.db$", "Windows thumbnail cache"),
        ("exclude_pattern", r"^\.DS_Store$", "macOS metadata"),
    ]

    created_ids = []
    for rule_type, value, description in preset_rules:
        # Skip if rule already exists
        existing = session.execute(
            sa_text("""SELECT id FROM crawl_exclusion_rules
            WHERE tenant_id = :tid AND rule_type = :rt AND value = :val"""),
            {"tid": tenant_id, "rt": rule_type, "val": value},
        ).fetchone()

        if existing:
            continue

        rule_id = str(uuid.uuid4())
        session.execute(
            sa_text("""INSERT INTO crawl_exclusion_rules
            (id, tenant_id, rule_type, value, description,
              created_at, created_by, updated_at)
            VALUES (:id, :tid, :rtype, :val, :desc,
                    1, :now, :by, :now)"""),
            {
                "id": rule_id, "tid": tenant_id,
                "rtype": rule_type, "val": value,
                "desc": description,
                "now": now, "by": user_id,
            },
        )
        created_ids.append(rule_id)

    write_audit(
        tenant_id=tenant_id, user_id=user_id,
        action="apply_preset", module="dms",
        table_name="crawl_exclusion_rules",
        record_id="ediscovery-safe",
        new_value={"rules_created": len(created_ids)},
        source="admin_crawl_rules",
    )
    session.commit()

    _invalidate_rule_cache(tenant_id)
    return {
        "preset": "ediscovery-safe",
        "rules_created": len(created_ids),
        "rules_skipped_existing": len(preset_rules) - len(created_ids),
    }


# ── Rule Loading (used by file crawler) ───────────────────

def load_exclusion_rules(tenant_id: str) -> dict:
    """
    Load all active exclusion rules for a tenant.
    Returns a compiled ruleset dict used by the crawler's should_skip() function.
    Cached in Redis with 5-minute TTL.
    """
    import json

    # Try Redis cache first
    try:
        from redis import Redis
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        r = Redis.from_url(redis_url)
        cache_key = f"crawl_rules:{tenant_id}"
        cached = r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    # Load from DB
    import psycopg2
    db_url = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    _conn = psycopg2.connect(db_url)
    _cur = _conn.cursor()
    _cur.execute("SELECT rule_type, value FROM crawl_exclusion_rules WHERE TRIM(tenant_id) = %s AND session_end IS NULL", (tenant_id.strip(),))
    rules = [{"rule_type": r[0], "value": r[1]} for r in _cur.fetchall()]
    _conn.close()

    ruleset = {
        "excluded_extensions": set(),
        "included_extensions": set(),  # If populated, acts as whitelist
        "max_file_size": None,
        "min_file_size": None,
        "excluded_paths": [],
        "excluded_patterns": [],
    }

    for rule in rules:
        rt = rule["rule_type"]
        val = rule["value"]

        if rt == "exclude_extension":
            ext = val if val.startswith(".") else f".{val}"
            ruleset["excluded_extensions"].add(ext.lower())
        elif rt == "include_extension":
            ext = val if val.startswith(".") else f".{val}"
            ruleset["included_extensions"].add(ext.lower())
        elif rt == "max_file_size":
            try:
                size = int(val)
                if ruleset["max_file_size"] is None or size < ruleset["max_file_size"]:
                    ruleset["max_file_size"] = size
            except ValueError:
                pass
        elif rt == "min_file_size":
            try:
                size = int(val)
                if ruleset["min_file_size"] is None or size > ruleset["min_file_size"]:
                    ruleset["min_file_size"] = size
            except ValueError:
                pass
        elif rt == "exclude_path":
            ruleset["excluded_paths"].append(val)
        elif rt == "exclude_pattern":
            try:
                ruleset["excluded_patterns"].append(re.compile(val))
            except re.error:
                logger.warning(f"Invalid regex pattern in crawl rule: {val}")

    # Serialize for Redis (convert sets to lists, patterns to strings)
    cache_data = {
        "excluded_extensions": list(ruleset["excluded_extensions"]),
        "included_extensions": list(ruleset["included_extensions"]),
        "max_file_size": ruleset["max_file_size"],
        "min_file_size": ruleset["min_file_size"],
        "excluded_paths": ruleset["excluded_paths"],
        "excluded_patterns": [p.pattern for p in ruleset["excluded_patterns"]],
    }

    try:
        r.setex(cache_key, 300, json.dumps(cache_data))  # 5-min TTL
    except Exception:
        pass

    return cache_data


def should_skip_file(
    ruleset: dict,
    filename: str,
    file_size: int,
    file_path: str,
) -> tuple[bool, str]:
    """
    Check whether a file should be skipped based on the tenant's exclusion rules.
    Returns (should_skip, reason).
    Called by the file crawler for every file encountered.
    """
    ext = os.path.splitext(filename)[1].lower()

    # Extension whitelist mode — if include_extensions is populated,
    # ONLY those extensions are crawled
    if ruleset.get("included_extensions"):
        included = set(ruleset["included_extensions"])
        if ext not in included:
            return True, f"extension {ext} not in include list"

    # Extension blacklist
    if ext in set(ruleset.get("excluded_extensions", [])):
        return True, f"extension {ext} excluded"

    # File size checks
    max_size = ruleset.get("max_file_size")
    if max_size is not None and file_size > max_size:
        return True, f"file size {file_size} exceeds max {max_size}"

    min_size = ruleset.get("min_file_size")
    if min_size is not None and file_size < min_size:
        return True, f"file size {file_size} below min {min_size}"

    # Path prefix exclusions
    for excluded_path in ruleset.get("excluded_paths", []):
        if excluded_path.endswith("*"):
            prefix = excluded_path.rstrip("*")
            if file_path.startswith(prefix) or f"/{prefix}" in file_path:
                return True, f"path matches excluded prefix {excluded_path}"
        elif excluded_path in file_path:
            return True, f"path contains excluded segment {excluded_path}"

    # Filename pattern exclusions
    for pattern_str in ruleset.get("excluded_patterns", []):
        try:
            if re.match(pattern_str, filename):
                return True, f"filename matches excluded pattern {pattern_str}"
        except re.error:
            pass

    return False, ""


def _validate_rule_value(rule_type: str, value: str):
    """Validate that a rule value is well-formed."""
    if rule_type in ("exclude_extension", "include_extension"):
        if not value.startswith("."):
            raise HTTPException(
                status_code=400,
                detail=f"Extension must start with a dot (e.g. '.pst'). Got: {value}",
            )
    elif rule_type in ("max_file_size", "min_file_size"):
        try:
            size = int(value)
            if size <= 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Size must be a positive integer (bytes). Got: {value}",
            )
    elif rule_type == "exclude_pattern":
        try:
            re.compile(value)
        except re.error as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid regex pattern: {e}",
            )


def _invalidate_rule_cache(tenant_id: str):
    """Clear cached rules so next crawl picks up changes immediately."""
    try:
        from redis import Redis
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        r = Redis.from_url(redis_url)
        r.delete(f"crawl_rules:{tenant_id}")
    except Exception:
        pass
