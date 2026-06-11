"""0028 — Deal Room: key documents, deal points, matter subjects

Adds three tables for the transactional matter homepage:

1. matter_key_documents — pinned documents on the deal homepage
   (contract, title commitment, survey, etc.) linked via DMS
   context menu "Link as Key Document" action.

2. matter_deal_points — negotiated terms with document provenance.
   Each row is a key-value pair traceable to the source clause.
   Amendment history via audit_log.

3. matter_subjects — type-specific asset/entity data for the
   matter (property details for real estate, company for M&A,
   borrower for lending, etc.). JSONB details column dispatches
   on matter type. One row per matter.

Revision ID: 0028_deal_room_tables
Revises: 0027_matter_permissions
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0028_deal_room_tables'
down_revision = '0027_matter_permissions'
branch_labels = None
depends_on = None


def upgrade():
    # ── matter_key_documents ──────────────────────────────────────────
    op.create_table(
        'matter_key_documents',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('matter_id', UUID(as_uuid=True),
                  sa.ForeignKey('matters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_id', UUID(as_uuid=True),
                  sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_role', sa.String(80), nullable=False),
        sa.Column('label', sa.String(255), nullable=True),
        sa.Column('display_order', sa.SmallInteger(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('linked_by', sa.BigInteger(), sa.ForeignKey('users.id'),
                  nullable=True),
        sa.Column('linked_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.UniqueConstraint('tenant_id', 'matter_id', 'document_id',
                            name='uq_mkd_tenant_matter_doc'),
    )
    op.create_index('ix_mkd_matter', 'matter_key_documents',
                    ['tenant_id', 'matter_id'])

    # Seed the canonical document_role values as a comment
    # Roles: contract, amendment, title_commitment, survey, closing_checklist,
    #        loi, financing_commitment, deed, assignment, easement, plat,
    #        environmental, appraisal, insurance, other

    # ── matter_deal_points ────────────────────────────────────────────
    op.create_table(
        'matter_deal_points',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('matter_id', UUID(as_uuid=True),
                  sa.ForeignKey('matters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('point_key', sa.String(100), nullable=False),
        sa.Column('point_label', sa.String(255), nullable=False),
        sa.Column('point_value', sa.Text(), nullable=True),
        sa.Column('point_type', sa.String(30), server_default=sa.text("'text'"),
                  nullable=False),
        sa.Column('source_document_id', UUID(as_uuid=True),
                  sa.ForeignKey('documents.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('source_clause', sa.String(100), nullable=True),
        sa.Column('display_order', sa.SmallInteger(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('updated_by', sa.BigInteger(), sa.ForeignKey('users.id'),
                  nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('tenant_id', 'matter_id', 'point_key',
                            name='uq_mdp_tenant_matter_key'),
    )
    op.create_index('ix_mdp_matter', 'matter_deal_points',
                    ['tenant_id', 'matter_id'])

    # point_type values: text, currency, date, percentage, integer, boolean
    # point_key examples: purchase_price, closing_date, earnest_money,
    #   financing_contingency_exp, due_diligence_exp, earnest_money_release,
    #   lease_commencement, option_period_exp, rollback_tax_responsibility,
    #   broker_commission, mineral_rights

    # ── matter_subjects ───────────────────────────────────────────────
    op.create_table(
        'matter_subjects',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('tenant_id', sa.String(36), nullable=False),
        sa.Column('matter_id', UUID(as_uuid=True),
                  sa.ForeignKey('matters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('subject_type', sa.String(50), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=False),
        sa.Column('address_line1', sa.String(255), nullable=True),
        sa.Column('address_line2', sa.String(255), nullable=True),
        sa.Column('city', sa.String(100), nullable=True),
        sa.Column('state', sa.String(50), nullable=True),
        sa.Column('zip_code', sa.String(20), nullable=True),
        sa.Column('county', sa.String(100), nullable=True),
        sa.Column('latitude', sa.Numeric(10, 7), nullable=True),
        sa.Column('longitude', sa.Numeric(10, 7), nullable=True),
        sa.Column('parcel_id', sa.String(100), nullable=True),
        sa.Column('legal_description', sa.Text(), nullable=True),
        sa.Column('acreage', sa.Numeric(12, 4), nullable=True),
        sa.Column('photo_document_id', UUID(as_uuid=True),
                  sa.ForeignKey('documents.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('details', JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column('data_sources', JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('tenant_id', 'matter_id',
                            name='uq_ms_tenant_matter'),
    )
    op.create_index('ix_ms_matter', 'matter_subjects',
                    ['tenant_id', 'matter_id'])
    op.create_index('ix_ms_parcel', 'matter_subjects',
                    ['tenant_id', 'parcel_id'],
                    postgresql_where=sa.text('parcel_id IS NOT NULL'))

    # subject_type values: real_property, company, borrower, vessel,
    #   intellectual_property, estate, trust, other
    #
    # details JSONB is type-dispatched:
    #   real_property: {appraised_value, tax_year, zoning, improvements_sqft,
    #                   land_sqft, year_built, school_district, flood_zone,
    #                   cad_url, deed_volume, deed_page}
    #   company: {ticker, industry, deal_size, entity_type, state_of_formation,
    #             sos_filing_number}
    #   borrower: {loan_amount, collateral_type, lien_position}


def downgrade():
    op.drop_table('matter_subjects')
    op.drop_table('matter_deal_points')
    op.drop_table('matter_key_documents')
