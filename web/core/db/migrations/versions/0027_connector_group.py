"""0027_connector_group

Add connector_group and display_order to connector_registry.
Assigns all existing rows to correct groups.
Seeds auth group stubs for future LocalAuthAdapter build.

Revision ID: 0027_connector_group
Revises: 0026_widget_registry
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0027_connector_group'
down_revision = '0026_widget_registry'
branch_labels = None
depends_on = None


def upgrade():
    # ── Add columns ──────────────────────────────────────────────────────────
    op.add_column('connector_registry',
        sa.Column('connector_group', sa.String(64), nullable=False,
                  server_default='data'))
    op.add_column('connector_registry',
        sa.Column('display_order', sa.Integer(), nullable=False,
                  server_default='99'))

    # ── Index for group queries ───────────────────────────────────────────────
    op.create_index('ix_connector_registry_group', 'connector_registry',
                    ['connector_group', 'display_order'])

    # ── Assign groups to existing rows ────────────────────────────────────────
    op.execute("""
        UPDATE connector_registry SET connector_group = 'data', display_order = 10
        WHERE connector_type = 'windows_agent'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'data', display_order = 11
        WHERE connector_type = 'file_crawler'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'data', display_order = 12
        WHERE connector_type = 'exchange'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'billing', display_order = 10
        WHERE connector_type = 'timeslips'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'billing', display_order = 11
        WHERE connector_type = 'manictime'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'billing', display_order = 12
        WHERE connector_type = 'pbx_cdr'
    """)
    op.execute("""
        UPDATE connector_registry SET connector_group = 'research', display_order = 10
        WHERE connector_type = 'courtlistener'
    """)

    # ── Auth group stubs (is_active=false — placeholders until LocalAuthAdapter) ──
    op.execute("""
        INSERT INTO connector_registry
            (connector_type, display_name, description, sync_type,
             connector_group, display_order, is_active,
             config_fields, credential_fields, schedule_options)
        VALUES
        (
            'auth_local',
            'Local Authentication',
            'Username and password authentication against the platform user directory. Default for all new tenants and demo environments.',
            'internal',
            'auth', 10, false,
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        ),
        (
            'auth_ldap',
            'LDAP / Active Directory',
            'Authenticate users against an on-premises Active Directory or LDAP server via LDAPS.',
            'internal',
            'auth', 11, false,
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        ),
        (
            'auth_azure',
            'Azure AD / Entra ID',
            'Single sign-on via Microsoft Azure Active Directory (Entra ID) using OIDC/OAuth2.',
            'internal',
            'auth', 12, false,
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        ),
        (
            'auth_okta',
            'Okta SSO',
            'Single sign-on via Okta. Coming soon.',
            'internal',
            'auth', 13, false,
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        )
        ON CONFLICT (connector_type) DO NOTHING
    """)

    # ── eDiscovery stub (one row, coming soon) ────────────────────────────────
    op.execute("""
        INSERT INTO connector_registry
            (connector_type, display_name, description, sync_type,
             connector_group, display_order, is_active,
             config_fields, credential_fields, schedule_options)
        VALUES
        (
            'ediscovery_client_pull',
            'Client Source Connectors',
            'Connect directly to a client custodian mailbox or cloud drive for eDiscovery ingestion. Client authenticates via OAuth2 — no PST export required. Scoped per matter and custodian. Coming soon.',
            'oauth2_pull',
            'ediscovery', 10, false,
            '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        )
        ON CONFLICT (connector_type) DO NOTHING
    """)


def downgrade():
    op.execute("DELETE FROM connector_registry WHERE connector_type IN "
               "('auth_local','auth_ldap','auth_azure','auth_okta','ediscovery_client_pull')")
    op.drop_index('ix_connector_registry_group', table_name='connector_registry')
    op.drop_column('connector_registry', 'display_order')
    op.drop_column('connector_registry', 'connector_group')
