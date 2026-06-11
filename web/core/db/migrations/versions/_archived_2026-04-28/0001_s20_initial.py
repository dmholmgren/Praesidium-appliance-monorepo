"""Series 2.0 initial schema — PostgreSQL 16

Drop-and-recreate migration. Database is empty (test data only).
Enables: pgvector, pg_trgm, btree_gin, uuid-ossp extensions.
Creates all Series 1.0 tables + all Series 2.0 additions.

Revision ID: 0001_s20_initial
Revises:
Create Date: 2026-03-29 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import (
    UUID,
    JSONB,
    ARRAY,
)

# ── Revision identifiers ─────────────────────────────────────────────────────
revision: str = "0001_s20_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extensions ───────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")

    # ── Series 1.0 Core Tables ───────────────────────────────────────────────

    op.create_table(
        "tenants",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("name", sa.VARCHAR(255), nullable=False),
        sa.Column("slug", sa.VARCHAR(100), nullable=False, unique=True),
        sa.Column("tier", sa.Enum("shared", "isolated", "dedicated", name="tenant_tier"), nullable=False),
        sa.Column("db_url", sa.TEXT),
        sa.Column("storage_adapter", sa.VARCHAR(50), nullable=False),
        sa.Column("auth_adapter", sa.VARCHAR(50), nullable=False),
        sa.Column("email_adapter", sa.VARCHAR(50), nullable=False),
        sa.Column("calendar_adapter", sa.VARCHAR(50), nullable=False),
        sa.Column("research_providers", sa.VARCHAR(100), nullable=False),
        sa.Column("ai_provider", sa.VARCHAR(50), server_default="anthropic"),
        sa.Column("plan", sa.VARCHAR(50), nullable=False),
        sa.Column("status", sa.Enum("active", "suspended", "cancelled", name="tenant_status"), server_default="active"),
        sa.Column("domain", sa.VARCHAR(255)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("external_id", sa.VARCHAR(255)),
        sa.Column("email", sa.VARCHAR(255), nullable=False),
        sa.Column("full_name", sa.VARCHAR(255), nullable=False),
        sa.Column("display_name", sa.VARCHAR(100)),
        sa.Column("role", sa.Enum("super_admin", "admin", "attorney", "paralegal", name="user_role"), nullable=False),
        sa.Column("billing_rate", sa.DECIMAL(10, 2)),
        sa.Column("bar_number", sa.VARCHAR(100)),
        sa.Column("is_active", sa.BOOLEAN, server_default="true"),
        sa.Column("password_hash", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_users_tenant_email", "users", ["tenant_id", "email"])
    op.create_index("idx_users_tenant_role", "users", ["tenant_id", "role"])

    op.create_table(
        "clients",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("client_name", sa.VARCHAR(500), nullable=False),
        sa.Column("client_type", sa.VARCHAR(50), server_default="entity"),
        sa.Column("address1", sa.VARCHAR(255)),
        sa.Column("address2", sa.VARCHAR(255)),
        sa.Column("city", sa.VARCHAR(100)),
        sa.Column("state", sa.VARCHAR(50)),
        sa.Column("zip_code", sa.VARCHAR(20)),
        sa.Column("country", sa.VARCHAR(50), server_default="US"),
        sa.Column("phone", sa.VARCHAR(50)),
        sa.Column("email", sa.VARCHAR(255)),
        sa.Column("notes", sa.TEXT),
        sa.Column("is_active", sa.BOOLEAN, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_clients_tenant", "clients", ["tenant_id"])

    op.create_table(
        "matters",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("matter_number", sa.VARCHAR(100), nullable=False),
        sa.Column("matter_name", sa.VARCHAR(500), nullable=False),
        sa.Column("practice_area", sa.VARCHAR(100)),
        sa.Column("status", sa.VARCHAR(50), server_default="active"),
        sa.Column("responsible_attorney_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("billing_method", sa.VARCHAR(50)),
        sa.Column("rate", sa.DECIMAL(10, 2)),
        sa.Column("retainer_amount", sa.DECIMAL(10, 2)),
        sa.Column("retainer_balance", sa.DECIMAL(10, 2)),
        sa.Column("notes", sa.TEXT),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_matters_tenant", "matters", ["tenant_id"])
    op.create_index("idx_matters_tenant_status", "matters", ["tenant_id", "status"])

    op.create_table(
        "documents",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("filename", sa.VARCHAR(500), nullable=False),
        sa.Column("original_filename", sa.VARCHAR(500)),
        sa.Column("mime_type", sa.VARCHAR(100)),
        sa.Column("file_size", sa.BIGINT),
        sa.Column("storage_path", sa.TEXT),
        sa.Column("document_type", sa.VARCHAR(100)),
        sa.Column("status", sa.VARCHAR(50), server_default="pending"),
        sa.Column("extracted_text", sa.TEXT),
        sa.Column("page_count", sa.INTEGER),
        sa.Column("embedding", sa.Text),   # stored as vector(1536) via raw SQL init
        sa.Column("metadata", JSONB),
        sa.Column("uploaded_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_documents_tenant_matter", "documents", ["tenant_id", "matter_id"])
    op.create_index("idx_documents_tenant_type", "documents", ["tenant_id", "document_type"])
    # GIN index for full-text search on extracted_text
    op.execute("CREATE INDEX idx_documents_fts ON documents USING gin(to_tsvector('english', coalesce(extracted_text,'')))")

    op.create_table(
        "time_entries",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("description", sa.TEXT, nullable=False),
        sa.Column("hours", sa.DECIMAL(6, 2), nullable=False),
        sa.Column("rate", sa.DECIMAL(10, 2)),
        sa.Column("date", sa.DATE, nullable=False),
        sa.Column("billable", sa.BOOLEAN, server_default="true"),
        sa.Column("status", sa.VARCHAR(50), server_default="draft"),
        sa.Column("invoice_id", UUID),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_time_entries_tenant_matter", "time_entries", ["tenant_id", "matter_id"])

    op.create_table(
        "invoices",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("invoice_number", sa.VARCHAR(100), nullable=False),
        sa.Column("status", sa.VARCHAR(50), server_default="draft"),
        sa.Column("subtotal", sa.DECIMAL(12, 2)),
        sa.Column("tax", sa.DECIMAL(12, 2)),
        sa.Column("total", sa.DECIMAL(12, 2)),
        sa.Column("due_date", sa.DATE),
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("notes", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "learning_signals",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id")),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("signal_type", sa.Enum(name="signal_type"), nullable=False),
        sa.Column("entity_type", sa.VARCHAR(100)),
        sa.Column("entity_id", UUID),
        sa.Column("payload", JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_learning_signals_tenant", "learning_signals", ["tenant_id"])
    op.create_index("idx_learning_signals_type", "learning_signals", ["signal_type"])

    op.create_table(
        "audit_log",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("user_id", UUID),
        sa.Column("action", sa.VARCHAR(200), nullable=False),
        sa.Column("entity_type", sa.VARCHAR(100)),
        sa.Column("entity_id", UUID),
        sa.Column("changes", JSONB),
        sa.Column("ip_address", sa.VARCHAR(50)),
        sa.Column("user_agent", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_audit_log_tenant", "audit_log", ["tenant_id"])
    op.create_index("idx_audit_log_entity", "audit_log", ["entity_type", "entity_id"])

    op.create_table(
        "tenant_branding",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        sa.Column("firm_name", sa.VARCHAR(255), nullable=False),
        sa.Column("base_domain", sa.VARCHAR(255), nullable=False),
        sa.Column("logo_url", sa.TEXT),
        sa.Column("primary_color", sa.VARCHAR(20)),
        sa.Column("secondary_color", sa.VARCHAR(20)),
        sa.Column("patent_notice", sa.TEXT),
        sa.Column("tagline", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── eDiscovery (Series 1.0 tables) ───────────────────────────────────────
    op.create_table(
        "ediscovery_collections",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("name", sa.VARCHAR(500), nullable=False),
        sa.Column("status", sa.VARCHAR(50), server_default="active"),
        sa.Column("issue_map", JSONB),          # DEPRECATED in S2.0 — use issue_map_versions
        sa.Column("document_count", sa.INTEGER, server_default="0"),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_ediscovery_collections_tenant_matter", "ediscovery_collections", ["tenant_id", "matter_id"])

    op.create_table(
        "ediscovery_documents",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("relevance_score", sa.FLOAT),
        sa.Column("review_status", sa.VARCHAR(50), server_default="unreviewed"),
        sa.Column("reviewed_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("privilege_status", sa.VARCHAR(50)),
        sa.Column("coding", JSONB),             # DEPRECATED in S2.0 — use document_tags
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ── Series 2.0 — Matter Knowledge Graph ──────────────────────────────────

    op.create_table(
        "matter_entities",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("entity_type", sa.TEXT, nullable=False),
        # person|company|contract|event|claim|defense|admission|contradiction|document|ruling|industry_fact
        sa.Column("name", sa.TEXT, nullable=False),
        sa.Column("properties", JSONB),
        sa.Column("source_doc_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("confidence", sa.FLOAT),
        sa.Column("created_by", sa.TEXT, nullable=False),  # 'ai' or user UUID
        sa.Column("version", sa.INTEGER, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_matter_entities_tenant_matter", "matter_entities", ["tenant_id", "matter_id"])
    op.create_index("idx_matter_entities_type", "matter_entities", ["tenant_id", "entity_type"])

    op.create_table(
        "matter_relationships",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, nullable=False),
        sa.Column("from_entity_id", UUID, sa.ForeignKey("matter_entities.id"), nullable=False),
        sa.Column("relationship", sa.TEXT, nullable=False),
        # employed_by|contradicts|supports|admitted_in|party_to|signed|testified_about
        sa.Column("to_entity_id", UUID, sa.ForeignKey("matter_entities.id"), nullable=False),
        sa.Column("source_doc_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("confidence", sa.FLOAT),
        sa.Column("created_by", sa.TEXT, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_matter_relationships_from", "matter_relationships", ["tenant_id", "from_entity_id"])
    op.create_index("idx_matter_relationships_to", "matter_relationships", ["tenant_id", "to_entity_id"])

    op.create_table(
        "entity_versions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("entity_id", UUID, sa.ForeignKey("matter_entities.id"), nullable=False),
        sa.Column("version", sa.INTEGER, nullable=False),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("changed_by", sa.TEXT, nullable=False),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("change_reason", sa.TEXT),
    )
    op.create_index("idx_entity_versions_entity", "entity_versions", ["entity_id"])

    # ── Series 2.0 — Issue Map Versioning and Drift ───────────────────────────

    op.create_table(
        "issue_map_versions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("version_number", sa.INTEGER, nullable=False),
        sa.Column("triggered_by", sa.TEXT, nullable=False),
        # manual|document_added|document_updated|scheduled
        sa.Column("source_documents", JSONB),
        sa.Column("issue_map", JSONB, nullable=False),
        sa.Column("diff_from_previous", JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_issue_map_versions_collection", "issue_map_versions", ["collection_id"])
    op.create_unique_constraint("uq_issue_map_collection_version", "issue_map_versions", ["collection_id", "version_number"])

    op.create_table(
        "case_drift_events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("drift_dimension", sa.TEXT, nullable=False),
        sa.Column("drift_type", sa.TEXT, nullable=False),
        sa.Column("description", sa.TEXT),
        sa.Column("source_document_id", UUID, sa.ForeignKey("documents.id")),
        sa.Column("issue_map_v_from", INTEGER_OR_NULL := sa.INTEGER),
        sa.Column("issue_map_v_to", sa.INTEGER),
        sa.Column("magnitude", sa.FLOAT),
        sa.Column("detected_by", sa.TEXT),        # 'ai' or user UUID
        sa.Column("confirmed_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_case_drift_events_collection", "case_drift_events", ["tenant_id", "collection_id"])

    op.create_table(
        "case_drift_parties",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("event_id", UUID, sa.ForeignKey("case_drift_events.id"), nullable=False),
        sa.Column("party", sa.TEXT, nullable=False),
        sa.Column("drift_source", sa.TEXT),
        sa.Column("party_theory_before", sa.TEXT),
        sa.Column("party_theory_after", sa.TEXT),
    )
    op.create_index("idx_case_drift_parties_event", "case_drift_parties", ["event_id"])

    # ── Series 2.0 — eDiscovery Multi-Pass Scoring ───────────────────────────

    op.create_table(
        "ai_review_passes",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("pass_number", sa.INTEGER, nullable=False),
        sa.Column("issue_map_version_id", UUID, sa.ForeignKey("issue_map_versions.id")),
        sa.Column("triggered_by", sa.TEXT, nullable=False),
        sa.Column("status", sa.VARCHAR(50), server_default="pending"),
        sa.Column("documents_scored", sa.INTEGER, server_default="0"),
        sa.Column("documents_changed", sa.INTEGER, server_default="0"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_ai_review_passes_collection", "ai_review_passes", ["tenant_id", "collection_id"])

    op.create_table(
        "document_review_scores",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("pass_id", UUID, sa.ForeignKey("ai_review_passes.id"), nullable=False),
        sa.Column("issue_map_version_id", UUID, sa.ForeignKey("issue_map_versions.id")),
        sa.Column("issue_key", sa.TEXT, nullable=False),
        sa.Column("relevance_score", sa.FLOAT),
        sa.Column("prior_score", sa.FLOAT),
        sa.Column("score_delta", sa.FLOAT),
        sa.Column("reasoning", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_document_review_scores_doc", "document_review_scores", ["tenant_id", "document_id"])

    # ── Series 2.0 — Case Intelligence / WIAM ────────────────────────────────

    op.create_table(
        "intelligence_sessions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("session_type", sa.TEXT, nullable=False),
        # wiam|drift_review|tag_suggestion|depo_analysis|pleading_review|industry_research|general
        sa.Column("status", sa.VARCHAR(50), server_default="active"),
        sa.Column("context_snapshot", JSONB),
        sa.Column("messages", JSONB, server_default="[]"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("idx_intelligence_sessions_tenant_matter", "intelligence_sessions", ["tenant_id", "matter_id"])

    op.create_table(
        "wiam_results",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("session_id", UUID, sa.ForeignKey("intelligence_sessions.id")),
        sa.Column("issue_map_version_id", UUID, sa.ForeignKey("issue_map_versions.id")),
        sa.Column("dimensions_analyzed", JSONB),
        sa.Column("total_findings", sa.INTEGER, server_default="0"),
        sa.Column("critical_findings", sa.INTEGER, server_default="0"),
        sa.Column("status", sa.VARCHAR(50), server_default="pending"),
        sa.Column("run_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("idx_wiam_results_tenant_matter", "wiam_results", ["tenant_id", "matter_id"])

    op.create_table(
        "wiam_findings",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("result_id", UUID, sa.ForeignKey("wiam_results.id"), nullable=False),
        sa.Column("finding_type", sa.TEXT, nullable=False),
        # gap|contradiction|inconsistency|opportunity|risk
        sa.Column("dimension", sa.TEXT, nullable=False),
        sa.Column("claim_element", sa.TEXT),
        sa.Column("citations", JSONB, nullable=False),
        # [{doc_id, page, line, excerpt_summary}] — required, no findings without citations
        sa.Column("confidence", sa.FLOAT),
        sa.Column("priority", sa.TEXT),           # critical|high|medium|low
        sa.Column("suggested_action", sa.TEXT),
        sa.Column("resolved", sa.BOOLEAN, server_default="false"),
        sa.Column("resolved_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("resolution_notes", sa.TEXT),
    )
    op.create_index("idx_wiam_findings_result", "wiam_findings", ["result_id"])
    op.create_index("idx_wiam_findings_priority", "wiam_findings", ["tenant_id", "priority"])

    # ── Series 2.0 — Depo Prep ───────────────────────────────────────────────

    op.create_table(
        "depo_prep_outlines",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("witness_entity_id", UUID, sa.ForeignKey("matter_entities.id")),
        sa.Column("witness_name", sa.TEXT, nullable=False),
        sa.Column("witness_role", sa.TEXT),
        sa.Column("outline", JSONB, nullable=False),
        sa.Column("source_documents", JSONB),
        sa.Column("issue_map_version_id", UUID, sa.ForeignKey("issue_map_versions.id")),
        sa.Column("generated_by", sa.TEXT, nullable=False),  # 'ai' or user UUID
        sa.Column("reviewed_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("status", sa.VARCHAR(50), server_default="draft"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_depo_prep_outlines_matter", "depo_prep_outlines", ["tenant_id", "matter_id"])

    # ── Series 2.0 — Tag System ───────────────────────────────────────────────
    # Replaces single coding JSONB field in ediscovery_documents.
    # AI tags and user tags are ALWAYS stored separately (source field enforces this).

    op.create_table(
        "tags",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id")),
        sa.Column("name", sa.TEXT, nullable=False),
        sa.Column("category", sa.TEXT),            # relevance|privilege|issue|custom
        sa.Column("description", sa.TEXT),
        sa.Column("color", sa.VARCHAR(20)),
        sa.Column("is_system", sa.BOOLEAN, server_default="false"),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_tags_tenant_collection", "tags", ["tenant_id", "collection_id"])

    op.create_table(
        "document_tags",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("tag_id", UUID, sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("source", sa.TEXT, nullable=False),   # 'ai' or 'user' — NEVER mix
        sa.Column("confidence", sa.FLOAT),              # AI tags only
        sa.Column("applied_by", UUID, sa.ForeignKey("users.id")),   # user tags only
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("issue_key", sa.TEXT),
        sa.Column("pass_id", UUID, sa.ForeignKey("ai_review_passes.id")),
    )
    op.create_index("idx_document_tags_document", "document_tags", ["tenant_id", "document_id"])
    op.create_index("idx_document_tags_source", "document_tags", ["tenant_id", "source"])
    # Prevent duplicate tags per source on same document
    op.create_unique_constraint(
        "uq_document_tag_source", "document_tags",
        ["document_id", "tag_id", "source"]
    )

    op.create_table(
        "tag_learning_signals",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("tag_id", UUID, sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("signal_type", sa.TEXT, nullable=False),
        # tag_confirmed|tag_rejected|tag_modified
        sa.Column("original_source", sa.TEXT),     # 'ai' or 'user'
        sa.Column("user_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("context", JSONB),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_tag_learning_signals_tag", "tag_learning_signals", ["tenant_id", "tag_id"])

    op.create_table(
        "tag_cooccurrence",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("tag_id_a", UUID, sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("tag_id_b", UUID, sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("cooccurrence_count", sa.INTEGER, server_default="0"),
        sa.Column("last_updated", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_tag_cooccurrence_tenant", "tag_cooccurrence", ["tenant_id"])

    op.create_table(
        "tag_quality_metrics",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("tag_id", UUID, sa.ForeignKey("tags.id"), nullable=False, unique=True),
        sa.Column("total_applications", sa.INTEGER, server_default="0"),
        sa.Column("ai_application_count", sa.INTEGER, server_default="0"),
        sa.Column("user_confirmation_count", sa.INTEGER, server_default="0"),
        sa.Column("human_confirmation_rate", sa.FLOAT),
        sa.Column("human_rejection_rate", sa.FLOAT),
        sa.Column("suggested_threshold", sa.FLOAT),
        sa.Column("last_updated", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    # ── Series 2.0 — Discovery Intelligence ──────────────────────────────────

    op.create_table(
        "discovery_postmortems",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("failure_type", sa.TEXT, nullable=False),
        sa.Column("description", sa.TEXT),
        sa.Column("contributing_factors", JSONB),
        sa.Column("outcome", sa.TEXT),
        sa.Column("prevention_notes", sa.TEXT),
        sa.Column("court", sa.TEXT),
        sa.Column("judge", sa.TEXT),
        sa.Column("opposing_counsel", sa.TEXT),
        sa.Column("submitted_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_discovery_postmortems_matter", "discovery_postmortems", ["tenant_id", "matter_id"])

    op.create_table(
        "production_records",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("collection_id", UUID, sa.ForeignKey("ediscovery_collections.id"), nullable=False),
        sa.Column("production_name", sa.TEXT, nullable=False),
        sa.Column("produced_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("bates_start", sa.TEXT),
        sa.Column("bates_end", sa.TEXT),
        sa.Column("document_count", sa.INTEGER, server_default="0"),
        sa.Column("format", sa.TEXT),
        sa.Column("notes", sa.TEXT),
        sa.Column("produced_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_production_records_collection", "production_records", ["tenant_id", "collection_id"])

    op.create_table(
        "bates_insertions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("document_id", UUID, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("production_record_id", UUID, sa.ForeignKey("production_records.id")),
        sa.Column("insertion_type", sa.TEXT),     # header|footer|overlay
        sa.Column("bates_ref", sa.TEXT, nullable=False),
        sa.Column("accepted", sa.BOOLEAN, server_default="false"),
        sa.Column("accepted_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_bates_insertions_document", "bates_insertions", ["tenant_id", "document_id"])

    # ── Series 2.0 — Cross-Matter Intelligence ───────────────────────────────

    op.create_table(
        "matter_intelligence_index",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False, unique=True),
        sa.Column("practice_area", sa.TEXT),
        sa.Column("claim_types", JSONB),           # list of claim type strings
        sa.Column("industry", sa.TEXT),
        sa.Column("opposing_counsel", JSONB),      # list of opposing counsel identifiers
        sa.Column("court", sa.TEXT),
        sa.Column("judge", sa.TEXT),
        sa.Column("outcome", sa.TEXT),
        sa.Column("key_arguments", JSONB),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_matter_intelligence_index_tenant", "matter_intelligence_index", ["tenant_id"])

    op.create_table(
        "cross_matter_signals",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", UUID, sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("signal_type", sa.TEXT, nullable=False),
        sa.Column("anonymized_data", JSONB, nullable=False),
        sa.Column("opted_out", sa.BOOLEAN, server_default="false"),
        sa.Column("contributed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_cross_matter_signals_tenant", "cross_matter_signals", ["tenant_id"])

    op.create_table(
        "global_learning_events",
        # RULE 14: This table MUST NEVER contain data traceable to a specific tenant or client.
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("signal_hash", sa.TEXT, nullable=False, unique=True),
        sa.Column("signal_type", sa.TEXT, nullable=False),
        sa.Column("contributed_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("tenant_anonymized_id", sa.TEXT, nullable=False),
        # HMAC of tenant_id with a platform key — NOT the real tenant_id
    )
    op.create_index("idx_global_learning_events_signal_type", "global_learning_events", ["signal_type"])

    # ── Series 2.0 — Licensing and Tenant Administration ─────────────────────

    op.create_table(
        "tenant_licenses",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False, unique=True),
        sa.Column("tier", sa.TEXT, nullable=False),
        # foundation|litigation|intelligence|enterprise
        sa.Column("feature_flags", JSONB, nullable=False, server_default="{}"),
        sa.Column("licensed_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("billing_plan", sa.TEXT),
        sa.Column("notes", sa.TEXT),
    )

    op.create_table(
        "feature_overrides",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("feature_flag", sa.TEXT, nullable=False, unique=True),
        sa.Column("enabled_globally", sa.BOOLEAN, nullable=False, server_default="true"),
        sa.Column("override_reason", sa.TEXT),
        sa.Column("set_by", sa.TEXT, nullable=False),    # Praesidium admin identifier
        sa.Column("set_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tenant_provisioning_log",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("action", sa.TEXT, nullable=False),
        # created|upgraded|downgraded|suspended|reactivated|deleted
        sa.Column("performed_by", sa.TEXT, nullable=False),
        sa.Column("prior_state", JSONB),
        sa.Column("new_state", JSONB),
        sa.Column("notes", sa.TEXT),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_tenant_provisioning_log_tenant", "tenant_provisioning_log", ["tenant_id"])

    op.create_table(
        "global_learning_opt_outs",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope", sa.TEXT, nullable=False),     # global|tenant|matter|attorney
        sa.Column("scope_id", sa.TEXT, nullable=False),  # tenant_id, matter_id, user_id as appropriate
        sa.Column("opted_out_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("opted_out_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("reason", sa.TEXT),
    )
    op.create_index("idx_global_learning_opt_outs_scope", "global_learning_opt_outs", ["scope", "scope_id"])

    # ── Vector column upgrade ────────────────────────────────────────────────
    # pgvector requires ALTER COLUMN — cannot be done via Column() directly in Alembic.
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding_vec vector(1536)")
    op.execute("ALTER TABLE matter_intelligence_index ADD COLUMN IF NOT EXISTS embedding vector(1536)")


def downgrade() -> None:
    # Drop all Series 2.0 tables in reverse dependency order
    op.drop_table("global_learning_opt_outs")
    op.drop_table("tenant_provisioning_log")
    op.drop_table("feature_overrides")
    op.drop_table("tenant_licenses")
    op.drop_table("global_learning_events")
    op.drop_table("cross_matter_signals")
    op.drop_table("matter_intelligence_index")
    op.drop_table("bates_insertions")
    op.drop_table("production_records")
    op.drop_table("discovery_postmortems")
    op.drop_table("tag_quality_metrics")
    op.drop_table("tag_cooccurrence")
    op.drop_table("tag_learning_signals")
    op.drop_table("document_tags")
    op.drop_table("tags")
    op.drop_table("depo_prep_outlines")
    op.drop_table("wiam_findings")
    op.drop_table("wiam_results")
    op.drop_table("intelligence_sessions")
    op.drop_table("document_review_scores")
    op.drop_table("ai_review_passes")
    op.drop_table("case_drift_parties")
    op.drop_table("case_drift_events")
    op.drop_table("issue_map_versions")
    op.drop_table("entity_versions")
    op.drop_table("matter_relationships")
    op.drop_table("matter_entities")
    op.drop_table("ediscovery_documents")
    op.drop_table("ediscovery_collections")
    op.drop_table("tenant_branding")
    op.drop_table("audit_log")
    op.drop_table("learning_signals")
    op.drop_table("invoices")
    op.drop_table("time_entries")
    op.drop_table("documents")
    op.drop_table("matters")
    op.drop_table("clients")
    op.drop_table("users")
    op.drop_table("tenants")

    # Drop enums
    op.execute("DROP TYPE IF EXISTS signal_type")
    op.execute("DROP TYPE IF EXISTS billing_status")
    op.execute("DROP TYPE IF EXISTS document_status")
    op.execute("DROP TYPE IF EXISTS matter_status")
    op.execute("DROP TYPE IF EXISTS user_role")
    op.execute("DROP TYPE IF EXISTS tenant_status")
    op.execute("DROP TYPE IF EXISTS tenant_tier")

    # Drop extensions
    op.execute("DROP EXTENSION IF EXISTS btree_gin")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
