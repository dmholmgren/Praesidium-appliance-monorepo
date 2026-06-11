"""Add payment_gateway_config table for multi-gateway payment connector credentials.

Revision ID: 0019_payment_gateway_config
Revises: 0018_bill_templates
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0019_payment_gateway_config'
down_revision = '0018_bill_templates'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'payment_gateway_config',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('gateway_type', sa.String(50), nullable=False),
        # Encrypted credentials blob — decrypt at adapter init
        sa.Column('credentials_json', sa.Text(), nullable=True),
        # Non-secret config (sandbox, enabled flags, sync prefs)
        sa.Column('config_json', JSONB, server_default='{}', nullable=False),
        sa.Column('is_sandbox', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
        # Status tracking
        sa.Column('last_status', sa.String(20), server_default='unconfigured', nullable=False),
        sa.Column('last_status_message', sa.Text(), nullable=True),
        sa.Column('last_successful_call', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_diagnosis_json', JSONB, nullable=True),
        sa.Column('last_diagnosis_at', sa.DateTime(timezone=True), nullable=True),
        # Audit
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('updated_by', sa.BigInteger(), nullable=True),
        # One config per gateway per tenant
        sa.UniqueConstraint('tenant_id', 'gateway_type', name='uq_pgc_tenant_gateway'),
    )
    op.create_index('idx_pgc_tenant', 'payment_gateway_config', ['tenant_id'])
    op.create_index('idx_pgc_type', 'payment_gateway_config', ['gateway_type'])

    # Webhook event log — track all inbound webhook payloads
    op.create_table(
        'payment_webhook_log',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('gateway_type', sa.String(50), nullable=False),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('transaction_id', sa.String(255), nullable=True),
        sa.Column('invoice_reference', sa.String(255), nullable=True),
        sa.Column('amount_cents', sa.BigInteger(), nullable=True),
        sa.Column('currency', sa.String(3), server_default='USD', nullable=False),
        sa.Column('status', sa.String(50), nullable=True),
        sa.Column('signature_valid', sa.Boolean(), nullable=True),
        sa.Column('processed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('raw_headers', JSONB, nullable=True),
        sa.Column('raw_body', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_pwl_tenant', 'payment_webhook_log', ['tenant_id'])
    op.create_index('idx_pwl_gateway', 'payment_webhook_log', ['gateway_type'])
    op.create_index('idx_pwl_txn', 'payment_webhook_log', ['transaction_id'])
    op.create_index('idx_pwl_received', 'payment_webhook_log', ['received_at'])

    # Add gateway_type and gateway_transaction_id to payments table
    # so payments can be attributed to any gateway, not just LawPay
    op.add_column('payments', sa.Column('gateway_type', sa.String(50), nullable=True))
    op.add_column('payments', sa.Column('gateway_transaction_id', sa.String(255), nullable=True))
    op.create_index('idx_payments_gateway', 'payments', ['gateway_type'])
    op.create_index('idx_payments_gateway_txn', 'payments', ['gateway_transaction_id'])


def downgrade():
    op.drop_index('idx_payments_gateway_txn', table_name='payments')
    op.drop_index('idx_payments_gateway', table_name='payments')
    op.drop_column('payments', 'gateway_transaction_id')
    op.drop_column('payments', 'gateway_type')
    op.drop_table('payment_webhook_log')
    op.drop_table('payment_gateway_config')
