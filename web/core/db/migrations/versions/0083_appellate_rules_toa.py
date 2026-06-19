"""0083: appellate compliance rules (data) + brief authorities (auto-TOA) -- U5.

Two halves of Module B / Unit 5:

  appellate_rules    Compliance is DATA, never hardcoded (so a COA's local rules /
                     FRAP swap by config). One row per (jurisdiction, doc_type,
                     rule_key): word limits (TRAP 9.4(i)), body/footnote font (9.4(e)),
                     required sections (38.1), mandatory appendix contents (38.1(k)(1)).
                     Seeded TX-TRAP (primary) + US-FRAP (alternate). verified=false
                     until the live rule text is checked vs txcourts.gov / uscourts.gov
                     (numbers change; the seed is the single point to update).

  brief_authorities  The auto-Table-of-Authorities, compiled deterministically from
                     the citation extractor (find_citations / CITATION_PATTERNS) over
                     the brief draft: one row per distinct authority with its TOA group
                     (Cases/Statutes/Rules/Constitutional/Other), case name, the brief
                     pages it appears on, occurrence count, and passim (>=5). Rebuilt
                     on demand; this is the appellate designation-report -- the manual
                     TOA grind, gone.
"""
from alembic import op


revision = "0083_appellate_rules_toa"
down_revision = "0082_depo_sync_meta"
branch_labels = None
depends_on = None


_SEED = [
    # (jurisdiction, doc_type, rule_key, value_json, rule_cite, is_primary)
    ("TX-TRAP", "brief_appellant", "word_limit", '15000', "TRAP 9.4(i)(2)(B)", True),
    ("TX-TRAP", "brief_appellee", "word_limit", '15000', "TRAP 9.4(i)(2)(B)", True),
    ("TX-TRAP", "brief_reply", "word_limit", '7500', "TRAP 9.4(i)(2)(C)", True),
    ("TX-TRAP", "petition", "word_limit", '4500', "TRAP 9.4(i)(2)(D)", True),
    ("TX-TRAP", "*", "body_font_pt", '14', "TRAP 9.4(e)", True),
    ("TX-TRAP", "*", "footnote_font_pt", '12', "TRAP 9.4(e)", True),
    ("TX-TRAP", "brief_appellant", "required_sections",
     '["identity_of_parties","table_of_contents","index_of_authorities",'
     '"statement_of_the_case","statement_on_oral_argument","issues_presented",'
     '"statement_of_facts","summary_of_the_argument","argument","prayer",'
     '"certifications","appendix"]', "TRAP 38.1", True),
    ("TX-TRAP", "brief_appellant", "appendix_contents",
     '["judgment_or_order_appealed","jury_charge_and_verdict_or_findings_and_conclusions",'
     '"text_of_law_relied_on","central_contract_or_document"]', "TRAP 38.1(k)(1)", True),
    # FRAP alternate (documented; swap by config for a future federal appeal)
    ("US-FRAP", "brief_principal", "word_limit", '13000', "FRAP 32(a)(7)(B)(i)", False),
    ("US-FRAP", "brief_reply", "word_limit", '6500', "FRAP 32(a)(7)(B)(ii)", False),
    ("US-FRAP", "*", "body_font_pt", '14', "FRAP 32(a)(5)", False),
    ("US-FRAP", "brief_principal", "required_sections",
     '["corporate_disclosure","table_of_contents","table_of_authorities",'
     '"jurisdictional_statement","statement_of_issues","statement_of_the_case",'
     '"summary_of_argument","argument","conclusion","certificates"]',
     "FRAP 28(a)", False),
]


def _q(v):
    return "'" + str(v).replace("'", "''") + "'"


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS appellate_rules (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            jurisdiction varchar(40) NOT NULL,
            doc_type     varchar(40) NOT NULL DEFAULT '*',
            rule_key     varchar(60) NOT NULL,
            rule_value   jsonb NOT NULL,
            rule_cite    varchar(120),
            is_primary   boolean NOT NULL DEFAULT true,
            verified     boolean NOT NULL DEFAULT false,
            notes        text,
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_appellate_rules_key "
               "ON appellate_rules (jurisdiction, doc_type, rule_key)")

    for (jur, dt, key, val, cite, prim) in _SEED:
        op.execute(
            "INSERT INTO appellate_rules (jurisdiction, doc_type, rule_key, rule_value, "
            "  rule_cite, is_primary, verified, notes) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s, false, "
            "  'seed value -- verify current rule text vs txcourts.gov/uscourts.gov') "
            "ON CONFLICT (jurisdiction, doc_type, rule_key) DO NOTHING" %
            tuple(_q(x) for x in (jur, dt, key, val, cite, ("true" if prim else "false"))))

    op.execute("""
        CREATE TABLE IF NOT EXISTS brief_authorities (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          char(36) NOT NULL,
            appellate_case_id  uuid NOT NULL REFERENCES appellate_cases(id) ON DELETE CASCADE,
            brief_document_id  uuid,
            toa_group          varchar(20) NOT NULL,   -- Cases | Statutes | Rules | Constitutional | Other
            citation_type      varchar(30) NOT NULL,   -- case | statute | rule | constitution | regulation
            citation_text      text NOT NULL,          -- normalized reporter/statute cite
            case_name          text,                   -- 'X v. Y' for cases, when recovered
            toa_label          text,                   -- the full TOA line (name + cite + parenthetical)
            first_brief_page   integer,
            brief_pages        integer[],
            occurrences        integer NOT NULL DEFAULT 1,
            passim             boolean NOT NULL DEFAULT false,
            reference_opinion_id uuid,                  -- CourtListener link (U9)
            sort_key           text,
            built_at           timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_brief_authorities_case "
               "ON brief_authorities (appellate_case_id, toa_group, sort_key)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_brief_authorities_cite "
               "ON brief_authorities (appellate_case_id, brief_document_id, citation_text)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS brief_authorities")
    op.execute("DROP TABLE IF EXISTS appellate_rules")
