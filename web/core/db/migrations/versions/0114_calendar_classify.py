"""0114: calendar event typing + matter-link provenance (Court/Hearing Step 2, Gap #3).

calendar_events arrived from the Exchange import with event_type NULL on all 482
rows and matter_id linked on 0. Nothing could recognize a hearing or attribute
an event to a matter. This migration lays the persistence for the two classifiers
in modules.intelligence.calendar_classifier:

  * event typing  — denormalized event_type on calendar_events (consumed by the
    §2 typed-event → workspace projector), plus provenance columns so a typing
    decision is reviewable and a human correction is protected from re-runs.
  * matter-link   — calendar_matter_assignments, mirroring the established
    email_matter_assignments shape (assigned_by rule|ai|manual, confidence,
    was_corrected/original_matter_id). matter_id is also denormalized onto
    calendar_events for fast projection. Corrections never overwritten by a
    re-run (was_corrected / assigned_by='manual' are sticky).

Both follow the shared-core invariant proven for the document classifier
(structural-first → model-on-residue; supersede/correct; idempotent).
"""
from alembic import op


revision = "0114_calendar_classify"
down_revision = "0113_classifier_seed"
branch_labels = None
depends_on = None


def upgrade():
    # typing provenance on the event itself (column event_type already exists)
    op.execute("ALTER TABLE calendar_events "
               "ADD COLUMN IF NOT EXISTS event_type_source varchar(16)")
    op.execute("ALTER TABLE calendar_events "
               "ADD COLUMN IF NOT EXISTS event_type_confidence numeric")
    op.execute("ALTER TABLE calendar_events "
               "ADD COLUMN IF NOT EXISTS event_typed_at timestamptz")
    op.execute("CREATE INDEX IF NOT EXISTS ix_calendar_events_event_type "
               "ON calendar_events (tenant_id, event_type)")

    # calendar_matter_assignments already exists (the email_matter_assignments
    # family: one live row per event, was_corrected/original_matter_id capture a
    # correction in place — no supersede chain). Augment it with a `signals`
    # provenance column and enforce one row per event so the resolver can upsert.
    op.execute("ALTER TABLE calendar_matter_assignments "
               "ADD COLUMN IF NOT EXISTS signals jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cal_matter_assign_event "
               "ON calendar_matter_assignments (event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cal_matter_assign_matter "
               "ON calendar_matter_assignments (tenant_id, matter_id)")

    # AI residue routes for the two calendar purposes (module 'classification').
    for purpose, max_tok in (("calendar_event_type", 60),
                             ("calendar_matter_link", 80)):
        op.execute(f"""
            INSERT INTO ai_model_routing
                (tenant_id, module, purpose, primary_model, fallback_model,
                 max_tokens, status)
            SELECT NULL, 'classification', '{purpose}',
                   'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', {max_tok},
                   'published'
            WHERE NOT EXISTS (
                SELECT 1 FROM ai_model_routing
                WHERE module='classification' AND purpose='{purpose}'
                  AND tenant_id IS NULL)
        """)


def downgrade():
    op.execute("DELETE FROM ai_model_routing WHERE module='classification' "
               "AND purpose IN ('calendar_event_type','calendar_matter_link')")
    op.execute("DROP INDEX IF EXISTS uq_cal_matter_assign_event")
    op.execute("DROP INDEX IF EXISTS ix_cal_matter_assign_matter")
    op.execute("ALTER TABLE calendar_matter_assignments DROP COLUMN IF EXISTS signals")
    op.execute("ALTER TABLE calendar_events DROP COLUMN IF EXISTS event_typed_at")
    op.execute("ALTER TABLE calendar_events DROP COLUMN IF EXISTS event_type_confidence")
    op.execute("ALTER TABLE calendar_events DROP COLUMN IF EXISTS event_type_source")
