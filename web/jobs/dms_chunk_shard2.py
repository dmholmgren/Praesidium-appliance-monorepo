import uuid
def run_shard(tenant, doc_ids, seg_run=None):
    import psycopg2
    from jobs.canonical_segmenter import process_doc as seg, get_db_url
    from jobs.spine_chunker import process_doc as chunk
    conn = psycopg2.connect(get_db_url()); conn.autocommit = False; cur = conn.cursor()
    sr = seg_run or str(uuid.uuid4())
    cur.execute("INSERT INTO extraction_runs (id,tenant_id,run_type,source_type,extraction_model,status,started_at) "
                "VALUES (%s::uuid,%s,'canonical_segment','dms','shard2','running',NOW()) ON CONFLICT (id) DO NOTHING", (sr, tenant)); conn.commit()
    s = {'seg': 0, 'chunked': 0, 'chunks': 0, 'drift': 0, 'err': 0, 'n': len(doc_ids)}
    for i, d in enumerate(doc_ids):
        cur.execute('SAVEPOINT sp')
        try:
            rs = seg(cur, 'dms', tenant, d, sr, False)            # ALWAYS re-segment (supersedes stale -> fixes drift)
            if rs.get('status') == 'written': s['seg'] += 1
            rc = chunk(cur, 'dms', tenant, d, False, False)
            if rc.get('status') == 'written': s['chunked'] += 1; s['chunks'] += rc.get('chunks', 0)
            elif rc.get('status') == 'chunk_errors': s['drift'] += 1
            cur.execute('RELEASE SAVEPOINT sp'); conn.commit()
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT sp'); conn.commit(); s['err'] += 1
    cur.execute("UPDATE extraction_runs SET status='completed',completed_at=NOW() WHERE id=%s::uuid", (sr,)); conn.commit()
    conn.close(); return s
