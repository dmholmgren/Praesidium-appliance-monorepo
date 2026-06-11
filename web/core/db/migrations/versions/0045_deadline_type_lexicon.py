"""0045 deadline_type_lexicon: seed a normalization lexicon for local-model deadline typing.

Context: date extraction now works (abbreviated-month parser fixed; structural engine
populating document_deadlines). The next layer is *typing*: local inference must map an
extracted deadline surface form -> a canonical deadline_type_library code, escalating to
the cloud model only when it cannot resolve confidently. Per the locked pipeline:

    local model  = extract + normalize (writes document_deadlines.deadline_type_id)
    escalate     = on no confident match, hand the one row to the cloud model
    deadline eng = runs SEPARATELY downstream off the typed data (rules/chain/calendar)

This migration creates the lexicon that local normalizes against, and seeds it from real
Texas + federal scheduling/docket-control-order language (incl. the verbatim titles HJMM's
Marcus consolidation order produced -- corpus-sourced, not textbook). Each surface form is
one row (atomic primitive), so the escalation loop can append learned rows and we can query
"what could local not classify". The library's `definition` column is also seeded as the
semantic anchor for the embedding-match tier.

Scope:
  * NEW TABLE deadline_type_lexicon (surface_form -> deadline_type_library.id), with a
      768-d embedding slot to live in the same local-ModernBERT space as 0044. HNSW
      deferred (no ANN index over an empty/NULL column -- per 0041/0043/0044).
  * UPDATE deadline_type_library.definition for the 17 canonical litigation types.
  * SEED ~90 surface forms (source='seed', tenant_id NULL = global). Idempotent
      (ON CONFLICT DO NOTHING); runtime appends source='learned'/'cloud_escalation'.

NOT touched: the 16 live Marcus rows in `deadlines` (their coarse deadline_type values are
re-normalized in a separate follow-up step, now that this lexicon exists).

asyncpg: one statement per op.execute(). tenant_id is varchar(36) here (not char(36)) to
avoid the trailing-space padding trap in the COALESCE unique index.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0045_deadline_type_lexicon"
down_revision = "0044_embed_768"
branch_labels = None
depends_on = None


# Recorded for the embedding pass to run once lexicon vectors are populated. NOT run here
# (deferring ANN indexes over empty columns, per 0041/0043/0044). 768-d, normalized,
# cosine -- same space as the firm/litigation/caselaw corpus.
DEFERRED_HNSW = [
    "CREATE INDEX IF NOT EXISTS ix_deadline_lexicon_embedding ON deadline_type_lexicon USING hnsw (embedding vector_cosine_ops)",
]


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS deadline_type_lexicon (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deadline_type_id uuid NOT NULL REFERENCES deadline_type_library(id) ON DELETE CASCADE,
    surface_form     text        NOT NULL,
    normalized_form  text        NOT NULL,
    match_type       varchar(20) NOT NULL DEFAULT 'phrase',   -- exact | phrase | keyword | regex
    weight           numeric(4,2) NOT NULL DEFAULT 1.00,      -- disambiguation priority
    jurisdiction     varchar(20),                             -- 'TX' | 'FED' | NULL = any
    source           varchar(20) NOT NULL DEFAULT 'seed',     -- seed | hjmm_corpus | learned | cloud_escalation
    tenant_id        varchar(36),                             -- NULL = global; set = tenant-learned
    embedding        vector(768),                             -- local ModernBERT space (0044); populated by embed pass
    is_active        boolean     NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
)
"""

# Unique on (type, normalized form, tenant) so the same phrase can be global AND
# tenant-learned, but never duplicated within a scope. COALESCE for the NULL global rows.
UX_NORM = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_deadline_lexicon_norm "
    "ON deadline_type_lexicon (deadline_type_id, normalized_form, COALESCE(tenant_id, ''))"
)
IX_NORM = (
    "CREATE INDEX IF NOT EXISTS ix_deadline_lexicon_norm "
    "ON deadline_type_lexicon (normalized_form)"
)
IX_TYPE = (
    "CREATE INDEX IF NOT EXISTS ix_deadline_lexicon_type "
    "ON deadline_type_lexicon (deadline_type_id)"
)

# Semantic anchor per canonical type (also the embedding source text for the match tier).
SEED_DEFINITIONS = """
UPDATE deadline_type_library AS t SET definition = v.def
FROM (VALUES
  ('trial_setting',          'Date the case is set for trial to the court or a jury.'),
  ('pretrial_conference',    'Final conference before the court shortly before trial to address remaining matters.'),
  ('pretrial_disclosures',   'Deadline to serve final pretrial disclosures of trial witnesses and exhibits (TRCP 194.4 / FRCP 26(a)(3)).'),
  ('pretrial_exchange',      'Deadline for parties to exchange trial exhibit and witness materials before trial (TRCP 166).'),
  ('pretrial_confer',        'Deadline for parties to confer regarding pretrial and trial materials.'),
  ('pretrial_motions',       'Deadline to file pretrial motions, including motions in limine.'),
  ('amended_pleadings',      'Deadline to amend pleadings or add claims.'),
  ('joinder_parties',        'Deadline to join additional parties.'),
  ('fact_discovery_close',   'Date fact discovery closes; the discovery cutoff.'),
  ('expert_desg_aff',        'Deadline for the party seeking affirmative relief to designate testifying experts.'),
  ('expert_desg_opp',        'Deadline for the party opposing affirmative relief to designate testifying experts.'),
  ('expert_desg_rebuttal',   'Deadline to designate rebuttal experts.'),
  ('expert_discovery_close', 'Date expert discovery closes.'),
  ('mediation',              'Deadline to complete mediation or alternative dispute resolution.'),
  ('msj_deadline',           'Deadline to file dispositive motions, including motions for summary judgment.'),
  ('daubert_deadline',       'Deadline to file motions to exclude or strike expert testimony (Daubert / Robinson).'),
  ('sol_expiry',             'Date the statute of limitations expires on a claim.')
) AS v(code, def)
WHERE t.code = v.code
"""

# ~90 surface forms. Verbatim Marcus-order titles carry weight 1.00; variants scaled down.
# normalized_form computed as lower(btrim(surface_form)) so local can do a cheap exact hit
# before falling back to the embedding tier.
SEED_LEXICON = """
INSERT INTO deadline_type_lexicon
    (deadline_type_id, surface_form, normalized_form, match_type, jurisdiction, weight, source)
SELECT l.id, v.sf, lower(btrim(v.sf)), 'phrase', v.jur, v.wt, 'seed'
FROM deadline_type_library l
JOIN (VALUES
  ('trial_setting','Trial Setting',NULL,1.00),
  ('trial_setting','Trial Date',NULL,0.95),
  ('trial_setting','Jury Trial Setting',NULL,0.95),
  ('trial_setting','Bench Trial Setting',NULL,0.95),
  ('trial_setting','Case Set for Trial',NULL,0.90),
  ('trial_setting','Trial Commences',NULL,0.85),
  ('trial_setting','Week of Trial',NULL,0.80),

  ('pretrial_conference','Formal Pre-Trial Conference',NULL,1.00),
  ('pretrial_conference','Pre-Trial Conference',NULL,1.00),
  ('pretrial_conference','Pretrial Conference',NULL,1.00),
  ('pretrial_conference','Final Pretrial Conference',NULL,0.95),
  ('pretrial_conference','Docket Call','TX',0.80),

  ('pretrial_disclosures','Pretrial Disclosures',NULL,1.00),
  ('pretrial_disclosures','Pre-Trial Disclosures',NULL,1.00),
  ('pretrial_disclosures','Pretrial Disclosures (Rule 194.4)','TX',1.00),
  ('pretrial_disclosures','Rule 194.4 Disclosures','TX',0.95),
  ('pretrial_disclosures','Rule 26(a)(3) Disclosures','FED',0.95),
  ('pretrial_disclosures','Final Witness and Exhibit Lists',NULL,0.85),

  ('pretrial_exchange','Exchange Bench Trial Materials',NULL,1.00),
  ('pretrial_exchange','Pre-Trial Exchange',NULL,0.95),
  ('pretrial_exchange','Pre-Trial Exchange (Rule 166)','TX',1.00),
  ('pretrial_exchange','Exchange Exhibit Lists',NULL,0.90),
  ('pretrial_exchange','Exchange Witness Lists',NULL,0.90),
  ('pretrial_exchange','Exchange of Trial Exhibits',NULL,0.90),
  ('pretrial_exchange','Rule 166 Exchange','TX',0.90),

  ('pretrial_confer','Confer on Trial Materials',NULL,1.00),
  ('pretrial_confer','Pre-Trial Confer',NULL,0.95),
  ('pretrial_confer','Meet and Confer on Pretrial Matters',NULL,0.85),
  ('pretrial_confer','Confer Regarding Trial Exhibits',NULL,0.85),

  ('pretrial_motions','Pre-trial Motions Filing Deadline',NULL,1.00),
  ('pretrial_motions','Pretrial Motions',NULL,0.90),
  ('pretrial_motions','Motions in Limine',NULL,0.90),
  ('pretrial_motions','Deadline to File Pretrial Motions',NULL,0.90),
  ('pretrial_motions','Motions in Limine Deadline',NULL,0.90),

  ('amended_pleadings','Amended Pleadings',NULL,1.00),
  ('amended_pleadings','Amended Pleadings Deadline',NULL,1.00),
  ('amended_pleadings','Deadline to Amend Pleadings',NULL,0.95),
  ('amended_pleadings','Leave to Amend Pleadings',NULL,0.85),
  ('amended_pleadings','Amend Pleadings',NULL,0.85),

  ('joinder_parties','Joinder of Parties',NULL,1.00),
  ('joinder_parties','Joinder of Additional Parties',NULL,0.95),
  ('joinder_parties','Deadline to Join Parties',NULL,0.90),
  ('joinder_parties','Add Additional Parties',NULL,0.85),
  ('joinder_parties','Joinder of Responsible Third Parties','TX',0.85),

  ('fact_discovery_close','Fact Discovery Closes',NULL,1.00),
  ('fact_discovery_close','Close of Fact Discovery',NULL,0.95),
  ('fact_discovery_close','Discovery Cutoff',NULL,0.90),
  ('fact_discovery_close','Discovery Deadline',NULL,0.85),
  ('fact_discovery_close','Completion of Discovery',NULL,0.85),
  ('fact_discovery_close','Fact Discovery Deadline',NULL,0.90),
  ('fact_discovery_close','Discovery Period Ends',NULL,0.80),

  ('expert_desg_aff','Expert Designations of Party Seeking Affirmative Relief',NULL,1.00),
  ('expert_desg_aff','Affirmative Expert Designations',NULL,0.95),
  ('expert_desg_aff','Expert Designation - Party with Burden of Proof',NULL,0.90),
  ('expert_desg_aff','Plaintiff''s Expert Designations',NULL,0.85),
  ('expert_desg_aff','Designation of Experts (Affirmative)',NULL,0.90),

  ('expert_desg_opp','Expert Designations of Party Opposing Affirmative Relief',NULL,1.00),
  ('expert_desg_opp','Responsive Expert Designations',NULL,0.90),
  ('expert_desg_opp','Defendant''s Expert Designations',NULL,0.85),
  ('expert_desg_opp','Designation of Experts (Opposing)',NULL,0.90),

  ('expert_desg_rebuttal','Designation of Rebuttal Experts',NULL,1.00),
  ('expert_desg_rebuttal','Rebuttal Expert Designations',NULL,1.00),
  ('expert_desg_rebuttal','Rebuttal Experts',NULL,0.90),

  ('expert_discovery_close','Expert Discovery Closes',NULL,1.00),
  ('expert_discovery_close','Close of Expert Discovery',NULL,0.95),
  ('expert_discovery_close','Expert Discovery Deadline',NULL,0.90),
  ('expert_discovery_close','Expert Discovery Cutoff',NULL,0.90),

  ('mediation','Mediation Required',NULL,1.00),
  ('mediation','Mediation Deadline',NULL,1.00),
  ('mediation','Complete Mediation',NULL,0.90),
  ('mediation','Mediation to be Completed',NULL,0.90),
  ('mediation','ADR Deadline',NULL,0.85),
  ('mediation','Alternative Dispute Resolution Deadline',NULL,0.85),

  ('msj_deadline','Motions for Summary Judgment',NULL,1.00),
  ('msj_deadline','MSJ Filing Deadline',NULL,1.00),
  ('msj_deadline','Dispositive Motion Deadline',NULL,0.95),
  ('msj_deadline','Summary Judgment Motions',NULL,0.90),
  ('msj_deadline','Deadline to File Dispositive Motions',NULL,0.90),
  ('msj_deadline','Motion for Summary Judgment Deadline',NULL,0.95),

  ('daubert_deadline','Motions to Exclude Expert Testimony',NULL,1.00),
  ('daubert_deadline','Daubert Motions','FED',0.95),
  ('daubert_deadline','Robinson Challenges','TX',0.90),
  ('daubert_deadline','Motions to Strike Experts',NULL,0.85),
  ('daubert_deadline','Challenges to Expert Testimony',NULL,0.85),
  ('daubert_deadline','Daubert/Robinson Deadline',NULL,0.90),

  ('sol_expiry','Statute of Limitations',NULL,1.00),
  ('sol_expiry','Limitations Period Expires',NULL,0.95),
  ('sol_expiry','Limitations Deadline',NULL,0.90),
  ('sol_expiry','SOL Expiration',NULL,0.85)
) AS v(code, sf, jur, wt) ON l.code = v.code
ON CONFLICT DO NOTHING
"""


UPGRADE_STATEMENTS = [
    CREATE_TABLE,
    UX_NORM,
    IX_NORM,
    IX_TYPE,
    SEED_DEFINITIONS,
    SEED_LEXICON,
]


DOWNGRADE_STATEMENTS = [
    "DROP TABLE IF EXISTS deadline_type_lexicon",
    # leave definitions in place on downgrade (reference data; non-destructive)
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
