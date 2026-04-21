"""0028_connector_fields

Fix connector_registry config_fields and credential_fields for all connectors.
Timeslips gets full Firebird + agent config fields — retires hardcoded route.
Adds description field updates for connectors missing good descriptions.

Revision ID: 0028_connector_fields
Revises: 0027_connector_group
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa
import json

revision = '0028_connector_fields'
down_revision = '0027_connector_group'
branch_labels = None
depends_on = None


def upgrade():

    # ── Timeslips — full field set, retiring hardcoded route ──────────────────
    op.execute("""
        UPDATE connector_registry SET
            display_name = 'Sage Timeslips',
            description  = 'Live billing sync from Sage Timeslips via Windows agent on Timeslips VM. Syncs time entries, clients, invoices, and payments.',
            config_fields = %s::jsonb,
            credential_fields = %s::jsonb
        WHERE connector_type = 'timeslips'
    """ % (
        "'" + json.dumps([
            {"name": "fb_host",      "type": "text",   "label": "Timeslips Server Hostname or IP", "required": True,  "placeholder": "192.168.1.x or timeslips-vm"},
            {"name": "fb_database",  "type": "text",   "label": "Firebird Database Path",          "required": True,  "placeholder": "C:\\Timeslips\\ts2022.fdb"},
            {"name": "fb_user",      "type": "text",   "label": "Firebird Username",               "required": True,  "placeholder": "SYSDBA"},
            {"name": "import_from",  "type": "text",   "label": "Import From Date",                "required": False, "placeholder": "2020-01-01", "help": "Leave blank to import all history"},
            {"name": "sync_frequency","type": "select","label": "Sync Frequency",                  "required": True,
             "options": [
                 {"value": "hourly",  "label": "Hourly"},
                 {"value": "daily",   "label": "Daily"},
                 {"value": "manual",  "label": "Manual only"}
             ]},
        ]).replace("'", "''") + "'",
        "'" + json.dumps([
            {"name": "fb_password",    "type": "password", "label": "Firebird Password",  "secret": True, "required": True,  "help": "SYSDBA password for Firebird database"},
            {"name": "ingest_api_key", "type": "password", "label": "Ingest API Key",     "secret": True, "required": False, "help": "Auto-generated on first save. Used by Windows agent to authenticate ingest calls."},
        ]).replace("'", "''") + "'"
    ))

    # ── Windows Agent — add recursive flag and exclusion patterns ─────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'Local file indexing agent running on Windows Server. Direct NTFS access — no CIFS overhead. Syncs documents into DMS and eDiscovery.',
            config_fields = %s::jsonb,
            credential_fields = %s::jsonb
        WHERE connector_type = 'windows_agent'
    """ % (
        "'" + json.dumps([
            {"name": "watch_folders",  "type": "text",     "label": "Watch Folders",     "required": True,  "placeholder": "D:\\Public\\Clients", "help": "Semicolon-separated list of folders to index"},
            {"name": "batch_size",     "type": "number",   "label": "Batch Size",        "required": False, "placeholder": "250",                 "help": "Files per sync batch. Default 250."},
            {"name": "exclude_patterns","type": "textarea","label": "Exclude Patterns",  "required": False, "placeholder": "*.tmp\n*.lnk\nThumb.db","help": "One pattern per line. Wildcards supported."},
        ]).replace("'", "''") + "'",
        "'" + json.dumps([
            {"name": "ingest_api_key", "type": "password", "label": "Ingest API Key", "secret": True, "required": True, "help": "Generated on first save. Paste into agent installer."},
        ]).replace("'", "''") + "'"
    ))

    # ── Exchange — add sync scope options ─────────────────────────────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'On-premises Microsoft Exchange via EWS. Monitors mailboxes for client communications, feeds AI timesheet and billing reconciliation.',
            config_fields = %s::jsonb
        WHERE connector_type = 'exchange'
    """ % (
        "'" + json.dumps([
            {"name": "ews_url",   "type": "text",   "label": "EWS URL",         "required": True,  "placeholder": "https://mail.firm.com/EWS/Exchange.asmx"},
            {"name": "mailbox",   "type": "text",   "label": "Mailbox",         "required": True,  "placeholder": "attorney@firm.com"},
            {"name": "sync_scope","type": "select", "label": "Sync Scope",      "required": True,
             "options": [
                 {"value": "inbox",    "label": "Inbox only"},
                 {"value": "sent",     "label": "Sent items only"},
                 {"value": "all_mail", "label": "All mail folders"},
             ]},
            {"name": "days_back", "type": "number", "label": "Initial Lookback (days)", "required": False, "placeholder": "90", "help": "How many days back to sync on first run"},
        ]).replace("'", "''") + "'"
    ))

    # ── File Crawler — add auth fields ────────────────────────────────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'Legacy CIFS/SMB share crawler. Superseded by Windows Agent for bulk indexing. Use for network shares not accessible from SERVER1.',
            config_fields = %s::jsonb,
            credential_fields = %s::jsonb
        WHERE connector_type = 'file_crawler'
    """ % (
        "'" + json.dumps([
            {"name": "share_path", "type": "text", "label": "Share Path",  "required": True,  "placeholder": "\\\\server\\share"},
            {"name": "domain",     "type": "text", "label": "Domain",      "required": False, "placeholder": "FIRM"},
            {"name": "username",   "type": "text", "label": "Username",    "required": False, "placeholder": "svc_crawler"},
        ]).replace("'", "''") + "'",
        "'" + json.dumps([
            {"name": "password", "type": "password", "label": "Password", "secret": True, "required": False},
        ]).replace("'", "''") + "'"
    ))

    # ── CourtListener — add api_key field ─────────────────────────────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'Federal court data — dockets, opinions, and filings. Seeds deadline engine and judicial intelligence.',
            config_fields = %s::jsonb,
            credential_fields = %s::jsonb
        WHERE connector_type = 'courtlistener'
    """ % (
        "'" + json.dumps([
            {"name": "sync_scope", "type": "select", "label": "Sync Scope", "required": True,
             "options": [
                 {"value": "active_matters", "label": "Active matters only"},
                 {"value": "all_matters",    "label": "All matters"},
             ]},
        ]).replace("'", "''") + "'",
        "'" + json.dumps([
            {"name": "api_token", "type": "password", "label": "API Token", "secret": True, "required": True, "help": "From courtlistener.com account settings"},
        ]).replace("'", "''") + "'"
    ))

    # ── ManicTime — description update ───────────────────────────────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'Desktop activity tracking via ManicTime Server. Feeds AI timesheet reconciliation with application and document usage data.'
        WHERE connector_type = 'manictime'
    """)

    # ── FreePBX CDR — description update + improve fields ────────────────────
    op.execute("""
        UPDATE connector_registry SET
            description = 'Call detail records from FreePBX or carrier CSV export. Feeds billing reconciliation — maps calls to matters for time entry suggestions.',
            config_fields = %s::jsonb,
            credential_fields = %s::jsonb
        WHERE connector_type = 'pbx_cdr'
    """ % (
        "'" + json.dumps([
            {"name": "pbx_url",     "type": "text",   "label": "FreePBX URL",     "required": False, "placeholder": "https://pbx.firm.com", "help": "Leave blank for CSV-only import"},
            {"name": "sync_mode",   "type": "select", "label": "Sync Mode",       "required": True,
             "options": [
                 {"value": "api",  "label": "Live API pull (FreePBX)"},
                 {"value": "csv",  "label": "CSV import only"},
             ]},
        ]).replace("'", "''") + "'",
        "'" + json.dumps([
            {"name": "api_key", "type": "password", "label": "API Key", "secret": True, "required": False, "help": "Required for live API pull mode only"},
        ]).replace("'", "''") + "'"
    ))


def downgrade():
    # Revert to original sparse field definitions — not worth tracking exactly,
    # just clear fields on connectors we touched
    op.execute("UPDATE connector_registry SET config_fields = '[]'::jsonb, credential_fields = '[]'::jsonb WHERE connector_type IN ('timeslips','windows_agent','exchange','file_crawler','courtlistener','pbx_cdr')")
