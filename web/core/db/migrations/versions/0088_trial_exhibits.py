"""0088: trial_exhibits -- Module A / Unit 2, the trial exhibit register.

Generalizes the deposition exhibit model (deposition_exhibit_links / viaticum_exhibits)
with the trial lifecycle the depo case lacks: marked -> offered -> admitted | excluded
| withdrawn, each with its page:line locus in the Reporter's Record. The actual
document is a metadata-ref into the DMS/eDiscovery (document_id, NO copy -- S3-005).
Admitted exhibits are part of the appellate record, so Module B pulls them by status.

Populated Tier-1 deterministically from the trial transcript (offer/admit/exclude
language + the court's ruling), editable thereafter.
"""
from alembic import op


revision = "0088_trial_exhibits"
down_revision = "0087_record_cite_checks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS trial_exhibits (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            matter_id          uuid,
            trial_id           uuid REFERENCES trial_proceedings(id) ON DELETE CASCADE,
            transcript_id      uuid,                  -- the volume where the event occurred
            party              varchar(20),           -- plaintiff | defendant | state | joint | court
            exhibit_number     varchar(20),           -- normalized, e.g. '12' or 'P-3'
            exhibit_label      text,                  -- as cited, e.g. "Plaintiff's Exhibit 12"
            document_id        uuid,                  -- metadata-ref into DMS/eDiscovery (NO copy)
            document_source    varchar(20),           -- dms | ediscovery
            sponsoring_witness varchar(200),
            status             varchar(15) NOT NULL DEFAULT 'marked',  -- marked|offered|admitted|excluded|withdrawn
            admitted           boolean NOT NULL DEFAULT false,
            marked_page        integer, marked_line   integer,
            offered_page       integer, offered_line  integer,
            ruling_page        integer, ruling_line   integer,
            offered_locus      text,
            ruling_locus       text,
            notes              text,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trial_exhibits_id "
               "ON trial_exhibits (trial_id, party, exhibit_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trial_exhibits_status "
               "ON trial_exhibits (trial_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trial_exhibits_transcript "
               "ON trial_exhibits (transcript_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS trial_exhibits")
