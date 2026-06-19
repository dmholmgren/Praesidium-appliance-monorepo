"""0085: record_cite_checks -- Module B / Unit 7, record-cite verification.

The headline / patent candidate: for every factual assertion in the brief that
carries a record cite ([vol] RR [page] / CR [page]), semantically confirm (the
ModernBERT-768 space the record is already embedded in) that the cited record span
actually SUPPORTS the proposition -> flag supported / weak / unsupported / overbroad,
or unresolved (cite points at a volume/page not in the ingested record -- itself a
coverage gap worth surfacing). No bolt-on brief tool can do this; it requires holding
the record as page-addressed data, which U4 does.

One row per record cite occurrence in the brief.
"""
from alembic import op


revision = "0087_record_cite_checks"
down_revision = "0086_depo_schedule"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS record_cite_checks (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            appellate_case_id  uuid NOT NULL REFERENCES appellate_cases(id) ON DELETE CASCADE,
            brief_document_id  uuid,
            cite_text          text NOT NULL,        -- as written, e.g. '13 RR 111'
            cite_kind          varchar(10) NOT NULL, -- RR | CR
            locus              text,                 -- normalized locus from the resolver
            brief_page         integer,
            char_start         integer,
            char_end           integer,
            proposition        text,                 -- the cited sentence (cite stripped)
            record_span        text,                 -- the cited record text (snippet)
            similarity         numeric,              -- cosine in the 768-d space
            verdict            varchar(20) NOT NULL, -- supported | weak | unsupported | overbroad | unresolved
            reason             text,
            checked_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_record_cite_checks_case "
               "ON record_cite_checks (appellate_case_id, verdict)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS record_cite_checks")
