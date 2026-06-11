"""Matter party model — library-constrained roles, GF numbers, identifiers.

Revision ID: 0038_party_model
Revises: 0037_dual_calendar
Create Date: 2026-05-27

Schema changes:
  1. contact_role_library: add party spine (plaintiff, defendant, petitioner,
     respondent, intervenor, third_party, party), transactional roles
     (guarantor, notary, qualified_intermediary), logistics (court_reporter,
     process_server). Fix NULL matter_type_scope on 6 existing rows.
  2. matters: add gf_number varchar(64) with partial index.
  3. matter_identifiers: new table for typed identifier pairs (gf_number,
     loan_number, policy_number, escrow_number, filing_number, etc.).
  4. matter_contacts: add role_code (FK-like to contact_role_library.code),
     secondary_role_code, is_client_side boolean, is_signatory, signing_authority,
     confidence numeric. Partial indexes on role_code and is_client_side.

Backfill:
  - Existing free-text role values matching library codes -> role_code set
  - 324 'client' rows -> is_client_side=true, role_code NULL, queued as
    role_reclassify proposals in matter_contact_proposals for re-extraction
  - Alias mappings: expert->expert_witness, witness->fact_witness

Extractor v2 (matter_extract.py):
  - Prompts constrained to library role codes with is_client_side + confidence
  - GF number extraction from title commitments -> matters.gf_number
  - Cause number + identifiers -> matters.cause_number + matter_identifiers
  - DMS fallback via matter_folders.disk_root for matter-refresh endpoint
  - Intelligence extraction hook wired into dms_extract_job.run_extract_single()
  - backfill_intelligence.py job for bulk re-extraction across all matters

Applied via SQL file: 0038_matter_party_model.sql on 2026-05-27.
"""

revision = '0038_party_model'
down_revision = '0037_dual_calendar'

def upgrade():
    pass  # Applied by SQL

def downgrade():
    pass
