"""0048 processing_thresholds: tenant/matter-tunable pipeline gates.

Pulls the hardcoded confidence/score gates out of the processing scripts into a
config table mirroring ai_model_routing (nullable tenant_id = global default;
version/status for draft->published governance) plus a matter_id for the
matter > tenant > global override cascade. Scripts read effective values later
via a get_threshold()/get_thresholds() accessor; this revision only creates the
table and seeds the known defaults so there is something to read on day one.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0048_processing_thresholds"
down_revision = "0047_merge_46_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "processing_thresholds",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), sa.ForeignKey("tenants.id"), nullable=True, index=True),   # NULL = global default
        sa.Column("matter_id", UUID(as_uuid=True), sa.ForeignKey("matters.id"), nullable=True),         # NULL = tenant/global; set = per-matter override
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("threshold_key", sa.String(64), nullable=False),
        sa.Column("value", sa.Numeric(12, 6), nullable=False),
        sa.Column("min_value", sa.Numeric(12, 6), nullable=True),
        sa.Column("max_value", sa.Numeric(12, 6), nullable=True),
        sa.Column("step", sa.Numeric(12, 6), nullable=True),
        sa.Column("unit", sa.String(24), nullable=True),            # confidence|percent|count|days
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="published"),  # draft|published|deprecated
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Index("ix_processing_thresholds_lookup", "module", "purpose", "threshold_key", "status"),
    )

    # One published row per (cascade-level, module, purpose, key). COALESCE so NULL
    # tenant/matter don't read as "distinct" (the gap in ai_model_routing's plain
    # nullable unique index) -- duplicate globals can't sneak in.
    op.execute(
        "CREATE UNIQUE INDEX uq_processing_thresholds_published "
        "ON processing_thresholds "
        "(COALESCE(tenant_id::text, '~global'), COALESCE(matter_id::text, '~all'), "
        " module, purpose, threshold_key) "
        "WHERE status = 'published'"
    )

    # Seed known gates as global defaults (tenant_id/matter_id NULL). Values mirror
    # the current script literals so behavior is unchanged until someone tunes them.
    op.execute("""
        INSERT INTO processing_thresholds
            (module, purpose, threshold_key, value, min_value, max_value, step, unit, description)
        VALUES
            ('intelligence','extraction','escalate_to_ai', 0.75, 0, 1, 0.01, 'confidence',
                'Below this classifier confidence, escalate the document to the AI extraction route.'),
            ('intelligence','extraction','human_review',   0.70, 0, 1, 0.01, 'confidence',
                'Below this confidence, flag the extraction for human review.'),
            ('onboarding','folder_match','auto_accept',     0.95, 0, 1, 0.01, 'confidence',
                'At or above this match score, auto-accept the folder->matter proposal.'),
            ('onboarding','folder_match','confident',       0.65, 0, 1, 0.01, 'confidence',
                'At or above this score, treat the folder->matter match as confident.'),
            ('onboarding','folder_match','review_floor',    0.50, 0, 1, 0.01, 'confidence',
                'Below this score, do not propose; route to eDiscovery/orphan alert.'),
            ('ediscovery','ocr','low_conf_word_pct',        0.15, 0, 1, 0.01, 'percent',
                'Above this fraction of low-confidence words, flag the page for OCR review.')
    """)


def downgrade():
    op.drop_table("processing_thresholds")
