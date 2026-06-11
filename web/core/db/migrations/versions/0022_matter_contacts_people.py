"""0022 — Matter Contacts People tab support

Add source/status/ai_summary to matter_contacts for proposal workflow.
Create contact_role_library for role picker.

Revision ID: 0022_matter_contacts_people
Revises: 0021_billing_settings
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_matter_contacts_people"
down_revision = "0021_billing_settings"
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Add columns to matter_contacts ──
    op.add_column("matter_contacts", sa.Column("source", sa.String(40), server_default="manual"))
    op.add_column("matter_contacts", sa.Column("status", sa.String(20), server_default="confirmed"))
    op.add_column("matter_contacts", sa.Column("ai_summary", sa.Text(), nullable=True))
    op.add_column("matter_contacts", sa.Column("category", sa.String(20), server_default="people"))
    # category: 'people' or 'witness' — drives which sub-tab the contact appears on

    # Index for fast per-matter people queries
    op.create_index("idx_mc_matter_status", "matter_contacts",
                    ["tenant_id", "matter_id", "status", "category"])

    # ── 2. Contact Role Library ──
    op.create_table(
        "contact_role_library",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),  # people | witness
        sa.Column("matter_type_scope", sa.ARRAY(sa.String), nullable=True),  # {litigation}, {transactional}, NULL=both
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean, server_default="true"),
    )

    # ── 3. Seed role library ──
    op.execute("""
        INSERT INTO contact_role_library (code, display_name, category, matter_type_scope, sort_order) VALUES
        -- People roles (both matter types)
        ('client_contact',    'Client Contact',       'people', NULL, 1),
        ('opposing_counsel',  'Opposing Counsel',     'people', NULL, 2),
        ('co_counsel',        'Co-Counsel',           'people', NULL, 3),
        ('judge',             'Judge',                'people', '{litigation}', 4),
        ('court_coordinator', 'Court Coordinator',    'people', '{litigation}', 5),
        ('court_clerk',       'Court Clerk',          'people', '{litigation}', 6),
        ('mediator',          'Mediator',             'people', '{litigation}', 7),
        ('arbitrator',        'Arbitrator',           'people', '{litigation}', 8),
        ('title_company',     'Title Company',        'people', '{transactional}', 10),
        ('escrow_agent',      'Escrow Agent',         'people', '{transactional}', 11),
        ('lender',            'Lender',               'people', '{transactional}', 12),
        ('borrower',          'Borrower',             'people', '{transactional}', 13),
        ('buyer',             'Buyer',                'people', '{transactional}', 14),
        ('seller',            'Seller',               'people', '{transactional}', 15),
        ('broker',            'Broker',               'people', '{transactional}', 16),
        ('surveyor',          'Surveyor',             'people', '{transactional}', 17),
        ('inspector',         'Inspector',            'people', '{transactional}', 18),
        ('appraiser',         'Appraiser',            'people', '{transactional}', 19),
        ('accountant',        'Accountant / CPA',     'people', '{transactional}', 20),
        ('financial_advisor', 'Financial Advisor',    'people', '{transactional}', 21),
        ('insurer',           'Insurer / Carrier',    'people', '{litigation}', 22),
        ('guardian_ad_litem', 'Guardian Ad Litem',    'people', '{litigation}', 23),
        ('regulator',         'Regulator',            'people', NULL, 24),
        ('vendor',            'Vendor',               'people', NULL, 25),
        ('other',             'Other',                'people', NULL, 99),
        -- Witness roles (litigation only)
        ('fact_witness',      'Fact Witness',         'witness', '{litigation}', 1),
        ('expert_witness',    'Expert Witness',       'witness', '{litigation}', 2),
        ('custodian',         'Custodian',            'witness', '{litigation}', 3),
        ('deponent',          'Deponent',             'witness', '{litigation}', 4),
        ('corporate_rep',     'Corporate Representative', 'witness', '{litigation}', 5),
        ('affiant',           'Affiant',              'witness', '{litigation}', 6)
    """)


def downgrade():
    op.drop_table("contact_role_library")
    op.drop_index("idx_mc_matter_status", "matter_contacts")
    op.drop_column("matter_contacts", "category")
    op.drop_column("matter_contacts", "ai_summary")
    op.drop_column("matter_contacts", "status")
    op.drop_column("matter_contacts", "source")
