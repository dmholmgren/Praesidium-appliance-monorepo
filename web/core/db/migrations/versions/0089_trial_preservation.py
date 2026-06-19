"""0089: trial_preservation -- Module A / Unit 3, the objection<->ruling index.

Error preservation (Tex. R. App. P. 33.1) is the spine of an appeal: a complaint
is preserved only if the record shows a timely, specific objection (or offer of
proof, TRE 103) AND a ruling -- or a refusal to rule. This table indexes every
trial objection, its grounds, the court's ruling, and the page:line loci, so
Module B (U8) can check each appellate complaint against the record's preservation
posture rather than the lawyer's say-so.

Populated Tier-1 deterministically from the transcript (objection language +
the court's next ruling utterance + running-objection / offer-of-proof / motion-
in-limine flags), editable thereafter.
"""
from alembic import op


revision = "0089_trial_preservation"
down_revision = "0088_trial_exhibits"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS trial_preservation (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         char(36) NOT NULL,
            matter_id         uuid,
            trial_id          uuid REFERENCES trial_proceedings(id) ON DELETE CASCADE,
            transcript_id     uuid,
            objector_speaker  varchar(120),          -- "MR. HOLMGREN"
            objector_party    varchar(20),           -- plaintiff|defendant (best-effort, editable)
            witness           varchar(200),          -- witness on the stand, if known
            grounds           text,                  -- normalized ground codes, comma-list
            objection_text    text,                  -- raw objection utterance
            objection_page    integer, objection_line integer,
            objection_locus   text,
            ruled             boolean NOT NULL DEFAULT false,
            ruling            varchar(20),           -- sustained|overruled|carried|withdrawn|granted|denied
            ruling_text       text,
            ruling_page       integer, ruling_line  integer,
            ruling_locus      text,
            running           boolean NOT NULL DEFAULT false,   -- running/continuing objection
            motion_in_limine  boolean NOT NULL DEFAULT false,
            offer_of_proof    boolean NOT NULL DEFAULT false,   -- offer of proof / bill of exception
            stricken          boolean NOT NULL DEFAULT false,
            preserved         varchar(15),           -- yes|unclear|no (Tier-1 heuristic)
            notes             text,
            source            varchar(15) NOT NULL DEFAULT 'auto',  -- auto|manual
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trial_preservation_locus "
               "ON trial_preservation (transcript_id, objection_page, objection_line)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trial_preservation_trial "
               "ON trial_preservation (trial_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trial_preservation_ruling "
               "ON trial_preservation (trial_id, ruling)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS trial_preservation")
