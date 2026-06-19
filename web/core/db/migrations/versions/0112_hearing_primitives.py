"""0112: Court/Hearing system — Layer-1 hearing primitives.

Implements Migration 1 of Praesidium_Court_Hearing_Architecture_v1_0 §3:
hearings (durable identity), hearing_reschedules (append-only move log),
hearing_signals (evidence ledger / keystone of the AI-guided backfill).

First principles honored here:
  - Primitive-not-chunk: these are durable rows, never embeddings.
  - Provenance is sacred: original_start_at is an immutable baseline; every
    move is an append-only hearing_reschedules row stamped to its source
    witness; nothing overwrites a prior date.
  - Three independent witnesses to "when": all evidence lands in
    hearing_signals BEFORE any hearing is concluded, so reconstruction is
    auditable, idempotent and re-runnable.

Crosscheck taxonomy extension (§3 "Crosscheck taxonomy extension"):
calendar_crosscheck_log.discrepancy_type is a free varchar(32) with NO check
constraint, so the two new values — hearing_missing_notice, unmatched_notice —
require NO DDL. They are documented allowed values consumed by the daily
Cross-Check Agent 450; this migration intentionally adds no constraint so the
agent stays free to write them.

tenant_id is char(36) to match the canonical schema (matters/dms_documents/
time_entries/calendar_events all use char(36)); app-layer joins TRIM.
"""
from alembic import op


revision = "0112_hearing_primitives"
down_revision = "0111_transcript_views"
branch_labels = None
depends_on = None


def upgrade():
    # ── hearings ─────────────────────────────────────────────────────────
    # Durable hearing identity, distinct from any calendar event id. The
    # calendar event is repointable (current_event_id); the hearing endures
    # across every reschedule. original_start_at is the immutable baseline.
    op.execute("""
        CREATE TABLE IF NOT EXISTS hearings (
            id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               char(36)    NOT NULL,
            matter_id               uuid        NOT NULL
                                        REFERENCES matters(id) ON DELETE CASCADE,
            current_event_id        uuid
                                        REFERENCES calendar_events(id) ON DELETE SET NULL,
            hearing_type            varchar(64),
            judge                   varchar(255),
            courtroom               varchar(255),
            status                  varchar(32) NOT NULL DEFAULT 'scheduled',
            outcome                 varchar(64),
            original_start_at       timestamptz,         -- immutable baseline (earliest known)
            current_start_at        timestamptz,         -- denormalized convenience
            notice_status           varchar(24) NOT NULL DEFAULT 'pending',
                                        -- pending | attached | reconciled | oral_no_notice
            notice_document_id      uuid        REFERENCES dms_documents(id) ON DELETE SET NULL,
            transcript_doc_id       uuid        REFERENCES dms_documents(id) ON DELETE SET NULL,
            -- who/why behind a terminal notice_status (esp. oral_no_notice,
            -- set from the bench) so the daily alert can be credibly silenced
            notice_status_set_by    bigint,
            notice_status_set_at    timestamptz,
            notice_status_reason    text,
            confidence              numeric,
            provenance              jsonb       NOT NULL DEFAULT '{}'::jsonb,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearings_tenant_matter "
               "ON hearings (tenant_id, matter_id)")
    # daily alert scans for hearings still in 'pending'
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearings_notice_status "
               "ON hearings (tenant_id, notice_status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearings_current_event "
               "ON hearings (current_event_id)")

    # ── hearing_reschedules ──────────────────────────────────────────────
    # Append-only move log. "Reset N times" = COUNT(*) per hearing_id; the
    # timeline is the ordered rows, each linked to the source witness that
    # produced it. Never updated in place.
    op.execute("""
        CREATE TABLE IF NOT EXISTS hearing_reschedules (
            id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            hearing_id              uuid        NOT NULL
                                        REFERENCES hearings(id) ON DELETE CASCADE,
            tenant_id               char(36)    NOT NULL,
            sequence_no             integer     NOT NULL,
            from_start_at           timestamptz,
            to_start_at             timestamptz NOT NULL,
            delta_days              integer,
            source                  varchar(24) NOT NULL,
                                        -- notice | order | exchange | time_signal | manual
            source_document_id      uuid        REFERENCES dms_documents(id) ON DELETE SET NULL,
            source_time_entry_id    uuid        REFERENCES time_entries(id) ON DELETE SET NULL,
            moving_party            varchar(255),
            reason                  text,
            confidence              numeric,
            detected_at             timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_hearing_reschedule_seq UNIQUE (hearing_id, sequence_no)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearing_reschedules_hearing "
               "ON hearing_reschedules (hearing_id, sequence_no)")

    # ── hearing_signals ──────────────────────────────────────────────────
    # Evidence ledger: every witness (notice/order/exchange/time-entry/email)
    # lands here BEFORE any hearing is concluded. matter_id and
    # resolved_into_hearing_id are nullable because evidence may precede both
    # matter resolution and hearing conclusion. This is what makes the
    # backfill auditable, idempotent and re-runnable.
    op.execute("""
        CREATE TABLE IF NOT EXISTS hearing_signals (
            id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id               char(36)    NOT NULL,
            matter_id               uuid        REFERENCES matters(id) ON DELETE CASCADE,
            signal_type             varchar(24) NOT NULL,
                                        -- notice | order | exchange_event | time_entry | email
            source_ref              text,       -- heterogeneous pointer (doc id / time entry id / exchange row / email segment)
            candidate_date          timestamptz,
            party                   varchar(255),
            match_score             numeric,
            resolved_into_hearing_id uuid       REFERENCES hearings(id) ON DELETE SET NULL,
            collected_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearing_signals_tenant_matter "
               "ON hearing_signals (tenant_id, matter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearing_signals_resolved "
               "ON hearing_signals (resolved_into_hearing_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hearing_signals_type "
               "ON hearing_signals (tenant_id, signal_type)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS hearing_signals")
    op.execute("DROP TABLE IF EXISTS hearing_reschedules")
    op.execute("DROP TABLE IF EXISTS hearings")
