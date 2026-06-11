"""
Alembic migration: Create onboarding matching tables (0046) - FIXED
Uses CHAR(36) for tenant_id to match tenants table
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0046_onboarding_matching'
down_revision = '0045_deadline_type_lexicon'
branch_labels = None
depends_on = None

def upgrade():
    # Main onboarding segments table
    op.create_table(
        'onboarding_matching_segments',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column('tenant_id', sa.CHAR(36), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('legacy_file_id', UUID(as_uuid=True), nullable=False),
        sa.Column('legacy_file_path', sa.String(2048), nullable=False),
        sa.Column('file_type', sa.String(20), nullable=False),
        sa.Column('segment_index', sa.Integer, nullable=False),
        sa.Column('content', sa.Text, nullable=True),
        sa.Column('char_start', sa.Integer, nullable=True),
        sa.Column('char_end', sa.Integer, nullable=True),
        sa.Column('embedding', sa.Float(), nullable=True),
        sa.Column('proposed_matter_id', UUID(as_uuid=True), sa.ForeignKey('matters.id'), nullable=True),
        sa.Column('confidence', sa.Numeric(3, 2), nullable=True),
        sa.Column('evidence', sa.Text, nullable=True),
        sa.Column('status', sa.String(20), default='pending', nullable=False),
        sa.Column('extraction_job_id', UUID(as_uuid=True), nullable=True),
        sa.Column('matched_at', sa.DateTime(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', sa.String(255), nullable=True),
        sa.Column('copied_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Index('idx_onboarding_segments_tenant_status', 'tenant_id', 'status'),
        sa.Index('idx_onboarding_segments_matter', 'proposed_matter_id'),
    )
    
    # Metadata for legacy files
    op.create_table(
        'onboarding_legacy_files',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column('tenant_id', sa.CHAR(36), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('full_path', sa.String(2048), nullable=False),
        sa.Column('file_type', sa.String(20), nullable=False),
        sa.Column('size_bytes', sa.BigInteger, nullable=False),
        sa.Column('modified_at', sa.DateTime(), nullable=True),
        sa.Column('extracted_text', sa.Text, nullable=True),
        sa.Column('page_count', sa.Integer, nullable=True),
        sa.Column('has_images', sa.Boolean, default=False),
        sa.Column('extraction_status', sa.String(20), default='pending', nullable=False),
        sa.Column('extraction_job_id', UUID(as_uuid=True), nullable=True),
        sa.Column('extracted_at', sa.DateTime(), nullable=True),
        sa.Column('extraction_error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Index('idx_onboarding_files_tenant_status', 'tenant_id', 'extraction_status'),
    )
    
    # Onboarding job tracking
    op.create_table(
        'onboarding_jobs',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column('tenant_id', sa.CHAR(36), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('job_type', sa.String(50), nullable=False),
        sa.Column('status', sa.String(20), default='pending', nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('files_processed', sa.Integer, default=0),
        sa.Column('files_succeeded', sa.Integer, default=0),
        sa.Column('files_failed', sa.Integer, default=0),
        sa.Column('metadata', JSONB, nullable=True),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Index('idx_onboarding_jobs_tenant_status', 'tenant_id', 'status'),
    )
    
    # Matter-level onboarding alerts
    op.create_table(
        'onboarding_alerts',
        sa.Column('id', UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column('tenant_id', sa.CHAR(36), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('matter_id', UUID(as_uuid=True), sa.ForeignKey('matters.id'), nullable=False),
        sa.Column('alert_type', sa.String(50), nullable=False),
        sa.Column('count', sa.Integer, default=0),
        sa.Column('message', sa.Text, nullable=True),
        sa.Column('status', sa.String(20), default='new', nullable=False),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_by', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Index('idx_onboarding_alerts_matter_type', 'matter_id', 'alert_type'),
    )

def downgrade():
    op.drop_table('onboarding_alerts')
    op.drop_table('onboarding_jobs')
    op.drop_table('onboarding_legacy_files')
    op.drop_table('onboarding_matching_segments')
