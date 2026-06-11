"""ai_api_calls document-level provenance — Morgan v. V2X disclosure support

Adds document_id + document_source to ai_api_calls so every inference call
(local or cloud) can be tied to the specific document transmitted, not just
the matter. Enables one-query AI-use disclosures under protective orders:
which documents left the building, to which provider, when, and how much.

Nullable by design: chat/widget/case_seed purposes have no single document.
Historical rows stay NULL — honest provenance, no fabricated backfill.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0067_ai_call_document_provenance"
down_revision = "0066_rendition_path"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_api_calls",
                  sa.Column("document_id", UUID(as_uuid=True), nullable=True))
    op.add_column("ai_api_calls",
                  sa.Column("document_source", sa.String(40), nullable=True))
    # Partial index: the disclosure query filters on document_id IS NOT NULL
    # and groups by provider; most rows (chat, widgets) stay NULL forever.
    op.create_index(
        "ix_ai_api_calls_document",
        "ai_api_calls",
        ["document_id", "provider"],
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_ai_api_calls_document", table_name="ai_api_calls")
    op.drop_column("ai_api_calls", "document_source")
    op.drop_column("ai_api_calls", "document_id")
