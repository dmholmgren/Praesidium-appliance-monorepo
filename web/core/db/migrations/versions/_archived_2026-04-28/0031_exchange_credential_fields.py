"""0031_exchange_credential_fields

Patches connector_registry for exchange connector:
- Adds credential_fields (username, password, domain)
- Credentials stored in credentials_vault, not env vars

Revision ID: 0031_exchange_credential_fields
Revises: 0030_exchange_calendar
Create Date: 2026-04-13
"""
from alembic import op

revision = '0031_exchange_credential_fields'
down_revision = '0030_exchange_calendar'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE connector_registry SET
            credential_fields = '[
                {
                    "name": "username",
                    "type": "text",
                    "label": "Service Account Username",
                    "placeholder": "praesidium",
                    "required": true,
                    "help": "AD username with ApplicationImpersonation rights"
                },
                {
                    "name": "password",
                    "type": "password",
                    "label": "Service Account Password",
                    "required": true
                },
                {
                    "name": "domain",
                    "type": "text",
                    "label": "AD Domain",
                    "placeholder": "hjmmlegal.com",
                    "required": true
                }
            ]'::jsonb
        WHERE connector_type = 'exchange'
    """)


def downgrade():
    op.execute("""
        UPDATE connector_registry SET
            credential_fields = '[]'::jsonb
        WHERE connector_type = 'exchange'
    """)
