"""0077: triage_turns — capture guided-ingest review decisions as a learnable corpus.

Every review action on an ingestion plan — whether the attorney typed an
instruction into the plan assistant or tapped a Reject/Restore button — is one
labeled example: given this plan state, the human's instruction produced this
include/exclude/dedup change. We were generating that signal in the chat
endpoint and the modal and throwing it away on return; this table keeps it.

Linked back to ai_api_calls.id (bigint) for chat turns so the prompt/response
spend row and the human outcome sit together. Button taps carry no ai_call_id.

Idempotent; safe to re-run.
"""
from alembic import op

revision = "0077_triage_turns"
down_revision = "0076_onboarding_alert_payload"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS triage_turns (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    varchar NOT NULL,
            matter_id    uuid,
            proposal_id  uuid,
            custodian    text,
            source       text NOT NULL DEFAULT 'chat',  -- 'chat' | 'button' | 'chat_confirm'
            user_message text,                           -- typed instruction; null for button taps
            plan_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,  -- collections as seen at decision time
            actions      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{op, name, custodian?, reason?}]
            ai_call_id   bigint,                         -- ai_api_calls.id for chat turns
            applied      boolean NOT NULL DEFAULT false,
            user_id      bigint,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_triage_turns_matter "
               "ON triage_turns (tenant_id, matter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_triage_turns_proposal "
               "ON triage_turns (proposal_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS triage_turns")
