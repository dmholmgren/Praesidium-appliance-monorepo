"""0030_exchange_calendar

Adds:
- exchange_calendar_events   — raw events pulled from Exchange per mailbox
- calendar_matter_assignments — confirmed event→matter mappings (training data)
- calendar_divergence_log    — malpractice double-calendaring audit trail
- firm_calendar_sources      — which mailboxes feed the firm/system calendar

Also updates connector_registry exchange config_fields:
  - Removes single mailbox field (discovery replaces it)
  - Adds sync_calendar, sync_email, lookback fields

ARCHITECTURE:
  System Calendar (read-only, AUTO) = Exchange sync + M3 deadlines
  Admin Calendar (manual) = attorney manual entries
  Nightly divergence check = flag delta between the two
  Matter tagging = subject parse + attendee match + billing correlation

Revision ID: 0030_exchange_calendar
Revises: 0029_identity_federation
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import TIMESTAMP
import json

revision = '0030_exchange_calendar'
down_revision = '0029_identity_federation'
branch_labels = None
depends_on = None


def upgrade():

    # ── firm_calendar_sources ─────────────────────────────────────────────────
    # Which mailboxes feed the firm/system calendar.
    # calendar@hjmmlegal.com seeded as firm calendar (is_firm_calendar=true).
    # Per-user mailboxes added via entity mapping, seeded here with
    # is_firm_calendar=false.
    op.create_table('firm_calendar_sources',
        sa.Column('id',               sa.UUID(),      nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',        sa.CHAR(36),    nullable=False),
        sa.Column('mailbox',          sa.Text(),      nullable=False),  # email address
        sa.Column('display_name',     sa.Text(),      nullable=True),
        sa.Column('calendar_type',    sa.String(32),  nullable=False, server_default='personal'),
        # personal | firm | resource | shared
        sa.Column('is_firm_calendar', sa.Boolean(),   nullable=False, server_default='false'),
        # true = feeds firm/system calendar visible to all users
        sa.Column('is_service_account', sa.Boolean(), nullable=False, server_default='false'),
        # true = data source only, no platform login (e.g. calendar@hjmmlegal.com)
        sa.Column('mapped_user_id',   sa.BigInteger(), nullable=True),  # → users.id
        sa.Column('sync_enabled',     sa.Boolean(),   nullable=False, server_default='true'),
        sa.Column('last_sync_at',     TIMESTAMP(),    nullable=True),
        sa.Column('ews_url',          sa.Text(),      nullable=True),   # override per mailbox if needed
        sa.Column('created_at',       TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at',       TIMESTAMP(),    nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_fcs_tenant',   'firm_calendar_sources', ['tenant_id'])
    op.create_index('ix_fcs_mailbox',  'firm_calendar_sources', ['tenant_id', 'mailbox'])
    op.create_unique_constraint('uq_fcs_tenant_mailbox',
        'firm_calendar_sources', ['tenant_id', 'mailbox'])

    # Seed calendar@hjmmlegal.com as firm calendar service account
    op.execute("""
        INSERT INTO firm_calendar_sources
            (tenant_id, mailbox, display_name, calendar_type,
             is_firm_calendar, is_service_account, sync_enabled)
        VALUES
            ('hjmm-prod                           ', 'calendar@hjmmlegal.com',
             'HJMM Firm Calendar', 'firm', true, true, true)
        ON CONFLICT (tenant_id, mailbox) DO NOTHING
    """)

    # ── exchange_calendar_events ──────────────────────────────────────────────
    # Raw calendar events pulled from Exchange via EWS.
    # Covers per-user mailboxes and firm calendar.
    # Matter tagging: subject parse + attendee match + billing correlation.
    op.create_table('exchange_calendar_events',
        sa.Column('id',               sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',        sa.CHAR(36),     nullable=False),
        sa.Column('mailbox',          sa.Text(),       nullable=False),   # source mailbox
        sa.Column('ews_item_id',      sa.Text(),       nullable=False),   # Exchange ItemId
        sa.Column('change_key',       sa.Text(),       nullable=True),    # Exchange ChangeKey
        sa.Column('subject',          sa.Text(),       nullable=True),
        sa.Column('start_at',         TIMESTAMP(timezone=True), nullable=True),
        sa.Column('end_at',           TIMESTAMP(timezone=True), nullable=True),
        sa.Column('is_all_day',       sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('location',         sa.Text(),       nullable=True),
        sa.Column('organizer_email',  sa.Text(),       nullable=True),
        sa.Column('organizer_name',   sa.Text(),       nullable=True),
        sa.Column('attendees',        JSONB(),         nullable=False, server_default='[]'),
        # [{email, name, response_status}]
        sa.Column('body_preview',     sa.Text(),       nullable=True),    # first 500 chars
        sa.Column('is_recurring',     sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('recurrence_master_id', sa.Text(),   nullable=True),
        sa.Column('source_calendar',  sa.String(32),   nullable=False, server_default='personal'),
        # personal | firm | resource
        # ── Matter tagging ──
        sa.Column('matter_id',        sa.UUID(),       nullable=True),    # → matters.id
        sa.Column('match_confidence', sa.Numeric(5,4), nullable=True),    # 0.0–1.0
        sa.Column('match_signals',    JSONB(),         nullable=False, server_default='{}'),
        # {subject_matter_number, attendee_contact, billing_correlation, manual}
        sa.Column('routing_status',   sa.String(32),   nullable=False, server_default='pending'),
        # pending | auto_tagged | queued_review | tagged | untagged
        sa.Column('tagged_by',        sa.String(32),   nullable=True),    # auto | user:{id}
        sa.Column('tagged_at',        TIMESTAMP(),     nullable=True),
        sa.Column('attorney_user_id', sa.BigInteger(), nullable=True),    # → users.id (owner)
        sa.Column('created_at',       TIMESTAMP(),     nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at',       TIMESTAMP(),     nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ece_tenant_mailbox',  'exchange_calendar_events', ['tenant_id', 'mailbox'])
    op.create_index('ix_ece_tenant_matter',   'exchange_calendar_events', ['tenant_id', 'matter_id'])
    op.create_index('ix_ece_tenant_start',    'exchange_calendar_events', ['tenant_id', 'start_at'])
    op.create_index('ix_ece_routing_status',  'exchange_calendar_events', ['tenant_id', 'routing_status'])
    op.create_unique_constraint('uq_ece_tenant_mailbox_item',
        'exchange_calendar_events', ['tenant_id', 'mailbox', 'ews_item_id'])

    # ── calendar_matter_assignments ───────────────────────────────────────────
    # Confirmed event→matter assignments.
    # Source of truth for calendar history. Also training data for tagging model.
    op.create_table('calendar_matter_assignments',
        sa.Column('id',               sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',        sa.CHAR(36),     nullable=False),
        sa.Column('event_id',         sa.UUID(),       nullable=False),   # → exchange_calendar_events.id
        sa.Column('matter_id',        sa.UUID(),       nullable=False),   # → matters.id
        sa.Column('assigned_by',      sa.String(32),   nullable=False),   # auto | user:{id}
        sa.Column('confidence',       sa.Numeric(5,4), nullable=True),
        sa.Column('was_corrected',    sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('original_matter_id', sa.UUID(),     nullable=True),
        sa.Column('assigned_at',      TIMESTAMP(),     nullable=False, server_default=sa.text('now()')),
        sa.Column('assigned_user_id', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cma_tenant_matter', 'calendar_matter_assignments', ['tenant_id', 'matter_id'])
    op.create_index('ix_cma_event',         'calendar_matter_assignments', ['event_id'])

    # ── calendar_divergence_log ───────────────────────────────────────────────
    # Malpractice double-calendaring audit trail.
    # Nightly job compares M3 deadlines against exchange_calendar_events.
    # Flags matters where system calendar ≠ admin calendar.
    # Resolution required — no silent dismissal.
    op.create_table('calendar_divergence_log',
        sa.Column('id',              sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',       sa.CHAR(36),     nullable=False),
        sa.Column('matter_id',       sa.UUID(),       nullable=False),   # → matters.id
        sa.Column('deadline_id',     sa.UUID(),       nullable=True),    # → court_deadlines.id if applicable
        sa.Column('divergence_type', sa.String(64),   nullable=False),
        # missing_from_calendar | date_mismatch | missing_from_system | unconfirmed_deadline
        sa.Column('event_type',      sa.String(64),   nullable=True),    # trial | deposition | hearing | deadline
        sa.Column('system_date',     sa.Date(),       nullable=True),    # what M3 calculated
        sa.Column('calendar_date',   sa.Date(),       nullable=True),    # what's on Exchange calendar
        sa.Column('admin_date',      sa.Date(),       nullable=True),    # what admin manually entered
        sa.Column('delta_days',      sa.Integer(),    nullable=True),    # |system_date - calendar_date|
        sa.Column('severity',        sa.String(16),   nullable=False, server_default='warning'),
        # info | warning | critical
        sa.Column('flagged_at',      TIMESTAMP(),     nullable=False, server_default=sa.text('now()')),
        sa.Column('resolved_at',     TIMESTAMP(),     nullable=True),
        sa.Column('resolved_by',     sa.BigInteger(), nullable=True),    # → users.id
        sa.Column('resolution_note', sa.Text(),       nullable=True),
        sa.Column('is_resolved',     sa.Boolean(),    nullable=False, server_default='false'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cdl_tenant_matter',    'calendar_divergence_log', ['tenant_id', 'matter_id'])
    op.create_index('ix_cdl_unresolved',       'calendar_divergence_log', ['tenant_id', 'is_resolved', 'severity'])
    op.create_index('ix_cdl_flagged_at',       'calendar_divergence_log', ['tenant_id', 'flagged_at'])

    # ── Update exchange connector config_fields ───────────────────────────────
    # Remove single mailbox field — discovery replaces it.
    # Add calendar sync options.
    op.execute("""
        UPDATE connector_registry SET
            description = 'On-premises Microsoft Exchange via EWS. Syncs email and calendar for all mapped mailboxes including the firm shared calendar. Feeds matter routing, AI timesheet, and malpractice calendar check.',
            config_fields = %s::jsonb
        WHERE connector_type = 'exchange'
    """ % (
        "'" + json.dumps([
            {"name": "ews_url",
             "type": "text",
             "label": "EWS URL",
             "required": True,
             "placeholder": "https://exchange01.hjmmlegal.com/EWS/Exchange.asmx"},
            {"name": "sync_email",
             "type": "checkbox",
             "label": "Sync Email",
             "checkbox_label": "Pull email from mapped mailboxes for matter routing and AI timesheet",
             "required": False},
            {"name": "sync_calendar",
             "type": "checkbox",
             "label": "Sync Calendar",
             "checkbox_label": "Pull calendar events from mapped mailboxes and firm calendar",
             "required": False},
            {"name": "email_lookback_days",
             "type": "number",
             "label": "Email Lookback (days)",
             "required": False,
             "placeholder": "90",
             "help": "How many days back to sync on first run"},
            {"name": "calendar_lookback_days",
             "type": "number",
             "label": "Calendar Lookback (days)",
             "required": False,
             "placeholder": "30",
             "help": "How many days back to pull calendar events"},
            {"name": "calendar_lookahead_days",
             "type": "number",
             "label": "Calendar Lookahead (days)",
             "required": False,
             "placeholder": "180",
             "help": "How many days forward to pull calendar events"},
        ]).replace("'", "''") + "'"
    ))


def downgrade():
    op.drop_table('calendar_divergence_log')
    op.drop_table('calendar_matter_assignments')
    op.drop_table('exchange_calendar_events')
    op.drop_table('firm_calendar_sources')
    op.execute("""
        UPDATE connector_registry SET
            config_fields = '[{"name": "ews_url", "type": "text", "label": "EWS URL", "required": true},
                              {"name": "mailbox", "type": "text", "label": "Mailbox", "required": true}]'::jsonb
        WHERE connector_type = 'exchange'
    """)
