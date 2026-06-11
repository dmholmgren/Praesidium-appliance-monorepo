"""Dual calendar infrastructure.

Revision ID: 0037_dual_calendar
Revises: 0036_consol_edisco
Create Date: 2026-05-26

Adds dual calendar tables: calendar_events, exchange_calendar_events,
ai_calendar_events, calendar_crosscheck_log, calendar_divergence_log,
calendar_matter_assignments, firm_calendar_sources.

Applied via SQL file on 2026-05-26.
"""

revision = '0037_dual_calendar'
down_revision = '0036_consol_edisco'

def upgrade():
    pass  # Applied by SQL

def downgrade():
    pass
