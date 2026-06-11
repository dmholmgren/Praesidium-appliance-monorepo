"""Consolidated schema reconciliation + eDiscovery normalization.

Revision ID: 0036_consol_edisco
Revises: 0035_nav_section_tabs
Create Date: 2026-05-26

Consolidates 4 orphan stamps (0038_party_model, 0042_tasks_pm_schema,
0072_email_task_ext, 0080_prop_intel) into one head, and adds Phase 1
eDiscovery normalization columns (normalized_text, email_segments, etc).

Applied via SQL file: 0036_consolidated_ediscovery_norm.sql
"""

revision = '0036_consol_edisco'
down_revision = '0035_nav_section_tabs'

# Applied by SQL file — see 0036_consolidated_ediscovery_norm.sql
def upgrade():
    pass

def downgrade():
    pass