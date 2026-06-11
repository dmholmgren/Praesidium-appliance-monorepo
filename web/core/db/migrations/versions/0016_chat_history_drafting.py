"""Chat history persistence and drafting staging.

chat_sessions:       One row per AI chat conversation.
chat_messages:       Every user and assistant message, with tool calls.
drafting_outputs:    Staged draft files (chats/ dir) before DMS promotion.

Revision ID: 0016_chat_history_drafting
Revises: 0015_tk_assignments
"""
from alembic import op
import sqlalchemy as sa

revision = '0016_chat_history_drafting'
down_revision = '0015_tk_assignments'


def upgrade():
    # ── Chat Sessions ───────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            user_id         BIGINT,
            title           TEXT,
            context_page    TEXT,                    -- 'drafting', 'dms', 'billing', etc.
            matter_id       UUID REFERENCES matters(id),
            document_type   TEXT,
            status          TEXT NOT NULL DEFAULT 'active',  -- active, archived, cleared
            message_count   INT NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cleared_at      TIMESTAMPTZ              -- when user clears history
        );

        CREATE INDEX IF NOT EXISTS ix_chat_sessions_tenant_user
            ON chat_sessions(tenant_id, user_id);
        CREATE INDEX IF NOT EXISTS ix_chat_sessions_matter
            ON chat_sessions(matter_id);
        CREATE INDEX IF NOT EXISTS ix_chat_sessions_updated
            ON chat_sessions(tenant_id, updated_at DESC);
    """)

    # ── Chat Messages ───────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,            -- 'user', 'assistant'
            content         TEXT NOT NULL DEFAULT '',
            tool_calls      JSONB,                   -- [{name, input, result}]
            token_count     INT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS ix_chat_messages_session
            ON chat_messages(session_id, created_at);
    """)

    # ── Drafting Outputs (staged files) ─────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS drafting_outputs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            session_id      UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
            matter_id       UUID NOT NULL REFERENCES matters(id),
            filename        TEXT NOT NULL,
            storage_path    TEXT NOT NULL,
            document_type   TEXT,
            file_size       BIGINT,
            draft_text      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            promoted_at     TIMESTAMPTZ,
            promoted_path   TEXT,
            promoted_doc_id UUID,
            deleted_at      TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS ix_drafting_outputs_tenant_session
            ON drafting_outputs(tenant_id, session_id);
        CREATE INDEX IF NOT EXISTS ix_drafting_outputs_matter
            ON drafting_outputs(matter_id);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS drafting_outputs CASCADE;")
    op.execute("DROP TABLE IF EXISTS chat_messages CASCADE;")
    op.execute("DROP TABLE IF EXISTS chat_sessions CASCADE;")
