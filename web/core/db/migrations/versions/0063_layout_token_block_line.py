"""0063_layout_token_block_line — file-complete bridge 0053 -> 0063.

Consolidates the schema applied via run-scripts between 0053 and the live head
(0059 embedding_768, 0060 production share links, 0061 custodian_source,
0062 custodian_registry, 0063 layout-token block/line) PLUS the orphaned
doc_layout_tokens typography columns (font_size/is_bold/is_italic/font_name,
added ad-hoc 2026-06-09 with no migration) AND the ad-hoc doc_geometry canonical
header table (geometry thread, never migrated). Every statement is idempotent
(IF [NOT] EXISTS / DROP-then-ADD), so this is a no-op on the live DB (already
stamped 0063) and rebuilds the whole gap on a fresh `alembic upgrade head`.

The env runs on asyncpg -> ONE statement per op.execute().

Revision ID: 0063_layout_token_block_line
Revises: 0053_pii_review_decisions
"""
from alembic import op

revision = "0063_layout_token_block_line"
down_revision = "0053_pii_review_decisions"
branch_labels = None
depends_on = None


UPGRADE_STMTS = [
    # ── 0059: ediscovery_chunk_embeddings ModernBERT-768 ──
    "ALTER TABLE ediscovery_chunk_embeddings ADD COLUMN IF NOT EXISTS embedding_768 vector(768)",
    ("CREATE INDEX IF NOT EXISTS ix_edr_chunk_embeddings_768_hnsw "
     "ON ediscovery_chunk_embeddings USING hnsw (embedding_768 vector_cosine_ops) "
     "WHERE embedding_768 IS NOT NULL"),
    "ALTER TABLE ediscovery_chunk_embeddings DROP CONSTRAINT IF EXISTS ck_edr_chunk_embeddings_exactly_one_dim",
    ("ALTER TABLE ediscovery_chunk_embeddings ADD CONSTRAINT ck_edr_chunk_embeddings_exactly_one_dim "
     "CHECK ((CASE WHEN embedding_768 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_1024 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_1536 IS NOT NULL THEN 1 ELSE 0 END "
     "      + CASE WHEN embedding_3072 IS NOT NULL THEN 1 ELSE 0 END) = 1)"),

    # ── 0060: production share links + access log ──
    ("CREATE TABLE IF NOT EXISTS production_share_links ("
     " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
     " tenant_id varchar(36) NOT NULL,"
     " production_set_id uuid NOT NULL REFERENCES production_sets(id) ON DELETE CASCADE,"
     " token varchar(128) NOT NULL,"
     " recipient_name varchar(255), recipient_email varchar(255), message text,"
     " expires_at timestamptz,"
     " require_registration boolean NOT NULL DEFAULT true,"
     " access_password_hash varchar(255), max_downloads integer,"
     " download_count integer NOT NULL DEFAULT 0,"
     " is_revoked boolean NOT NULL DEFAULT false,"
     " created_by bigint,"
     " created_at timestamptz NOT NULL DEFAULT now(),"
     " updated_at timestamptz NOT NULL DEFAULT now())"),
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_share_links_token ON production_share_links (token)",
    "CREATE INDEX IF NOT EXISTS idx_share_links_production ON production_share_links (production_set_id)",
    "CREATE INDEX IF NOT EXISTS idx_share_links_tenant ON production_share_links (tenant_id)",
    ("CREATE TABLE IF NOT EXISTS production_share_access_log ("
     " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
     " tenant_id varchar(36) NOT NULL,"
     " share_link_id uuid NOT NULL REFERENCES production_share_links(id) ON DELETE CASCADE,"
     " action varchar(50) NOT NULL,"
     " visitor_name varchar(255), visitor_email varchar(255), visitor_firm varchar(255),"
     " ip_address varchar(45), user_agent text,"
     " accessed_at timestamptz NOT NULL DEFAULT now(),"
     " metadata_json jsonb)"),
    "CREATE INDEX IF NOT EXISTS idx_share_access_link ON production_share_access_log (share_link_id)",
    "CREATE INDEX IF NOT EXISTS idx_share_access_tenant ON production_share_access_log (tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_share_access_time ON production_share_access_log (accessed_at)",

    # ── 0061: ediscovery_documents.custodian_source ──
    "ALTER TABLE ediscovery_documents ADD COLUMN IF NOT EXISTS custodian_source varchar(32)",
    ("CREATE INDEX IF NOT EXISTS ix_edr_docs_custodian_unresolved "
     "ON ediscovery_documents (tenant_id) WHERE custodian_source IS NULL"),

    # ── 0062: ediscovery_custodians registry + doc link ──
    ("CREATE TABLE IF NOT EXISTS ediscovery_custodians ("
     " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
     " tenant_id varchar(36) NOT NULL, matter_id uuid NOT NULL,"
     " canonical_name varchar(255) NOT NULL, normalized_key varchar(255) NOT NULL,"
     " aliases jsonb NOT NULL DEFAULT '[]'::jsonb,"
     " emails jsonb NOT NULL DEFAULT '[]'::jsonb,"
     " created_at timestamptz NOT NULL DEFAULT now(),"
     " updated_at timestamptz NOT NULL DEFAULT now(),"
     " CONSTRAINT uq_edr_custodian_key UNIQUE (tenant_id, matter_id, normalized_key))"),
    "CREATE INDEX IF NOT EXISTS ix_edr_custodians_matter ON ediscovery_custodians (tenant_id, matter_id)",
    "CREATE INDEX IF NOT EXISTS ix_edr_custodians_emails ON ediscovery_custodians USING gin (emails)",
    "ALTER TABLE ediscovery_documents ADD COLUMN IF NOT EXISTS custodian_id uuid",
    "CREATE INDEX IF NOT EXISTS ix_edr_docs_custodian_id ON ediscovery_documents (custodian_id)",

    # ── geometry typography (orphaned ad-hoc 2026-06-09) ──
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS font_size numeric",
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS is_bold boolean",
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS is_italic boolean",
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS font_name text",

    # ── 0063: doc_layout_tokens fitz block/line grouping ──
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS block_no integer",
    "ALTER TABLE doc_layout_tokens ADD COLUMN IF NOT EXISTS line_no integer",

    # ── doc_geometry canonical header (ad-hoc geometry thread, never migrated) ──
    ("CREATE TABLE IF NOT EXISTS doc_geometry ("
     " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
     " tenant_id varchar(36) NOT NULL,"
     " corpus text NOT NULL,"
     " doc_id uuid NOT NULL,"
     " rendition text NOT NULL DEFAULT 'native_pdf',"
     " canonical_text text NOT NULL,"
     " page_count integer NOT NULL,"
     " char_count integer NOT NULL,"
     " token_count integer NOT NULL,"
     " has_text_layer boolean NOT NULL DEFAULT true,"
     " source text NOT NULL DEFAULT 'pdf_textlayer',"
     " extraction_model text,"
     " built_at timestamptz NOT NULL DEFAULT now())"),
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_doc_geometry_doc ON doc_geometry (corpus, doc_id, rendition)",
    "CREATE INDEX IF NOT EXISTS ix_doc_geometry_tenant ON doc_geometry (tenant_id)",

    # ── dms_chunks 768 HNSW (folds 0044 DEFERRED_HNSW idx_dms_emb_768_hnsw;
    #    built at runtime by embed_dms_chunks, now declarative for fresh tenants) ──
    ("CREATE INDEX IF NOT EXISTS idx_dms_emb_768_hnsw "
     "ON dms_chunk_embeddings USING hnsw (embedding_768 vector_cosine_ops) "
     "WHERE embedding_768 IS NOT NULL"),
]


def upgrade():
    for st in UPGRADE_STMTS:
        op.execute(st)


def downgrade():
    # Conservative: reverse only THIS session's doc_layout_tokens additions.
    # Bridged objects (embeddings/share-links/custodians) are left intact to
    # avoid destroying data created by their original run-scripts.
    for st in [
        "ALTER TABLE doc_layout_tokens DROP COLUMN IF EXISTS line_no",
        "ALTER TABLE doc_layout_tokens DROP COLUMN IF EXISTS block_no",
    ]:
        op.execute(st)
