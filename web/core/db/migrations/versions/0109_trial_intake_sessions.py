"""trial_intake_sessions — staging for AI-assisted trial document intake

A dropped trial document (exhibit / pleading / motion / transcript) is staged
here with its extracted text and the AI's running classification while the
intake chat decides what to do with it. On execute the row is marked done.

Revision ID: 0109_trial_intake_sessions
Revises: 0108_trial_center_nav
"""
from alembic import op

revision = "0109_trial_intake_sessions"
down_revision = "0108_trial_center_nav"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS trial_intake_sessions (
            id            uuid PRIMARY KEY,
            tenant_id     char(36) NOT NULL,
            matter_id     uuid,
            file_path     text NOT NULL,
            filename      text,
            mime_type     text,
            page_count    integer,
            extracted_text text,
            category      text,
            analysis      jsonb,
            status        text NOT NULL DEFAULT 'open',
            created_by    text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trial_intake_tenant_matter "
        "ON trial_intake_sessions (tenant_id, matter_id)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS trial_intake_sessions")
