"""0084: brief_sections -- Module B / Unit 6, the TRAP 38.1 section model.

The appellate brief workspace is an ordered set of sections mirroring TRAP 38.1,
stored as rows so FRAP / a COA's local order swap by config. Each section holds its
editable body; the live word count, TOC, and the U5 TOA/compliance panels read off
these rows. word_excluded marks the sections TRAP 9.4(i)(1) excludes from the word
limit (caption/identity/ToC/index/statement-of-the-case/issues/certs/appendix), so
the counted total is accurate -- not the whole-document count.

drafting_outputs (the drafting engine) is reused at ASSEMBLE/PROMOTE time (U10): the
sections compile into a drafting_outputs row -> promote_draft to the matter DMS.
drafting_output_id links a section to a staged draft when AI-drafted (U9).
"""
from alembic import op


revision = "0084_brief_sections"
down_revision = "0083_appellate_rules_toa"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS brief_sections (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            appellate_case_id  uuid NOT NULL REFERENCES appellate_cases(id) ON DELETE CASCADE,
            section_key        varchar(50) NOT NULL,
            title              text NOT NULL,
            sort_order         integer NOT NULL DEFAULT 0,
            body               text NOT NULL DEFAULT '',
            included           boolean NOT NULL DEFAULT true,
            word_excluded      boolean NOT NULL DEFAULT false,
            word_count         integer NOT NULL DEFAULT 0,
            drafting_output_id uuid,
            updated_at         timestamptz NOT NULL DEFAULT now(),
            created_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_brief_sections_case_key "
               "ON brief_sections (appellate_case_id, section_key)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_brief_sections_case_order "
               "ON brief_sections (appellate_case_id, sort_order)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS brief_sections")
