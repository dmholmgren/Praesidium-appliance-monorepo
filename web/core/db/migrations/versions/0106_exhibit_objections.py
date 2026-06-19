"""trial_exhibit_objections + derived color-state view.

Scope v2 §2.4 / §4.3 / build-sequence step 2.

An objection is an *overlay* on an exhibit, not a peer of offered/admitted/withdrawn
(those live in trial_exhibits.status). Opposing counsel's *served* objections are
loaded up front so contested exhibits are visible pretrial (source='pretrial_served');
they resolve live at trial (source='trial', ruling filled in).

Color states (§4.3, do NOT overload status -- varchar(15) won't hold
"conditionally_admitted"):
    red    = not admitted
    yellow = conditional (flag) or an objection carried/conditional
    green  = admitted
Encoded as a *derived view* (trial_exhibit_state) over a new `conditional` bool on
trial_exhibits (Open item C: derived view + conditional bool).

Revision ID: 0106_exhibit_objections
Revises: 0105_document_identifiers
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0106_exhibit_objections"
down_revision = "0105_document_identifiers"
branch_labels = None
depends_on = None


def upgrade():
    # 1. conditional flag on the register (yellow state; status stays varchar(15)).
    op.add_column("trial_exhibits",
                  sa.Column("conditional", sa.Boolean(), nullable=False,
                            server_default=sa.text("false")))

    # 2. the objection overlay.
    op.create_table(
        "trial_exhibit_objections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("exhibit_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("objecting_party", sa.String(length=20)),       # plaintiff|defendant|...
        sa.Column("basis", sa.Text()),                            # hearsay, 403, authentication, ...
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default=sa.text("'pretrial_served'")),   # pretrial_served | trial
        sa.Column("ruling", sa.String(length=20), nullable=False,
                  server_default=sa.text("'no_ruling'")),         # sustained|overruled|carried|conditional|no_ruling
        sa.Column("ruling_locus", sa.Text()),                     # page:line of the ruling
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_teo_exhibit", "trial_exhibit_objections", ["exhibit_id"])

    # 3. derived color-state view (single source of truth for the UI traffic light).
    op.execute("""
        CREATE OR REPLACE VIEW trial_exhibit_state AS
        SELECT
            te.id            AS exhibit_id,
            te.tenant_id,
            te.matter_id,
            te.trial_id,
            te.party,
            te.exhibit_number,
            te.status,
            te.admitted,
            te.conditional,
            CASE
                WHEN te.admitted THEN 'green'
                WHEN te.conditional
                  OR EXISTS (SELECT 1 FROM trial_exhibit_objections o
                             WHERE o.exhibit_id = te.id
                               AND o.ruling IN ('conditional', 'carried'))
                    THEN 'yellow'
                ELSE 'red'
            END AS color_state,
            COALESCE((SELECT count(*) FROM trial_exhibit_objections o
                      WHERE o.exhibit_id = te.id), 0)              AS objection_count,
            COALESCE((SELECT count(*) FROM trial_exhibit_objections o
                      WHERE o.exhibit_id = te.id
                        AND o.ruling IN ('no_ruling', 'carried', 'conditional')), 0)
                                                                   AS open_objection_count,
            COALESCE((SELECT count(*) FROM trial_exhibit_objections o
                      WHERE o.exhibit_id = te.id AND o.ruling = 'sustained'), 0)
                                                                   AS sustained_count
        FROM trial_exhibits te
    """)


def downgrade():
    op.execute("DROP VIEW IF EXISTS trial_exhibit_state")
    op.drop_index("ix_teo_exhibit", table_name="trial_exhibit_objections")
    op.drop_table("trial_exhibit_objections")
    op.drop_column("trial_exhibits", "conditional")
