"""0027 — Matter Permissions, Personal Matters, Chinese Wall

Adds:
- clients.is_personal, clients.owner_user_id
- matters.is_personal, matters.owner_user_id
- chinese_wall_exclusions table
- user_permissions table (per-user checkbox overrides from firm admin)
- Indexes for query-filter injection performance

Revision ID: 0027_matter_permissions
Revises: 0026_wiam_dimension
"""
from alembic import op
import sqlalchemy as sa

revision = '0027_matter_permissions'
down_revision = '0026_wiam_dimension'
branch_labels = None
depends_on = None


def upgrade():
    # ── clients: personal client support ──────────────────────────────
    op.add_column('clients', sa.Column('is_personal', sa.Boolean(),
                  server_default=sa.text('false'), nullable=False))
    op.add_column('clients', sa.Column('owner_user_id', sa.BigInteger(),
                  nullable=True))
    op.create_foreign_key('fk_clients_owner_user', 'clients', 'users',
                          ['owner_user_id'], ['id'])
    op.create_index('ix_clients_personal', 'clients',
                    ['tenant_id', 'is_personal'])
    op.create_index('ix_clients_owner', 'clients',
                    ['owner_user_id'], postgresql_where=sa.text('is_personal = true'))

    # ── matters: personal matter flag + owner ─────────────────────────
    op.add_column('matters', sa.Column('is_personal', sa.Boolean(),
                  server_default=sa.text('false'), nullable=False))
    op.add_column('matters', sa.Column('owner_user_id', sa.BigInteger(),
                  nullable=True))
    op.create_foreign_key('fk_matters_owner_user', 'matters', 'users',
                          ['owner_user_id'], ['id'])
    op.create_index('ix_matters_personal', 'matters',
                    ['tenant_id', 'is_personal'])
    op.create_index('ix_matters_owner', 'matters',
                    ['owner_user_id'], postgresql_where=sa.text('is_personal = true'))

    # ── chinese_wall_exclusions ───────────────────────────────────────
    op.create_table(
        'chinese_wall_exclusions',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('matter_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('matters.id'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_by', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.UniqueConstraint('tenant_id', 'user_id', 'matter_id',
                            name='uq_chinese_wall_tenant_user_matter'),
    )
    op.create_index('ix_cw_user_active', 'chinese_wall_exclusions',
                    ['tenant_id', 'user_id'],
                    postgresql_where=sa.text('is_active = true'))

    # ── user_permissions (checkbox overrides from firm admin) ─────────
    op.create_table(
        'user_permissions',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('permission_key', sa.String(100), nullable=False),
        sa.Column('granted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('granted_by', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('tenant_id', 'user_id', 'permission_key',
                            name='uq_user_perm_tenant_user_key'),
    )
    op.create_index('ix_user_perms_lookup', 'user_permissions',
                    ['tenant_id', 'user_id'])

    # ── matter_timekeepers: ensure role values documented ─────────────
    # role column already varchar(50), no schema change needed
    # Defined values: originating, responsible, assigned, supervising, of_counsel

    # ── matters.status: add index for closed-matter filtering ─────────
    op.create_index('ix_matters_status', 'matters', ['tenant_id', 'status'])


def downgrade():
    op.drop_index('ix_matters_status', 'matters')
    op.drop_table('user_permissions')
    op.drop_table('chinese_wall_exclusions')
    op.drop_index('ix_matters_owner', 'matters')
    op.drop_index('ix_matters_personal', 'matters')
    op.drop_constraint('fk_matters_owner_user', 'matters', type_='foreignkey')
    op.drop_column('matters', 'owner_user_id')
    op.drop_column('matters', 'is_personal')
    op.drop_index('ix_clients_owner', 'clients')
    op.drop_index('ix_clients_personal', 'clients')
    op.drop_constraint('fk_clients_owner_user', 'clients', type_='foreignkey')
    op.drop_column('clients', 'owner_user_id')
    op.drop_column('clients', 'is_personal')
