"""matter_contact_proposals: proposals queue for contact-matter linking

Revision ID: 0008_matter_contact_proposals
Revises: 0007_contact_hygiene_aux
Create Date: 2026-05-05

Adds the queue that the matter_contact_linker job (Phase B1) writes to
when it identifies probable matter-contact relationships from existing
signals (email_routing_queue.matched_matter_id, ts_slips → ts_clients,
direct keyword matches).

DESIGN
======

A row represents a proposed link between ONE contact and ONE matter, with
a confidence score and an explanation of which signals supported it.

This is intentionally structured differently from contact_dedup_candidates
(which represents a CLUSTER per row): here, each pair is independent and
human review is per-pair. The same contact can have multiple pending
proposals (linked to different matters), and the same matter can have
multiple pending contact proposals. UNIQUE constraint prevents double-
proposing the same contact-matter pair while pending.

PROPOSED ROLES
--------------
The 'role' field follows matter_contacts.role conventions: 'client',
'opposing_counsel', 'witness', 'expert', 'co_counsel', 'paralegal',
'referred_by', 'unknown'. The linker derives role from the signal source:
  - ts_clients direct match  -> 'client'
  - ts_clients.opp_counsel   -> 'opposing_counsel'
  - ts_clients.paralegal     -> 'paralegal'
  - ts_clients.referred_by   -> 'referred_by'
  - email from_email match   -> 'unknown' (need human disambiguation)

PER YOUR DECISION (May 5 2026):
- Auto-link only obvious 1.0-confidence signals (e.g. exact ts_email
  match in ts_clients with single mapping). Lower confidence requires
  human review.
- HOWEVER, even auto-link writes a proposal row first with status
  'auto_approved' and timestamps. The promotion to matter_contacts
  is a separate atomic step (handled in B1) that records who/what
  promoted it. This preserves the audit trail.

REVIEW STATUSES
---------------
- 'pending'        : Awaiting human review. Default.
- 'approved'       : Reviewer approved; promoted to matter_contacts.
- 'rejected'       : Reviewer rejected; not a real link.
- 'auto_approved'  : Linker auto-promoted (1.0-confidence signals only).
                     Still produces a matter_contacts row, but the
                     proposal record persists for audit.
- 'superseded'     : Another proposal for the same pair won out
                     (e.g. higher confidence later).

This table is per-tenant. tenant_id is the standard varchar(36)
trim-prone column.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0008_matter_contact_proposals"
down_revision = "0007_contact_hygiene_aux"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "matter_contact_proposals",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("(uuid_generate_v4())::text"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(36), nullable=False),

        # The pair being proposed
        sa.Column(
            "contact_id",
            sa.BigInteger(),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "matter_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("matters.id", ondelete="CASCADE"),
            nullable=False,
        ),

        # Proposed role for the matter_contacts row that would be created
        sa.Column("proposed_role", sa.String(64), nullable=True),
        sa.Column(
            "proposed_is_primary",
            sa.String(1),
            nullable=False,
            server_default="N",
            comment="'Y' or 'N' to match matter_contacts.is_primary convention",
        ),

        # Signal that produced this proposal. Loose vocabulary, not CHECK.
        # Examples:
        #   'ts_clients_direct'     - ts_clients.ts_name matched contact.full_name
        #   'ts_clients_email'      - ts_clients.ts_email = contacts.email
        #   'ts_clients_phone'      - ts_clients.ts_phone = contacts.phone
        #   'ts_clients_opp_counsel'- ts_clients.opp_counsel matched contact
        #   'email_from'            - email_routing_queue.from_email
        #   'email_to'              - email_routing_queue.to_emails
        #   'email_cc'              - email_routing_queue.cc_emails
        sa.Column("signal_type", sa.String(64), nullable=False),

        # 0.000-1.000
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),

        # How many independent signals support this pair (combined later
        # via aggregation when Phase B is re-run; higher = stronger).
        sa.Column(
            "signal_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),

        # Free-text explanation of what was matched (audit aid).
        sa.Column("notes", sa.Text(), nullable=True),

        # Review state ----------------------------------------------------
        # 'pending' | 'approved' | 'rejected' | 'auto_approved' | 'superseded'
        sa.Column(
            "review_status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),

        # Promotion tracking --------------------------------------------
        # When a proposal is auto_approved or approved and promoted to
        # matter_contacts, we record the matter_contacts.id of the row
        # we created. Allows reverse-lookup ("which proposal led to this
        # matter_contacts row?") and prevents double-promotion.
        sa.Column(
            "promoted_matter_contact_id",
            sa.BigInteger(),
            nullable=True,
            comment="Set after promotion to matter_contacts",
        ),
        sa.Column(
            "promoted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        # Run audit
        sa.Column(
            "linker_run_id",
            sa.Text(),
            nullable=True,
            comment="Stable id per linker run for grouping",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---------------- Indexes ----------------
    # Look up pending proposals by tenant + matter (for matter detail UI)
    op.create_index(
        "ix_mcp_tenant_matter_pending",
        "matter_contact_proposals",
        ["tenant_id", "matter_id"],
        postgresql_where=sa.text("review_status = 'pending'"),
    )
    # Look up pending proposals by tenant + contact (for contact detail UI)
    op.create_index(
        "ix_mcp_tenant_contact_pending",
        "matter_contact_proposals",
        ["tenant_id", "contact_id"],
        postgresql_where=sa.text("review_status = 'pending'"),
    )
    # Tenant-wide review queue (sort by confidence desc, signal_count desc)
    op.create_index(
        "ix_mcp_tenant_status_confidence",
        "matter_contact_proposals",
        ["tenant_id", "review_status"],
    )
    # Per-run grouping
    op.create_index(
        "ix_mcp_run",
        "matter_contact_proposals",
        ["linker_run_id"],
    )

    # ---------------- Pair uniqueness ----------------
    # Prevent the same (tenant, contact, matter) pair from having multiple
    # PENDING proposals. New runs that re-detect the same pair should
    # update the existing pending row (signal_count++, possibly higher
    # confidence) rather than insert a duplicate.
    #
    # Partial unique index achieves this without blocking historical
    # rows (approved/rejected/superseded) from coexisting with a fresh
    # pending row in case a reviewer wants to reconsider.
    op.create_index(
        "uq_mcp_pending_pair",
        "matter_contact_proposals",
        ["tenant_id", "contact_id", "matter_id"],
        unique=True,
        postgresql_where=sa.text("review_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_mcp_pending_pair", table_name="matter_contact_proposals")
    op.drop_index("ix_mcp_run", table_name="matter_contact_proposals")
    op.drop_index(
        "ix_mcp_tenant_status_confidence",
        table_name="matter_contact_proposals",
    )
    op.drop_index(
        "ix_mcp_tenant_contact_pending",
        table_name="matter_contact_proposals",
    )
    op.drop_index(
        "ix_mcp_tenant_matter_pending",
        table_name="matter_contact_proposals",
    )
    op.drop_table("matter_contact_proposals")
