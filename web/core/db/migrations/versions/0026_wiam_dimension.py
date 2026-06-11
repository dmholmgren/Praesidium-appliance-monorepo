"""wiam findings dimension column + session enhancements

Revision ID: 0026_wiam_dimension
Revises: 0025_meeting_workspaces
Create Date: 2026-05-17

Aligns wiam_findings with S2-002 patent spec (FIG. 3):
  - dimension: their_case | your_case | context | drift
  - wiam_sessions.error_log for engine diagnostics
  - wiam_sessions.total_findings, context_summary, dimensions_requested
  - wiam_findings index on (session_id, dimension, priority)
"""

revision = '0026_wiam_dimension'
down_revision = '0025_meeting_workspaces'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import op
    import sqlalchemy as sa

    # 1. Add dimension to wiam_findings (S2-002 FIG. 3)
    op.add_column('wiam_findings',
        sa.Column('dimension', sa.String(32), nullable=True))

    # 2. Backfill dimension from finding_type for any existing rows
    op.execute("""
        UPDATE wiam_findings SET dimension = CASE
            WHEN finding_type = 'opp_gap' THEN 'their_case'
            WHEN finding_type = 'own_gap' THEN 'your_case'
            WHEN finding_type = 'external_gap' THEN 'context'
            WHEN finding_type = 'drift_gap' THEN 'drift'
            ELSE 'your_case'
        END
        WHERE dimension IS NULL
    """)

    # 3. Add columns to wiam_sessions
    op.add_column('wiam_sessions',
        sa.Column('error_log', sa.Text(), nullable=True))
    op.add_column('wiam_sessions',
        sa.Column('dimensions_requested', sa.dialects.postgresql.JSONB(),
                  nullable=True))
    op.add_column('wiam_sessions',
        sa.Column('total_findings', sa.Integer(), server_default='0',
                  nullable=True))
    op.add_column('wiam_sessions',
        sa.Column('context_summary', sa.Text(), nullable=True))

    # 4. Indexes
    op.create_index(
        'ix_wiam_findings_session_dim_pri',
        'wiam_findings',
        ['session_id', 'dimension', 'priority'])
    op.create_index(
        'ix_wiam_findings_matter_tenant',
        'wiam_findings',
        ['matter_id', 'tenant_id'])
    op.create_index(
        'ix_wiam_sessions_matter_tenant',
        'wiam_sessions',
        ['matter_id', 'tenant_id', 'created_at'])


def downgrade():
    from alembic import op
    op.drop_index('ix_wiam_sessions_matter_tenant')
    op.drop_index('ix_wiam_findings_matter_tenant')
    op.drop_index('ix_wiam_findings_session_dim_pri')
    op.drop_column('wiam_sessions', 'context_summary')
    op.drop_column('wiam_sessions', 'total_findings')
    op.drop_column('wiam_sessions', 'dimensions_requested')
    op.drop_column('wiam_sessions', 'error_log')
    op.drop_column('wiam_findings', 'dimension')
