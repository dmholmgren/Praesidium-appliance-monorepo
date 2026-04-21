"""
Pass 1 Schema Fix — Praesidium Series 2.0
Adds all missing columns to existing tables.
Run: docker exec praesidium-web python3 /tmp/fix_schema_p1.py
"""
import os
import psycopg2

url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', '')
at = url.rfind('@')
credentials = url[:at]
hostpart = url[at+1:]
colon = credentials.index(':')
user = credentials[:colon]
password = credentials[colon+1:]
slash = hostpart.index('/')
hostport = hostpart[:slash]
dbname = hostpart[slash+1:].split('?')[0]
host = hostport.rsplit(':', 1)[0] if ':' in hostport else hostport

conn = psycopg2.connect(host=host, port=5432, dbname=dbname, user=user, password=password)
conn.autocommit = True
cur = conn.cursor()

fixes = []

# ── matters ───────────────────────────────────────────────────────────────────
fixes.append(("matters", """
    ADD COLUMN IF NOT EXISTS billing_type VARCHAR(11),
    ADD COLUMN IF NOT EXISTS cause_number VARCHAR(100),
    ADD COLUMN IF NOT EXISTS close_date DATE,
    ADD COLUMN IF NOT EXISTS contingency_pct NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS court VARCHAR(255),
    ADD COLUMN IF NOT EXISTS flat_fee_amount NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS folder_path VARCHAR(1000),
    ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS judge VARCHAR(255),
    ADD COLUMN IF NOT EXISTS jurisdiction VARCHAR(100),
    ADD COLUMN IF NOT EXISTS legacy_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS matter_type VARCHAR(15),
    ADD COLUMN IF NOT EXISTS open_date DATE,
    ADD COLUMN IF NOT EXISTS originating_attorney_id BIGINT,
    ADD COLUMN IF NOT EXISTS sol_date DATE
"""))

# ── documents ─────────────────────────────────────────────────────────────────
fixes.append(("documents", """
    ADD COLUMN IF NOT EXISTS checksum VARCHAR(64),
    ADD COLUMN IF NOT EXISTS created_by BIGINT,
    ADD COLUMN IF NOT EXISTS doc_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS file_name VARCHAR(500),
    ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS ocr_status VARCHAR(10),
    ADD COLUMN IF NOT EXISTS ocr_text TEXT,
    ADD COLUMN IF NOT EXISTS parent_doc_id BIGINT,
    ADD COLUMN IF NOT EXISTS title VARCHAR(500),
    ADD COLUMN IF NOT EXISTS version_number INTEGER DEFAULT 1
"""))

# ── ediscovery_documents ──────────────────────────────────────────────────────
fixes.append(("ediscovery_documents", """
    ADD COLUMN IF NOT EXISTS bates_end VARCHAR(50),
    ADD COLUMN IF NOT EXISTS bates_start VARCHAR(50),
    ADD COLUMN IF NOT EXISTS bates_begin VARCHAR(50),
    ADD COLUMN IF NOT EXISTS coding_notes TEXT,
    ADD COLUMN IF NOT EXISTS custodian VARCHAR(255),
    ADD COLUMN IF NOT EXISTS dms_document_id BIGINT,
    ADD COLUMN IF NOT EXISTS doc_date DATE,
    ADD COLUMN IF NOT EXISTS doc_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS doc_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS dupe_of_id BIGINT,
    ADD COLUMN IF NOT EXISTS duplicate_of_id BIGINT,
    ADD COLUMN IF NOT EXISTS email_cc TEXT,
    ADD COLUMN IF NOT EXISTS email_date TIMESTAMP,
    ADD COLUMN IF NOT EXISTS email_from VARCHAR(500),
    ADD COLUMN IF NOT EXISTS email_in_reply_to VARCHAR(500),
    ADD COLUMN IF NOT EXISTS email_message_id VARCHAR(500),
    ADD COLUMN IF NOT EXISTS email_references TEXT,
    ADD COLUMN IF NOT EXISTS email_subject VARCHAR(1000),
    ADD COLUMN IF NOT EXISTS email_thread_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS email_to TEXT,
    ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS file_name VARCHAR(500),
    ADD COLUMN IF NOT EXISTS file_path VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS file_size BIGINT,
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_near_duplicate BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mime_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS near_dupe_of_id BIGINT,
    ADD COLUMN IF NOT EXISTS near_dupe_score NUMERIC(5,4),
    ADD COLUMN IF NOT EXISTS original_path VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS page_count BIGINT,
    ADD COLUMN IF NOT EXISTS produced_in VARCHAR(100),
    ADD COLUMN IF NOT EXISTS relevance_breakdown JSONB,
    ADD COLUMN IF NOT EXISTS review_tier VARCHAR(8),
    ADD COLUMN IF NOT EXISTS tar_score NUMERIC(5,4),
    ADD COLUMN IF NOT EXISTS working_path VARCHAR(2000)
"""))

# ── ediscovery_collections ────────────────────────────────────────────────────
fixes.append(("ediscovery_collections", """
    ADD COLUMN IF NOT EXISTS collection_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS dms_source_path VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS original_file_name VARCHAR(500),
    ADD COLUMN IF NOT EXISTS original_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS processed_docs BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS received_by BIGINT,
    ADD COLUMN IF NOT EXISTS received_date DATE,
    ADD COLUMN IF NOT EXISTS received_method VARCHAR(255),
    ADD COLUMN IF NOT EXISTS reviewed_docs BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source_party VARCHAR(500),
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(27),
    ADD COLUMN IF NOT EXISTS stated_bates_range VARCHAR(255),
    ADD COLUMN IF NOT EXISTS storage_path VARCHAR(2000),
    ADD COLUMN IF NOT EXISTS total_docs BIGINT DEFAULT 0
"""))

# ── invoices ──────────────────────────────────────────────────────────────────
fixes.append(("invoices", """
    ADD COLUMN IF NOT EXISTS amount_paid NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS balance_due NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS billing_type VARCHAR(11),
    ADD COLUMN IF NOT EXISTS client_id BIGINT,
    ADD COLUMN IF NOT EXISTS invoice_date DATE,
    ADD COLUMN IF NOT EXISTS lawpay_invoice_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS ledes_data JSONB,
    ADD COLUMN IF NOT EXISTS pdf_path VARCHAR(1000),
    ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(10,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_amount NUMERIC(12,2) DEFAULT 0
"""))

# ── time_entries ──────────────────────────────────────────────────────────────
fixes.append(("time_entries", """
    ADD COLUMN IF NOT EXISTS ai_confidence NUMERIC(3,2),
    ADD COLUMN IF NOT EXISTS ai_original_description TEXT,
    ADD COLUMN IF NOT EXISTS amount NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS entry_date DATE,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reviewed_by BIGINT,
    ADD COLUMN IF NOT EXISTS source VARCHAR(12),
    ADD COLUMN IF NOT EXISTS source_metadata JSONB,
    ADD COLUMN IF NOT EXISTS utbms_code VARCHAR(20)
"""))

# ── audit_log ─────────────────────────────────────────────────────────────────
fixes.append(("audit_log", """
    ADD COLUMN IF NOT EXISTS new_values JSONB,
    ADD COLUMN IF NOT EXISTS old_values JSONB,
    ADD COLUMN IF NOT EXISTS record_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(36),
    ADD COLUMN IF NOT EXISTS table_name VARCHAR(100)
"""))

# ── learning_signals ──────────────────────────────────────────────────────────
fixes.append(("learning_signals", """
    ADD COLUMN IF NOT EXISTS context JSONB,
    ADD COLUMN IF NOT EXISTS module VARCHAR(50),
    ADD COLUMN IF NOT EXISTS new_value JSONB,
    ADD COLUMN IF NOT EXISTS old_value JSONB
"""))

# ── Run all fixes ─────────────────────────────────────────────────────────────
print("=" * 70)
print("Praesidium Series 2.0 — Pass 1 Schema Fix")
print("=" * 70)

total_fixed = 0
total_errors = 0

for table, columns_sql in fixes:
    try:
        cur.execute(f"ALTER TABLE {table} {columns_sql}")
        print(f"  ✅ {table}")
        total_fixed += 1
    except Exception as e:
        print(f"  ❌ {table}: {e}")
        total_errors += 1

# ── Verify ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("Verification — spot check key columns:")

checks = [
    ("matters", "billing_type"),
    ("matters", "sol_date"),
    ("documents", "checksum"),
    ("documents", "title"),
    ("ediscovery_documents", "bates_start"),
    ("ediscovery_documents", "doc_hash"),
    ("invoices", "total_amount"),
    ("invoices", "invoice_date"),
    ("time_entries", "entry_date"),
    ("audit_log", "table_name"),
]

for table, col in checks:
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
    """, (table, col))
    exists = cur.fetchone()[0] > 0
    status = "✅" if exists else "❌ MISSING"
    print(f"  {status}  {table}.{col}")

print("\n" + "=" * 70)
print(f"Tables fixed: {total_fixed}")
print(f"Errors: {total_errors}")
print("\nDone. Restart container to clear connection pool.")

cur.close()
conn.close()
