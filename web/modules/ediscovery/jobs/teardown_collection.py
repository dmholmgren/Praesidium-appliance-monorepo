"""
teardown_collection.py — safely remove an eDiscovery collection's data.

Reusable backend for the "Remove" action on FAILED / mis-ingested collections
in the collections status view. Deletes all derived rows (chunk embeddings,
chunks, sections, geometry, stage ledger) then the documents themselves, in
small COMMITTED batches so it never holds long locks or detonates a single
giant transaction — a mis-fanned load-file production can leave 10k+ wide
document rows (each with ~18 indexes), which a one-shot delete rolls back on
the slightest interruption. Optionally drops the collection row.

On-disk storage reclamation is intentionally NOT done here (recursive
filesystem removal is privilege-gated); call reclaim separately/explicitly
after the rows are gone if you also want the bytes back.

Idempotent: safe to re-run; only deletes what remains. Scoping is by
collection_id (a globally-unique uuid); tenant_id is accepted for symmetry
and optional assertion.

CLI:
  python -m modules.ediscovery.jobs.teardown_collection \
      --collection <uuid> [--tenant <uuid>] \
      [--drop-collection] [--require-failed] [--batch 500]
"""
import os
import logging
import psycopg2
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _connect():
    raw = os.environ.get("DATABASE_URL", "")
    for pref in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(pref):
            raw = "postgresql://" + raw[len(pref):]
            break
    u = urlparse(raw)
    return psycopg2.connect(
        dbname=u.path.lstrip("/") or "praesidium",
        user=u.username, password=u.password,
        host=u.hostname or "127.0.0.1", port=u.port or 5432,
    )


def delete_ediscovery_collection(tenant_id, collection_id, *,
                                 drop_collection=False, require_failed=False,
                                 batch=500):
    """Tear down a collection's derived data (and optionally its row).

    Returns a dict of deleted counts (and 'error' if it refused).

    drop_collection  also delete the ediscovery_collections row.
    require_failed   refuse unless status='failed' (guardrail for the UI
                     "remove failed collection" action).
    batch            document delete batch size (committed per batch).
    """
    cid = str(collection_id)
    conn = _connect()
    counts = {"collection_id": cid}
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, TRIM(tenant_id) "
                    "FROM ediscovery_collections WHERE id=%s", (cid,))
        row = cur.fetchone()
        if not row:
            return {"collection_id": cid, "error": "collection not found"}
        status, owner_tenant = row
        if tenant_id and owner_tenant and owner_tenant != str(tenant_id).strip():
            return {"collection_id": cid, "error": "tenant mismatch"}
        if require_failed and status != "failed":
            return {"collection_id": cid,
                    "error": "refusing: status=%s (not 'failed')" % status}

        D = "(SELECT id FROM ediscovery_documents WHERE collection_id=%s)"
        cur.execute("DELETE FROM ediscovery_chunk_embeddings WHERE chunk_id IN "
                    "(SELECT id FROM ediscovery_chunks WHERE document_id IN %s)" % D, (cid,))
        counts["chunk_embeddings"] = cur.rowcount
        conn.commit()
        cur.execute("DELETE FROM ediscovery_chunks WHERE document_id IN %s" % D, (cid,))
        counts["chunks"] = cur.rowcount
        conn.commit()
        cur.execute("DELETE FROM document_sections WHERE ediscovery_document_id IN %s" % D, (cid,))
        counts["sections"] = cur.rowcount
        conn.commit()
        cur.execute("DELETE FROM doc_geometry WHERE corpus='ediscovery' AND doc_id IN %s" % D, (cid,))
        counts["geometry"] = cur.rowcount
        conn.commit()
        cur.execute("DELETE FROM ediscovery_stage_status WHERE collection_id=%s", (cid,))
        counts["stage_status"] = cur.rowcount
        conn.commit()

        # Documents in committed batches: wide rows + many indexes make a single
        # bulk delete slow and rollback-prone. Per-batch commit keeps progress.
        docs = 0
        while True:
            cur.execute(
                "DELETE FROM ediscovery_documents WHERE id IN "
                "(SELECT id FROM ediscovery_documents WHERE collection_id=%s LIMIT %s)",
                (cid, batch))
            n = cur.rowcount
            conn.commit()
            docs += n
            if n == 0:
                break
        counts["documents"] = docs

        if drop_collection:
            cur.execute("DELETE FROM ediscovery_collections WHERE id=%s", (cid,))
            counts["collection_dropped"] = cur.rowcount
            conn.commit()

        logger.info("teardown %s: %s", cid, counts)
        return counts
    finally:
        conn.close()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True)
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID"))
    ap.add_argument("--drop-collection", action="store_true")
    ap.add_argument("--require-failed", action="store_true")
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args()
    print(delete_ediscovery_collection(
        a.tenant, a.collection,
        drop_collection=a.drop_collection,
        require_failed=a.require_failed, batch=a.batch))


if __name__ == "__main__":
    main()
