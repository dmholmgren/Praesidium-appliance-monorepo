"""
custodian_registry.py -- canonical custodian resolution (custodian model step 2,
plus finish: dedup roll-up canonicalization, manual override, review surface).

A per-matter registry (ediscovery_custodians) collapses custodian variants --
name spellings, PST filenames, and email addresses -- to one canonical custodian,
and links each document via ediscovery_documents.custodian_id. This makes
custodian-scoped dedup and the cross-custodian report count a person once instead
of double-counting "Veronica Flores" vs "vflores@x.com".

The canonicalization pass (canonicalize_collection / _matter):
  1. resolves each doc's step-1 raw custodian against the matter registry
     (self-building, idempotent; harvests email<->name pairings only when
     trustworthy), setting custodian_id + the canonical custodian name; and
  2. rewrites deduped_custodians (the dedup roll-up) through the same registry so
     the cross-custodian report is canonical -- copies held by "Veronica_Flores"
     and "vflores@x.com" collapse to one custodian. Ambiguous/empty entries become
     "(unresolved)" and are kept distinct (never silently merged).

Manual override (assign_custodian): resolve a human-chosen custodian onto a
collection's unresolved docs, an entire collection (pin a single-custodian or
folder-isn't-custodian collection), or explicit doc ids -- recorded with
custodian_source='manual'.

Review surface: custodian_summary(matter) and list_unresolved(collection).

CLI (inside praesidium-web):
    # canonicalize (default; what the orchestrator runs)
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --collection C
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --matter M [--dry-run]
    # manual override
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --collection C \
        --assign "Ernesto Tolivar" [--only-unresolved]
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --assign "Name" --doc <id>[,<id>...]
    # review surface
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --matter M --summary
    python -m modules.ediscovery.jobs.custodian_registry --tenant T --collection C --list-unresolved
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
UNRESOLVED_LABEL = "(unresolved)"
_SEP = re.compile(r"[._\-]+")
_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")


def normalize_key(s):
    if not s:
        return None
    s = _SEP.sub(" ", s)
    s = _WS.sub(" ", s).strip().lower()
    return s or None


def extract_email(s):
    if not s:
        return None
    m = _EMAIL.search(s)
    return m.group(0).lower() if m else None


def display_name(s):
    """'Veronica Flores <v@x>' -> 'Veronica Flores'; bare email stays as-is."""
    if not s:
        return None
    s = s.strip()
    if "<" in s:
        s = s.split("<", 1)[0].strip().strip('"').strip()
    return s or None


def _humanize(nm):
    return _WS.sub(" ", _SEP.sub(" ", nm)).strip() if nm else nm


# --------------------------------------------------------------------------- db
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


def _matter_for_collection(cur, tenant, collection_id):
    cur.execute("SELECT matter_id FROM ediscovery_collections "
                "WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (str(collection_id), tenant))
    row = cur.fetchone()
    if not row:
        raise ValueError("collection %s not found" % collection_id)
    return row[0]


# ----------------------------------------------------------------- registry ops
def _add_email(cur, cid, email):
    if not email:
        return
    email = email.lower()
    cur.execute(
        "UPDATE ediscovery_custodians SET emails = CASE WHEN emails @> to_jsonb(%s::text) "
        "THEN emails ELSE emails || to_jsonb(%s::text) END, updated_at=now() WHERE id=%s",
        (email, email, cid))


def _add_alias(cur, cid, alias):
    if not alias:
        return
    cur.execute(
        "UPDATE ediscovery_custodians SET aliases = CASE WHEN aliases @> to_jsonb(%s::text) "
        "THEN aliases ELSE aliases || to_jsonb(%s::text) END, updated_at=now() WHERE id=%s",
        (alias, alias, cid))


def _harvest(cur, cid, source, candidate_email, candidate_email_name, this_name):
    """Attach an email alias only when the email<->name pairing is trustworthy:
    custodian was derived from this From header (email_from), or the From display
    name matches this custodian's key (the owner's own sent mail)."""
    cur.execute("SELECT canonical_name, normalized_key FROM ediscovery_custodians WHERE id=%s", (cid,))
    canonical_name, ck = cur.fetchone()
    if candidate_email:
        trusted = (source == "email_from"
                   or (candidate_email_name and normalize_key(display_name(candidate_email_name)) == ck))
        if trusted:
            _add_email(cur, cid, candidate_email)
    if this_name and this_name != canonical_name:
        _add_alias(cur, cid, this_name)
    return canonical_name


def resolve_or_create(cur, tenant, matter_id, raw_name, source,
                      candidate_email=None, candidate_email_name=None):
    """Resolve raw_name to a canonical custodian (creating/merging registry rows).
    Returns (custodian_id, canonical_name) or (None, None) if unusable."""
    if not raw_name:
        return None, None
    tenant = tenant.strip()
    mid = str(matter_id)
    nm = display_name(raw_name)
    raw_email = extract_email(raw_name)
    is_email_only = bool(raw_email) and normalize_key(nm) == normalize_key(raw_email)

    # 1) match by a known email alias (links email-only / harvested addresses
    #    to an already-named custodian)
    probe_emails = []
    if is_email_only:
        probe_emails.append(raw_email)
    if candidate_email and source == "email_from":
        probe_emails.append(candidate_email)
    for em in probe_emails:
        cur.execute(
            "SELECT id, canonical_name FROM ediscovery_custodians "
            "WHERE TRIM(tenant_id)=%s AND matter_id=%s::uuid AND emails @> to_jsonb(%s::text) LIMIT 1",
            (tenant, mid, em.lower()))
        r = cur.fetchone()
        if r:
            _harvest(cur, r[0], source, candidate_email, candidate_email_name, nm)
            return r[0], r[1]

    # 2) upsert by normalized key
    if is_email_only:
        canonical, key = raw_email.lower(), raw_email.lower()
    else:
        canonical, key = _humanize(nm), normalize_key(nm)
    if not key:
        return None, None
    cur.execute(
        "INSERT INTO ediscovery_custodians (tenant_id, matter_id, canonical_name, normalized_key) "
        "VALUES (%s, %s::uuid, %s, %s) "
        "ON CONFLICT (tenant_id, matter_id, normalized_key) DO UPDATE SET updated_at=now() "
        "RETURNING id, canonical_name",
        (tenant, mid, canonical, key))
    cid, canonical_name = cur.fetchone()

    # 3) harvest emails / name variants
    if is_email_only:
        _add_email(cur, cid, raw_email)
    canonical_name = _harvest(cur, cid, source, candidate_email, candidate_email_name, nm)
    return cid, canonical_name


# ------------------------------------------------- dedup roll-up canonicalization
def _canonical_map(cur, tenant, matter_id):
    """Build {normalized token -> canonical} and {email -> canonical} for the
    matter, covering canonical names, recorded aliases, and emails."""
    cur.execute("SELECT canonical_name, normalized_key, aliases, emails "
                "FROM ediscovery_custodians WHERE TRIM(tenant_id)=%s AND matter_id=%s::uuid",
                (tenant, str(matter_id)))
    by_key, by_email = {}, {}
    for cn, nk, aliases, emails in cur.fetchall():
        if nk:
            by_key[nk] = cn
        for a in (aliases or []):
            k = normalize_key(display_name(a))
            if k:
                by_key.setdefault(k, cn)
        for e in (emails or []):
            by_email[str(e).lower()] = cn
    return by_key, by_email


def _map_name(raw, by_key, by_email):
    """Map one raw custodian string to its canonical name. Empty -> unresolved;
    unknown -> left as-is (never fabricated)."""
    if not raw or not str(raw).strip():
        return UNRESOLVED_LABEL
    e = extract_email(raw)
    if e and e in by_email:
        return by_email[e]
    k = normalize_key(display_name(raw))
    if k and k in by_key:
        return by_key[k]
    return raw


def _canonicalize_rollup(cur, tenant, collection_id, by_key, by_email):
    """Rewrite deduped_custodians for the collection through the registry, so the
    cross-custodian report is canonical. Returns count of docs changed."""
    cur.execute(
        "SELECT id::text, deduped_custodians FROM ediscovery_documents "
        "WHERE collection_id=%s::uuid AND TRIM(tenant_id)=%s "
        "  AND deduped_custodians IS NOT NULL AND deduped_custodians NOT IN ('', '[]')",
        (str(collection_id), tenant))
    changed = 0
    for did, raw_json in cur.fetchall():
        try:
            names = json.loads(raw_json)
        except Exception:
            continue
        if not isinstance(names, list):
            continue
        out, seen = [], set()
        for nm in names:
            cn = _map_name(nm, by_key, by_email)
            if cn not in seen:
                seen.add(cn)
                out.append(cn)
        new_json = json.dumps(out)
        if new_json != raw_json:
            cur.execute("UPDATE ediscovery_documents SET deduped_custodians=%s WHERE id=%s::uuid",
                        (new_json, did))
            changed += 1
    return changed


# --------------------------------------------------------------- the pass
def canonicalize_collection(tenant_id, collection_id, dry_run=False):
    tenant = tenant_id.strip()
    conn = _connect()
    s = {"docs": 0, "linked": 0, "rollup_changed": 0, "dry_run": dry_run}
    try:
        cur = conn.cursor()
        matter_id = _matter_for_collection(cur, tenant, collection_id)
        cur.execute(
            "SELECT id::text, custodian, custodian_source, doc_type, email_from "
            "FROM ediscovery_documents "
            "WHERE collection_id=%s::uuid AND TRIM(tenant_id)=%s "
            "  AND custodian IS NOT NULL AND custodian_source IS DISTINCT FROM 'unresolved' "
            "ORDER BY id", (str(collection_id), tenant))
        docs = cur.fetchall()
        s["docs"] = len(docs)
        for did, cust, csource, dtype, efrom in docs:
            cand_email = cand_name = None
            if dtype == "email" and efrom:
                cand_email, cand_name = extract_email(efrom), display_name(efrom)
            cid, canonical = resolve_or_create(cur, tenant, matter_id, cust, csource,
                                               cand_email, cand_name)
            if cid is None:
                continue
            if not dry_run:
                cur.execute("UPDATE ediscovery_documents SET custodian_id=%s, custodian=%s "
                            "WHERE id=%s::uuid", (cid, canonical, did))
            s["linked"] += 1
        # roll-up canonicalization (registry now fully built for this collection)
        by_key, by_email = _canonical_map(cur, tenant, matter_id)
        s["rollup_changed"] = _canonicalize_rollup(cur, tenant, collection_id, by_key, by_email)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        cur.execute("SELECT count(*) FROM ediscovery_custodians "
                    "WHERE TRIM(tenant_id)=%s AND matter_id=%s::uuid", (tenant, str(matter_id)))
        s["registry_size"] = cur.fetchone()[0]
        logger.info("canonicalize collection %s: %s", collection_id, s)
        return s
    finally:
        conn.close()


def canonicalize_matter(tenant_id, matter_id, dry_run=False):
    tenant = tenant_id.strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT id::text FROM ediscovery_collections "
                    "WHERE TRIM(tenant_id)=%s AND matter_id=%s::uuid", (tenant, str(matter_id)))
        cids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    agg = {"collections": len(cids), "docs": 0, "linked": 0, "rollup_changed": 0, "dry_run": dry_run}
    for cid in cids:
        r = canonicalize_collection(tenant, cid, dry_run=dry_run)
        agg["docs"] += r["docs"]
        agg["linked"] += r["linked"]
        agg["rollup_changed"] += r["rollup_changed"]
        agg["registry_size"] = r.get("registry_size")
    logger.info("canonicalize matter %s: %s", matter_id, agg)
    return agg


# --------------------------------------------------------------- manual override
def assign_custodian(tenant_id, canonical_name, collection_id=None, doc_ids=None,
                     only_unresolved=False):
    """Resolve a human-chosen custodian onto targeted docs (custodian_source='manual').
    Scope: a collection (optionally only its unresolved docs) OR explicit doc ids."""
    tenant = tenant_id.strip()
    if not collection_id and not doc_ids:
        raise ValueError("assign_custodian needs collection_id or doc_ids")
    conn = _connect()
    try:
        cur = conn.cursor()
        if collection_id:
            matter_id = _matter_for_collection(cur, tenant, collection_id)
        else:
            cur.execute("SELECT DISTINCT c.matter_id FROM ediscovery_documents d "
                        "JOIN ediscovery_collections c ON c.id=d.collection_id "
                        "WHERE d.id = ANY(%s::uuid[]) AND TRIM(d.tenant_id)=%s",
                        (list(doc_ids), tenant))
            ms = [r[0] for r in cur.fetchall()]
            if len(ms) != 1:
                raise ValueError("doc_ids span %d matters; assign within one matter" % len(ms))
            matter_id = ms[0]
        cid, canonical = resolve_or_create(cur, tenant, matter_id, canonical_name, "manual")
        if cid is None:
            raise ValueError("unusable custodian name: %r" % canonical_name)
        if collection_id:
            where = "collection_id=%s::uuid AND TRIM(tenant_id)=%s"
            params = [str(collection_id), tenant]
            if only_unresolved:
                where += " AND custodian_source='unresolved'"
        else:
            where = "id = ANY(%s::uuid[]) AND TRIM(tenant_id)=%s"
            params = [list(doc_ids), tenant]
        cur.execute(f"UPDATE ediscovery_documents SET custodian_id=%s, custodian=%s, "
                    f"custodian_source='manual' WHERE {where}", [cid, canonical] + params)
        n = cur.rowcount
        conn.commit()
        out = {"assigned": n, "custodian": canonical, "custodian_id": str(cid)}
        logger.info("assign_custodian: %s", out)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------- review surface
def custodian_summary(tenant_id, matter_id):
    """Per-canonical-custodian doc/dupe counts for a matter, unresolved last."""
    tenant = tenant_id.strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(cu.canonical_name, %s) AS name, count(*) AS docs, "
            "       count(*) FILTER (WHERE d.is_duplicate) AS dupes "
            "FROM ediscovery_documents d "
            "JOIN ediscovery_collections c ON c.id=d.collection_id "
            "LEFT JOIN ediscovery_custodians cu ON cu.id=d.custodian_id "
            "WHERE c.matter_id=%s::uuid AND TRIM(d.tenant_id)=%s "
            "GROUP BY 1 ORDER BY count(*) DESC",
            (UNRESOLVED_LABEL, str(matter_id), tenant))
        rows = [{"custodian": r[0], "docs": r[1], "dupes": r[2]} for r in cur.fetchall()]
        rows.sort(key=lambda x: (x["custodian"] == UNRESOLVED_LABEL, -x["docs"]))
        return rows
    finally:
        conn.close()


def list_unresolved(tenant_id, collection_id, limit=200):
    """Docs needing custodian triage (custodian_source='unresolved')."""
    tenant = tenant_id.strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id::text, regexp_replace(COALESCE(original_path, file_name, ''), '^.*/', '') "
            "FROM ediscovery_documents "
            "WHERE collection_id=%s::uuid AND TRIM(tenant_id)=%s AND custodian_source='unresolved' "
            "ORDER BY id LIMIT %s", (str(collection_id), tenant, int(limit)))
        return [{"id": r[0], "file": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    ap.add_argument("--collection")
    ap.add_argument("--matter")
    ap.add_argument("--doc", help="comma-separated doc ids (for --assign)")
    ap.add_argument("--assign", metavar="NAME", help="manually assign this custodian")
    ap.add_argument("--only-unresolved", action="store_true",
                    help="with --assign --collection: only docs whose custodian is unresolved")
    ap.add_argument("--summary", action="store_true", help="custodian summary for --matter")
    ap.add_argument("--list-unresolved", action="store_true", help="list unresolved docs for --collection")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.assign:
        doc_ids = [d.strip() for d in args.doc.split(",")] if args.doc else None
        if not args.collection and not doc_ids:
            ap.error("--assign needs --collection or --doc")
        out = assign_custodian(args.tenant, args.assign, collection_id=args.collection,
                               doc_ids=doc_ids, only_unresolved=args.only_unresolved)
    elif args.summary:
        if not args.matter:
            ap.error("--summary needs --matter")
        out = custodian_summary(args.tenant, args.matter)
    elif args.list_unresolved:
        if not args.collection:
            ap.error("--list-unresolved needs --collection")
        out = list_unresolved(args.tenant, args.collection)
    elif args.collection:
        out = canonicalize_collection(args.tenant, args.collection, dry_run=args.dry_run)
    elif args.matter:
        out = canonicalize_matter(args.tenant, args.matter, dry_run=args.dry_run)
    else:
        ap.error("provide --collection or --matter (canonicalize), or an action flag")
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
