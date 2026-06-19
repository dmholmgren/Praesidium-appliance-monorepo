"""0129: standalone firm PST/email archive collector (Lane B / B1).

A SEPARATE tenant-admin collection point, intentionally independent of the
eDiscovery pipeline (which stays untouched). PST uploads are extracted with
pffexport, parsed into per-message rows in `pst_messages`, deduped by
normalized_hash, tracked per upload in `pst_import_batches`, and made searchable
via the shared email_chunks / email_chunk_embeddings vector base
(source_type='pst_message').
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0129_pst_import"
down_revision = "0128_meeting_ws_calendar_fk"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pst_import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger()),
        sa.Column("source_filename", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger()),
        # 'firm_archive' = global dedup; 'custodian' = custodian-scoped dedup
        sa.Column("routing_tag", sa.String(length=20), nullable=False,
                  server_default=sa.text("'firm_archive'")),
        sa.Column("custodian_label", sa.String(length=255)),
        # pending | extracting | parsing | embedding | done | error
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("total_messages", sa.Integer(), server_default=sa.text("0")),
        sa.Column("imported_messages", sa.Integer(), server_default=sa.text("0")),
        sa.Column("duplicate_messages", sa.Integer(), server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_pst_import_batches_tenant", "pst_import_batches",
                    ["tenant_id", "status"])

    op.create_table(
        "pst_messages",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64)),
        sa.Column("message_path", sa.Text()),
        sa.Column("from_email", sa.Text()),
        sa.Column("from_display", sa.Text()),
        sa.Column("to_emails", sa.Text()),
        sa.Column("cc_emails", sa.Text()),
        sa.Column("subject", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("body_text", sa.Text()),
        sa.Column("has_attachments", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("attachment_names", postgresql.JSONB()),
        sa.Column("internet_message_id", sa.Text()),
        sa.Column("conversation_topic", sa.Text()),
        sa.Column("matter_id", postgresql.UUID(as_uuid=False)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["batch_id"], ["pst_import_batches.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_pst_messages_batch", "pst_messages", ["batch_id"])
    op.create_index("ix_pst_messages_hash", "pst_messages",
                    ["tenant_id", "normalized_hash"])
    op.create_index("ix_pst_messages_matter", "pst_messages", ["matter_id"])


def downgrade():
    op.drop_table("pst_messages")
    op.drop_table("pst_import_batches")
