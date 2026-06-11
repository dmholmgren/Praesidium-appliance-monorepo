"""0029_identity_federation

Adds:
- user_auth_credentials     — per-user auth source credentials (identity federation)
- connector_entity_map      — discovered external entities mapped to platform users
- connector_entity_sync_log — entity discovery run history
- email_routing_queue       — firm emails staged for matter routing (operational, not eDiscovery)
- email_matter_assignments  — confirmed email→matter mappings (training data)
- email_routing_rules       — per-tenant custom routing rules

ARCHITECTURE — two separate email pipelines:

1. FIRM EMAIL (this migration)
   Exchange/O365 firm connector → EmailRoutingEngine → matter filing + billing trigger
   Operational. No litigation hold. Feeds AI timesheet, matter comms, billing.
   Tables: email_routing_queue, email_matter_assignments, email_routing_rules.

2. CLIENT CUSTODIAN EMAIL (scoped for later — eDiscovery module)
   Client authenticates via OAuth2 → matter-scoped pull → eDiscovery collection
   Evidentiary. Full chain of custody. Litigation hold enforcement.
   Lives in eDiscovery module, NOT here. Only relevant when firm handles litigation
   requiring custodian data, or when the firm itself is sued.

user_auth_credentials:
   Praesidium user is the canonical identity. Auth source is how a user logs in.
   A user may have N credentials across M auth sources (LDAP, Azure AD, local, Okta).
   connector_entity_map always resolves to users.id — never to an external ID directly.

Revision ID: 0029_identity_federation
Revises: 0028_connector_fields
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import TIMESTAMP

revision = '0029_identity_federation'
down_revision = '0028_connector_fields'
branch_labels = None
depends_on = None


def upgrade():

    # ── user_auth_credentials ─────────────────────────────────────────────────
    # One row per (user, auth_source). A user may have credentials from
    # multiple auth sources. Login flow matches on external_id or email
    # across all credentials for the tenant's configured auth adapters.
    # is_primary drives display name resolution, email routing, calendar sync.
    op.create_table('user_auth_credentials',
        sa.Column('id',               sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',        sa.CHAR(36),     nullable=False),
        sa.Column('user_id',          sa.BigInteger(), nullable=False),   # → users.id
        sa.Column('auth_source',      sa.String(64),   nullable=False),   # ldap | azure | local | okta
        sa.Column('external_id',      sa.Text(),       nullable=True),    # sAMAccountName, OID, etc.
        sa.Column('email',            sa.Text(),       nullable=True),    # primary email for this credential
        sa.Column('upn',              sa.Text(),       nullable=True),    # userPrincipalName if applicable
        sa.Column('display_name',     sa.Text(),       nullable=True),    # as returned by auth source
        sa.Column('is_primary',       sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('meta',             JSONB(),         nullable=False, server_default='{}'),
        sa.Column('last_verified_at', sa.TIMESTAMP(timezone=True),nullable=True),
        sa.Column('created_at',       sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at',       sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_uac_tenant_user',   'user_auth_credentials', ['tenant_id', 'user_id'])
    op.create_index('ix_uac_tenant_source', 'user_auth_credentials', ['tenant_id', 'auth_source'])
    op.create_index('ix_uac_external_id',   'user_auth_credentials', ['tenant_id', 'auth_source', 'external_id'])
    op.create_index('ix_uac_email',         'user_auth_credentials', ['tenant_id', 'email'])
    op.create_unique_constraint('uq_uac_tenant_source_external',
        'user_auth_credentials', ['tenant_id', 'auth_source', 'external_id'])

    # ── connector_entity_map ──────────────────────────────────────────────────
    # Discovered external entities (mailboxes, PBX extensions, workstations)
    # mapped to canonical Praesidium users.id.
    # entity_id is the connector-native identifier (email address, extension, etc.)
    # mapped_user_id is NULL until an admin maps it (or auto-match fires).
    # Works identically for Exchange, O365, FreePBX, ManicTime, Clio, etc.
    op.create_table('connector_entity_map',
        sa.Column('id',             sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',      sa.CHAR(36),     nullable=False),
        sa.Column('connector_type', sa.String(64),   nullable=False),
        sa.Column('entity_id',      sa.Text(),       nullable=False),   # connector-native identifier
        sa.Column('entity_display', sa.Text(),       nullable=True),    # human-readable name from source
        sa.Column('entity_email',   sa.Text(),       nullable=True),    # email if applicable
        sa.Column('entity_meta',    JSONB(),         nullable=False, server_default='{}'),
        sa.Column('mapped_user_id', sa.BigInteger(), nullable=True),    # → users.id, NULL until mapped
        sa.Column('is_active',      sa.Boolean(),    nullable=False, server_default='true'),
        sa.Column('auto_matched',   sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('discovered_at',  sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.Column('mapped_at',      sa.TIMESTAMP(timezone=True),nullable=True),
        sa.Column('mapped_by',      sa.BigInteger(), nullable=True),    # → users.id of admin
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cem_tenant_connector', 'connector_entity_map', ['tenant_id', 'connector_type'])
    op.create_index('ix_cem_mapped_user',      'connector_entity_map', ['tenant_id', 'mapped_user_id'])
    op.create_index('ix_cem_entity_email',     'connector_entity_map', ['tenant_id', 'entity_email'])
    op.create_unique_constraint('uq_cem_tenant_connector_entity',
        'connector_entity_map', ['tenant_id', 'connector_type', 'entity_id'])

    # ── connector_entity_sync_log ─────────────────────────────────────────────
    # Tracks when entity discovery last ran per connector per tenant.
    op.create_table('connector_entity_sync_log',
        sa.Column('id',                 sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',          sa.CHAR(36),     nullable=False),
        sa.Column('connector_type',     sa.String(64),   nullable=False),
        sa.Column('started_at',         sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.Column('completed_at',       sa.TIMESTAMP(timezone=True),nullable=True),
        sa.Column('discovered_count',   sa.Integer(),    nullable=False, server_default='0'),
        sa.Column('new_count',          sa.Integer(),    nullable=False, server_default='0'),
        sa.Column('auto_matched_count', sa.Integer(),    nullable=False, server_default='0'),
        sa.Column('error',              sa.Text(),       nullable=True),
        sa.Column('triggered_by',       sa.BigInteger(), nullable=True),  # → users.id
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cesl_tenant_connector', 'connector_entity_sync_log',
                    ['tenant_id', 'connector_type', 'started_at'])

    # ── email_routing_queue ───────────────────────────────────────────────────
    # Firm emails pulled from Exchange/O365 connector, staged for matter routing.
    # HIGH confidence → auto-filed to matter (configurable threshold per tenant).
    # LOW confidence  → queued for attorney review.
    # Attorney confirm/correct → writes to email_matter_assignments (training data).
    #
    # NOTE: This is OPERATIONAL firm email — not eDiscovery custodian collection.
    # Client custodian email lives in the eDiscovery module (scoped for later).
    op.create_table('email_routing_queue',
        sa.Column('id',                  sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',           sa.CHAR(36),     nullable=False),
        sa.Column('connector_type',      sa.String(64),   nullable=False, server_default='exchange'),
        sa.Column('message_id',          sa.Text(),       nullable=False),   # RFC 2822 Message-ID
        sa.Column('internet_message_id', sa.Text(),       nullable=True),    # Exchange EWS InternetMessageId
        sa.Column('subject',             sa.Text(),       nullable=True),
        sa.Column('from_email',          sa.Text(),       nullable=True),
        sa.Column('from_display',        sa.Text(),       nullable=True),
        sa.Column('to_emails',           JSONB(),         nullable=False, server_default='[]'),
        sa.Column('cc_emails',           JSONB(),         nullable=False, server_default='[]'),
        sa.Column('received_at',         sa.TIMESTAMP(timezone=True),nullable=True),
        sa.Column('body_preview',        sa.Text(),       nullable=True),    # first 500 chars
        sa.Column('has_attachments',     sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('attachment_names',    JSONB(),         nullable=False, server_default='[]'),
        # Routing decision
        sa.Column('routing_status',      sa.String(32),   nullable=False, server_default='pending'),
        # pending | auto_filed | queued_review | filed | rejected | error
        sa.Column('matched_matter_id',   sa.UUID(),       nullable=True),    # → matters.id
        sa.Column('match_confidence',    sa.Numeric(5,4), nullable=True),    # 0.0000–1.0000
        sa.Column('match_signals',       JSONB(),         nullable=False, server_default='{}'),
        # {contact_match, subject_matter_number, domain_match, prior_thread, etc.}
        sa.Column('routed_by',           sa.String(32),   nullable=True),    # auto | user_id
        sa.Column('routed_at',           sa.TIMESTAMP(timezone=True),nullable=True),
        sa.Column('attorney_user_id',    sa.BigInteger(), nullable=True),    # → users.id (owner)
        sa.Column('filed_to_dms',        sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('billing_entry_id',    sa.UUID(),       nullable=True),    # → time_entries.id if triggered
        sa.Column('error',               sa.Text(),       nullable=True),
        sa.Column('created_at',          sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_erq_tenant_status',   'email_routing_queue', ['tenant_id', 'routing_status'])
    op.create_index('ix_erq_tenant_matter',   'email_routing_queue', ['tenant_id', 'matched_matter_id'])
    op.create_index('ix_erq_message_id',      'email_routing_queue', ['tenant_id', 'message_id'])
    op.create_index('ix_erq_from_email',      'email_routing_queue', ['tenant_id', 'from_email'])
    op.create_unique_constraint('uq_erq_tenant_message',
        'email_routing_queue', ['tenant_id', 'message_id'])

    # ── email_matter_assignments ──────────────────────────────────────────────
    # Confirmed email→matter assignments. Source of truth for routing history.
    # Also training data for the routing model.
    # Written by: auto-file (high confidence), attorney review confirmation.
    op.create_table('email_matter_assignments',
        sa.Column('id',              sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',       sa.CHAR(36),     nullable=False),
        sa.Column('email_id',        sa.UUID(),       nullable=False),   # → email_routing_queue.id
        sa.Column('matter_id',       sa.UUID(),       nullable=False),   # → matters.id
        sa.Column('assigned_by',     sa.String(32),   nullable=False),   # auto | user:{id}
        sa.Column('confidence',      sa.Numeric(5,4), nullable=True),
        sa.Column('was_corrected',   sa.Boolean(),    nullable=False, server_default='false'),
        # true if attorney overrode auto-assignment — high-value training signal
        sa.Column('original_matter_id', sa.UUID(),   nullable=True),    # if corrected, what was the original
        sa.Column('assigned_at',     sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.Column('assigned_user_id',sa.BigInteger(), nullable=True),   # → users.id if manual
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ema_tenant_matter',  'email_matter_assignments', ['tenant_id', 'matter_id'])
    op.create_index('ix_ema_email',          'email_matter_assignments', ['email_id'])

    # ── email_routing_rules ───────────────────────────────────────────────────
    # Per-tenant custom routing rules. Evaluated before AI routing.
    # Rule types: from_domain, from_email, subject_contains, matter_number_pattern.
    # Priority order: explicit rules → AI routing → review queue.
    op.create_table('email_routing_rules',
        sa.Column('id',             sa.UUID(),       nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id',      sa.CHAR(36),     nullable=False),
        sa.Column('rule_type',      sa.String(64),   nullable=False),
        # from_domain | from_email | subject_contains | matter_number_pattern | contact_match
        sa.Column('rule_value',     sa.Text(),       nullable=False),   # the pattern to match
        sa.Column('matter_id',      sa.UUID(),       nullable=True),    # → matters.id (target)
        sa.Column('priority',       sa.Integer(),    nullable=False, server_default='100'),
        sa.Column('is_active',      sa.Boolean(),    nullable=False, server_default='true'),
        sa.Column('created_by',     sa.BigInteger(), nullable=True),
        sa.Column('created_at',     sa.TIMESTAMP(timezone=True),nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_err_tenant_active', 'email_routing_rules',
                    ['tenant_id', 'is_active', 'priority'])


def downgrade():
    op.drop_table('email_routing_rules')
    op.drop_table('email_matter_assignments')
    op.drop_table('email_routing_queue')
    op.drop_table('connector_entity_sync_log')
    op.drop_table('connector_entity_map')
    op.drop_table('user_auth_credentials')
