"""AI Layer Foundation — model routing, cost exceptions, prompt templates

Revision ID: 0044_ai_layer_foundation
Revises: 0043_ediscovery_text_path
Create Date: 2026-04-18

Creates the four tables that make the AI layer data-driven per Architectural
Constraints #2 (state lives in the database) and #11 (AI operations cost-capped
and attributed):

    ai_model_routing           — (module, purpose) -> model, caps, fallback,
                                 warning thresholds, override policy
    ai_cost_exceptions         — overage events with disposition lifecycle
    prompt_templates           — latest-pointer for each named prompt
    prompt_template_versions   — immutable version history per template

Note on architectural approach:
    ai_model_routing is keyed by (tenant_id, module, purpose) with NULL tenant_id
    rows acting as platform defaults. A tenant override wins over a NULL default.
    This matches the widget_registry precedent (tenant_id nullable for
    platform-standard rows).

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision = "0044_ai_layer_foundation"
down_revision = "0043_ediscovery_text_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # ai_model_routing                                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "ai_model_routing",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        # NULL tenant_id = platform default row
        sa.Column("tenant_id", sa.CHAR(36), nullable=True),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(128), nullable=False),
        # Primary + fallback model selection
        sa.Column("primary_model", sa.String(100), nullable=False),
        sa.Column("fallback_model", sa.String(100), nullable=True),
        # Token and cost caps
        sa.Column("max_tokens", sa.Integer, nullable=False,
                  server_default=sa.text("4096")),
        sa.Column("per_call_token_cap", sa.Integer, nullable=True),
        sa.Column("matter_daily_cost_cap_usd", sa.Numeric(10, 4),
                  nullable=True),
        sa.Column("tenant_daily_cost_cap_usd", sa.Numeric(12, 4),
                  nullable=True),
        # Warning thresholds — default 0.75 / 0.90 / 1.00
        # Stored as jsonb array so they're per-row overridable
        sa.Column("warning_thresholds", postgresql.JSONB,
                  nullable=False,
                  server_default=sa.text("""'[0.75, 0.90]'::jsonb""")),
        # Override policy — who can override a cap breach and how
        # Shape: {
        #   "allow_override": true,
        #   "roles": ["admin", "super_admin"],
        #   "require_reason": true,
        #   "max_override_multiplier": 3.0,
        #   "bill_overage_to_matter": true
        # }
        sa.Column("override_policy", postgresql.JSONB,
                  nullable=False,
                  server_default=sa.text("""'{"allow_override": false}'::jsonb""")),
        # Lifecycle
        sa.Column("version", sa.Integer, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'published'")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft','published','deprecated')",
            name="ck_ai_model_routing_status"
        ),
    )
    # (tenant_id, module, purpose) must be unique per published row.
    # Partial unique index allows draft/deprecated rows to coexist with published.
    op.create_index(
        "uq_ai_model_routing_tenant_module_purpose_published",
        "ai_model_routing",
        ["tenant_id", "module", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "ix_ai_model_routing_lookup",
        "ai_model_routing",
        ["module", "purpose", "status"],
    )

    # ------------------------------------------------------------------ #
    # ai_cost_exceptions                                                  #
    # ------------------------------------------------------------------ #
    op.create_table(
        "ai_cost_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("matter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.BigInteger, nullable=True),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(128), nullable=False),
        # What was attempted and what happened
        sa.Column("routing_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_api_call_id", sa.BigInteger, nullable=True),
        sa.Column("breach_type", sa.String(32), nullable=False),
        sa.Column("breach_reason", sa.Text, nullable=True),
        sa.Column("attempted_model", sa.String(100), nullable=True),
        sa.Column("actual_model", sa.String(100), nullable=True),
        sa.Column("projected_cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("matter_spend_today_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("matter_cap_usd", sa.Numeric(10, 4), nullable=True),
        # Override chain
        sa.Column("override_used", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("override_reason", sa.Text, nullable=True),
        sa.Column("override_approved_by", sa.BigInteger, nullable=True),
        # Disposition lifecycle: pending -> approved|written_off|billed
        sa.Column("disposition", sa.String(20), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("disposition_set_by", sa.BigInteger, nullable=True),
        sa.Column("disposition_set_at", postgresql.TIMESTAMP(timezone=True),
                  nullable=True),
        sa.Column("disposition_notes", sa.Text, nullable=True),
        # If billed, link to the invoice line
        sa.Column("billed_invoice_line_id",
                  postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "breach_type IN ('cap_warning','soft_cap','hard_cap','fallback_used',"
            "'fallback_also_breached','override_used')",
            name="ck_ai_cost_exceptions_breach_type"
        ),
        sa.CheckConstraint(
            "disposition IN ('pending','approved','written_off','billed')",
            name="ck_ai_cost_exceptions_disposition"
        ),
    )
    op.create_index(
        "ix_ai_cost_exceptions_tenant_matter_created",
        "ai_cost_exceptions",
        ["tenant_id", "matter_id", "created_at"],
    )
    op.create_index(
        "ix_ai_cost_exceptions_disposition_pending",
        "ai_cost_exceptions",
        ["tenant_id", "disposition"],
        postgresql_where=sa.text("disposition = 'pending'"),
    )

    # ------------------------------------------------------------------ #
    # prompt_templates (latest-pointer) + prompt_template_versions (immutable)
    # ------------------------------------------------------------------ #
    op.create_table(
        "prompt_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("tenant_id", sa.CHAR(36), nullable=True),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(128), nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'published'")),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft','published','deprecated')",
            name="ck_prompt_templates_status"
        ),
    )
    op.create_index(
        "uq_prompt_templates_tenant_slug",
        "prompt_templates",
        ["tenant_id", "slug"],
        unique=True,
    )

    op.create_table(
        "prompt_template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True),
                  nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("system_prompt", sa.Text, nullable=True),
        sa.Column("user_prompt", sa.Text, nullable=False),
        # Variables declared by the template — validation at render time
        sa.Column("variables", postgresql.JSONB,
                  nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("response_format", sa.String(32), nullable=False,
                  server_default=sa.text("'text'")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["prompt_templates.id"],
            name="fk_prompt_template_versions_template",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "response_format IN ('text','json','json_array')",
            name="ck_prompt_template_versions_response_format"
        ),
    )
    op.create_index(
        "uq_prompt_template_versions_template_version",
        "prompt_template_versions",
        ["template_id", "version"],
        unique=True,
    )

    # ------------------------------------------------------------------ #
    # ai_api_calls — promote matter_id from JSONB to a column,
    # add allocation lifecycle columns.
    # ------------------------------------------------------------------ #
    # matter_id as a proper column makes partner reports fast and the
    # allocation drill-down joinable. Existing rows (if any) will have
    # NULL; the adapter backfills request_metadata on write so the JSONB
    # copy remains available for provenance.
    op.add_column(
        "ai_api_calls",
        sa.Column("matter_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # allocation_status:
    #   allocated       — properly tied to a matter, counts against cap
    #   unallocated     — no matter / needs partner review
    #   firm_overhead   — policy-driven, not billable to any matter
    #   reallocated     — moved to a matter retroactively (bookkeeping only,
    #                     does NOT re-trigger cap checks per Apr 18 spec)
    op.add_column(
        "ai_api_calls",
        sa.Column(
            "allocation_status", sa.String(20),
            nullable=False,
            server_default=sa.text("'allocated'"),
        ),
    )
    # unallocated_reason — three enum values, distinct review flows
    #   no_matter_context    — call made without matter_id (cross-matter by design)
    #   policy_firm_overhead — routing override_policy.bill_overage_to_matter = false
    #   matter_unavailable   — matter deleted/archived before billing could attach
    op.add_column(
        "ai_api_calls",
        sa.Column("unallocated_reason", sa.String(32), nullable=True),
    )
    # If a partner retroactively allocates, where it went + when + who
    op.add_column(
        "ai_api_calls",
        sa.Column(
            "allocated_to_matter_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "ai_api_calls",
        sa.Column("allocated_by", sa.BigInteger, nullable=True),
    )
    op.add_column(
        "ai_api_calls",
        sa.Column(
            "allocated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "ai_api_calls",
        sa.Column("allocation_notes", sa.Text, nullable=True),
    )
    # Flag surfaced in the unallocated report: true when this call,
    # at its time of execution, would have breached its matter cap
    # had it been attributed to the target matter. Informational only —
    # retroactive allocation does not re-enforce caps.
    op.add_column(
        "ai_api_calls",
        sa.Column(
            "would_have_breached_cap",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_check_constraint(
        "ck_ai_api_calls_allocation_status",
        "ai_api_calls",
        "allocation_status IN ('allocated','unallocated','firm_overhead','reallocated')",
    )
    op.create_check_constraint(
        "ck_ai_api_calls_unallocated_reason",
        "ai_api_calls",
        "unallocated_reason IS NULL OR unallocated_reason IN "
        "('no_matter_context','policy_firm_overhead','matter_unavailable')",
    )

    # Indexes for the drill-down queries
    op.create_index(
        "ix_ai_api_calls_tenant_matter_date",
        "ai_api_calls",
        ["tenant_id", "matter_id", "created_at"],
    )
    # Partial index: fast scan for the unallocated report
    op.create_index(
        "ix_ai_api_calls_unallocated_pending",
        "ai_api_calls",
        ["tenant_id", "created_at"],
        postgresql_where=sa.text(
            "allocation_status IN ('unallocated','firm_overhead')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_ai_api_calls_unallocated_pending",
                  table_name="ai_api_calls")
    op.drop_index("ix_ai_api_calls_tenant_matter_date",
                  table_name="ai_api_calls")
    op.drop_constraint("ck_ai_api_calls_unallocated_reason",
                       "ai_api_calls", type_="check")
    op.drop_constraint("ck_ai_api_calls_allocation_status",
                       "ai_api_calls", type_="check")
    op.drop_column("ai_api_calls", "would_have_breached_cap")
    op.drop_column("ai_api_calls", "allocation_notes")
    op.drop_column("ai_api_calls", "allocated_at")
    op.drop_column("ai_api_calls", "allocated_by")
    op.drop_column("ai_api_calls", "allocated_to_matter_id")
    op.drop_column("ai_api_calls", "unallocated_reason")
    op.drop_column("ai_api_calls", "allocation_status")
    op.drop_column("ai_api_calls", "matter_id")

    op.drop_index("uq_prompt_template_versions_template_version",
                  table_name="prompt_template_versions")
    op.drop_table("prompt_template_versions")

    op.drop_index("uq_prompt_templates_tenant_slug",
                  table_name="prompt_templates")
    op.drop_table("prompt_templates")

    op.drop_index("ix_ai_cost_exceptions_disposition_pending",
                  table_name="ai_cost_exceptions")
    op.drop_index("ix_ai_cost_exceptions_tenant_matter_created",
                  table_name="ai_cost_exceptions")
    op.drop_table("ai_cost_exceptions")

    op.drop_index("ix_ai_model_routing_lookup",
                  table_name="ai_model_routing")
    op.drop_index("uq_ai_model_routing_tenant_module_purpose_published",
                  table_name="ai_model_routing")
    op.drop_table("ai_model_routing")
