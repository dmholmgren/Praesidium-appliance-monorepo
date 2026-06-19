import uuid
def run_shard(tenant, doc_ids, seg_run=None):
    import psycopg2
    from jobs.canonical_segmenter import process_doc as seg_doc, get_db_url
    from jobs.spine_chunker import process_doc as chunk_doc
    conn = psycopg2.connect(get_db_url()); conn.autocommit = False; cur = conn.cursor()
    sr = seg_run or str(uuid.uuid4())
    cur.execute("INSERT INTO extraction_runs (id,tenant_id,run_type,source_type,extraction_model,status,started_at) "
                "VALUES (%s::uuid,%s,'canonical_segment','dms','shard','running',NOW()) ON CONFLICT (id) DO NOTHING",
                (sr, tenant)); conn.commit()
    s = {"seg_new": 0, "seg_skip": 0, "chunked_docs": 0, "chunks": 0, "err": 0, "n": len(doc_ids)}
    for i, d in enumerate(doc_ids):
        cur.execute('SAVEPOINT sp')
        try:
            cur.execute("SELECT 1 FROM document_sections WHERE dms_document_id=%s::uuid AND superseded_by_run_id IS NULL LIMIT 1", (d,))
            if cur.fetchone():
                s["seg_skip"] += 1
            else:
                rs = seg_doc(cur, 'dms', tenant, d, sr, False)
                if rs.get('status') == 'written': s["seg_new"] += 1
            rc = chunk_doc(cur, 'dms', tenant, d, False, False)
            if rc.get('status') == 'written':
                s["chunked_docs"] += 1; s["chunks"] += rc.get('chunks', 0)
            cur.execute('RELEASE SAVEPOINT sp')
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT sp'); s["err"] += 1
        if (i + 1) % 100 == 0: conn.commit()
    cur.execute("UPDATE extraction_runs SET status='completed',completed_at=NOW() WHERE id=%s::uuid", (sr,))
    conn.commit(); conn.close()
    return s
