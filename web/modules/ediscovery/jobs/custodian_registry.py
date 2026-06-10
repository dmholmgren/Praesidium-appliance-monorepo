"""
custodian_registry.py -- canonical custodian resolution (custodian model step 2).

A per-matter registry (ediscovery_custodians) collapses custodian variants --
name spellings, PST filenames, and email addresses -- to one canonical
custodian, and links each document via ediscovery_documents.custodian_id. This
is what makes custodian-scoped dedup and the cross-custodian report count a
person once instead of double-counting "Veronica Flores" vs "vflores@x.com".

Self-building and idempotent: the canonicalization pass resolves each doc's
step-1 raw custodian against the matter registry, creating entries as needed and
harvesting email<->name pairings from email headers ONLY when the pairing is
trustworthy (custodian was derived from that From header, or the From display
name matches the mailbox owner -- i.e. the owner's own sent mail). Ambiguous
docs (custodian_source='unresolved') are left unlinked.

CLI (inside praesidium-web):
    python -m modules.ediscovery.jobs.custodian_registry --tenant <uuid> --collection <uuid>
    python -m modules.ediscovery.jobs.custodian_registry --tenant <uuid> --matter <uuid> [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
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
        canonical, key = _WS.sub(" ", _SEP.sub(" ", nm)).strip(), normalize_key(nm)
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


# --------------------------------------------------------------- the pass
def canonicalize_collection(tenant_id, collection_id, dry_run=False):
    tenant = tenant_id.strip()
    conn = _connect()
    s = {"docs": 0, "linked": 0, "dry_run": dry_run}
    try:
        cur = conn.cursor()
        cur.execute("SELECT matter_id FROM ediscovery_collections "
                    "WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (str(collection_id), tenant))
        row = cur.fetchone()
        if not row:
            raise ValueError("collection %s not found" % collection_id)
        matter_id = row[0]
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
    agg = {"collections": len(cids), "docs": 0, "linked": 0, "dry_run": dry_run}
    for cid in cids:
        r = canonicalize_collection(tenant, cid, dry_run=dry_run)
        agg["docs"] += r["docs"]
        agg["linked"] += r["linked"]
        agg["registry_size"] = r.get("registry_size")
    logger.info("canonicalize matter %s: %s", matter_id, agg)
    return agg


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collection")
    g.add_argument("--matter")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import json
    if args.collection:
        out = canonicalize_collection(args.tenant, args.collection, dry_run=args.dry_run)
    else:
        out = canonicalize_matter(args.tenant, args.matter, dry_run=args.dry_run)
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()
