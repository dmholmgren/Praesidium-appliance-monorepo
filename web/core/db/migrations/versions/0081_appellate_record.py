"""0081: appellate record substrate -- Module B / Unit 4.

The appellate brief workspace sits on top of THE RECORD ON APPEAL = Clerk's Record
(CR) + Reporter's Record (RR), both ingested as page-addressed canonical objects so
every factual assertion in the brief can carry a click-through, verifiable record
cite (the substrate win). The record is ingested through the EXISTING geometry
pipeline (geometry_io.build_geometry / extract_pdf_with_geometry) under a new
corpus='record' tag -- the same word/line/block bbox<->char-span tokens
(doc_layout_tokens) + canonical (doc_geometry) that drive DMS/eDiscovery sectioning
and viewer overlays. Geometry is what makes the sectioning + exact-span click-through
work, so the record reuses it rather than re-extracting flat text.

Tables added:

  appellate_cases   Module B parent: one row per appeal (COA, cause numbers, parties,
                    brief deadline). trial_id -> trial_proceedings is the RR's
                    provenance (the proceedings below), set when the RR is ingested
                    via Module A.

  record_documents  the record register: each CR/RR/supplement/appendix/brief as a
                    metadata-ref into the DMS (document_id, NO copy -- S3-005). The
                    is_record flag enforces the real TRAP trap: CR/RR are THE RECORD
                    (citable as fact); appendix (38.1(k)) and briefs are NOT.
                    geometry_corpus/geometry_doc_id point at the doc_layout_tokens /
                    doc_geometry geometry for this record doc; rr_transcript_id links
                    an RR to its Module A transcript (page:line).

  record_pages      citable-page index for the Clerk's Record, DERIVED FROM geometry
                    (not a second extraction): page_label/page_number = the clerk's
                    stamped page (detected from a footer integer token by bbox),
                    char_start/char_end = the page's span into doc_geometry.canonical_text,
                    pageno_bbox = where the stamp sits. A record cite 'CR [page]'
                    resolves here -> the char span + its doc_layout_tokens (bbox) for
                    click-through. (RR cites 'RR [page:line]' resolve via Module A's
                    transcript_lines; RR geometry tokens supply the bbox.)

Additive + idempotent.
"""
from alembic import op


revision = "0081_appellate_record"
down_revision = "0080_trial_transcript_substrate"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS appellate_cases (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              char(36) NOT NULL,
            matter_id              uuid,
            trial_id               uuid REFERENCES trial_proceedings(id) ON DELETE SET NULL,
            court_of_appeals       varchar(200),
            appellate_cause_number varchar(120),
            trial_court            varchar(200),
            trial_cause_number     varchar(120),
            style                  text,
            appellant              varchar(300),
            appellee               varchar(300),
            jurisdiction           varchar(40) NOT NULL DEFAULT 'TX-TRAP',
            brief_deadline         date,
            status                 varchar(30) NOT NULL DEFAULT 'active',
            notes                  text,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_appellate_cases_matter "
               "ON appellate_cases (matter_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS record_documents (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         char(36) NOT NULL,
            appellate_case_id uuid NOT NULL REFERENCES appellate_cases(id) ON DELETE CASCADE,
            matter_id         uuid,
            record_kind       varchar(20) NOT NULL,   -- CR, SUPP_CR, RR, SUPP_RR, APPENDIX, BRIEF, MOTION, OTHER
            is_record         boolean NOT NULL DEFAULT false,  -- CR/RR(+supp) only; appendix/brief are NOT the record
            document_id       uuid,                   -- metadata-ref into documents (NO copy)
            geometry_corpus   varchar(20),            -- corpus tag for this doc's geometry (='record')
            geometry_doc_id   uuid,                   -- doc_id key into doc_layout_tokens/doc_geometry
            rr_transcript_id  uuid,                   -- -> deposition_transcripts when RR ingested via Module A
            volume            integer,
            label             text,
            storage_path      text,
            page_first        integer,
            page_last         integer,
            page_count        integer,
            geometry_status   varchar(30) NOT NULL DEFAULT 'pending',
            status            varchar(30) NOT NULL DEFAULT 'pending',
            sort_order        integer NOT NULL DEFAULT 0,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_record_documents_case "
               "ON record_documents (appellate_case_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_record_documents_case_doc "
               "ON record_documents (appellate_case_id, document_id) "
               "WHERE document_id IS NOT NULL")

    op.execute("""
        CREATE TABLE IF NOT EXISTS record_pages (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            record_document_id uuid NOT NULL REFERENCES record_documents(id) ON DELETE CASCADE,
            appellate_case_id  uuid NOT NULL,
            pdf_page           integer NOT NULL,        -- 1-based PDF page (geometry page_number)
            page_label         varchar(40),             -- the clerk's stamped page as cited ('123', '7')
            page_number        integer,                 -- normalized citable CR page when numeric
            char_start         integer NOT NULL DEFAULT 0,  -- span into doc_geometry.canonical_text
            char_end           integer NOT NULL DEFAULT 0,
            pageno_bbox        jsonb,                   -- bbox of the detected page-number stamp (calibration)
            created_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_record_pages_doc_pdfpage "
               "ON record_pages (record_document_id, pdf_page)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_record_pages_case_pageno "
               "ON record_pages (appellate_case_id, page_number)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS record_pages")
    op.execute("DROP TABLE IF EXISTS record_documents")
    op.execute("DROP TABLE IF EXISTS appellate_cases")
