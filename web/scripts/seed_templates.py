#!/usr/bin/env python3
"""
seed_templates.py — M10 Template Seeder
Syncs filesystem templates from /app/core/templates/ into the ui_templates DB table.

Run after every deploy that adds or modifies templates:
    docker exec praesidium-web python /app/scripts/seed_templates.py

Behavior:
  - Walks /app/core/templates/ recursively for *.html files
  - Upserts each file into ui_templates (INSERT ON CONFLICT DO UPDATE)
  - Version is auto-incremented when content changes; untouched files are skipped
  - Idempotent — safe to run on every deploy

Table expected (created by 0019_m10_connector_registry migration):
    ui_templates (
        id           SERIAL PRIMARY KEY,
        template_name TEXT UNIQUE NOT NULL,   -- relative path from templates root, e.g. "tenant_admin/connector_configure.html"
        content       TEXT NOT NULL,
        version       INTEGER NOT NULL DEFAULT 1,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at    TIMESTAMPTZ DEFAULT NOW(),
        updated_at    TIMESTAMPTZ DEFAULT NOW()
    )

Architectural constraints:
  - Uses psycopg2 synchronous connection (script context, not async)
  - CAST(:value AS jsonb) not applicable here — plain text content
  - DB_URL parsed with rfind('@') to handle '@' in password
"""

import hashlib
import os
import sys

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEMPLATES_ROOT = "/app/core/templates"
DB_URL = os.environ.get("DATABASE_URL", "")

# Supported file extensions
EXTENSIONS = {".html", ".htm", ".txt", ".jinja2", ".j2"}


# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------

def parse_db_url(url: str) -> dict:
    """
    Parse DATABASE_URL safely.
    Handles passwords containing '@' by using rfind('@') per Praesidium convention.
    Returns psycopg2 keyword arguments dict, not a DSN string.
    """
    url = url.strip()
    # Strip dialect prefix
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    # Split user:pass from host using rfind('@') — handles '@' in password
    at = url.rfind('@')
    userinfo = url[:at]
    hostinfo = url[at + 1:]
    # Split user:pass
    colon = userinfo.find(':')
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    # Split host:port/dbname
    slash = hostinfo.find('/')
    hostport = hostinfo[:slash]
    dbname = hostinfo[slash + 1:]
    if ':' in hostport:
        host, port_str = hostport.rsplit(':', 1)
        # Use port 5432 directly — bypass PgBouncer for script context
        port = 5432
    else:
        host = hostport
        port = 5432
    return {"host": host, "port": port, "user": user, "password": password, "dbname": dbname}


def get_connection():
    if not DB_URL:
        print("ERROR: DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)
    parsed = parse_db_url(DB_URL)
    return psycopg2.connect(**parsed)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def collect_templates(root: str) -> list[tuple[str, str]]:
    """
    Walk the templates root and collect (template_name, content) pairs.
    template_name is the relative path from root, e.g. "tenant_admin/connector_configure.html"
    """
    results = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, root)
            # Normalize to forward slashes
            template_name = rel_path.replace(os.sep, "/")
            with open(abs_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            results.append((template_name, content))
    return results


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def seed(conn, templates: list[tuple[str, str]]) -> dict:
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        for template_name, content in templates:
            try:
                # Check existing
                cur.execute(
                    "SELECT id, version, content FROM ui_templates WHERE template_name = %s",
                    (template_name,),
                )
                existing = cur.fetchone()

                if existing is None:
                    # Insert new
                    cur.execute(
                        """
                        INSERT INTO ui_templates (template_name, content, version, is_active, created_at, updated_at)
                        VALUES (%s, %s, 1, TRUE, NOW(), NOW())
                        """,
                        (template_name, content),
                    )
                    stats["inserted"] += 1
                    print(f"  [INSERT] {template_name}")
                else:
                    # Compare content hash to avoid spurious version bumps
                    existing_hash = content_hash(existing["content"])
                    new_hash = content_hash(content)
                    if existing_hash == new_hash:
                        stats["skipped"] += 1
                        print(f"  [SKIP]   {template_name}  (unchanged)")
                    else:
                        new_version = existing["version"] + 1
                        cur.execute(
                            """
                            UPDATE ui_templates
                            SET content    = %s,
                                version    = %s,
                                is_active  = TRUE,
                                updated_at = NOW()
                            WHERE template_name = %s
                            """,
                            (content, new_version, template_name),
                        )
                        stats["updated"] += 1
                        print(f"  [UPDATE] {template_name}  (v{existing['version']} → v{new_version})")

            except Exception as exc:
                conn.rollback()
                stats["errors"] += 1
                print(f"  [ERROR]  {template_name}: {exc}", file=sys.stderr)
                continue

        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Praesidium seed_templates.py")
    print(f"Templates root: {TEMPLATES_ROOT}")
    print("=" * 60)

    if not os.path.isdir(TEMPLATES_ROOT):
        print(f"ERROR: Templates root does not exist: {TEMPLATES_ROOT}", file=sys.stderr)
        sys.exit(1)

    templates = collect_templates(TEMPLATES_ROOT)
    if not templates:
        print("No templates found — nothing to seed.")
        return

    print(f"Found {len(templates)} template(s) to process.\n")

    conn = get_connection()
    try:
        stats = seed(conn, templates)
    finally:
        conn.close()

    print()
    print("─" * 40)
    print(f"  Inserted : {stats['inserted']}")
    print(f"  Updated  : {stats['updated']}")
    print(f"  Skipped  : {stats['skipped']}")
    print(f"  Errors   : {stats['errors']}")
    print("─" * 40)

    if stats["errors"] > 0:
        print("\nWARNING: Some templates failed to seed. Check output above.", file=sys.stderr)
        sys.exit(1)
    else:
        print("\nDone. All templates seeded successfully.")


if __name__ == "__main__":
    main()
