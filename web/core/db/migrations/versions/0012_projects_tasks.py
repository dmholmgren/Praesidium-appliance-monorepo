"""Add projects table and enhance tasks for project-centric workflow.

Revision ID: 0012_projects_tasks
Revises: 0011_nav_rail
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_projects_tasks"
down_revision = "0011_nav_rail"
branch_labels = None
depends_on = None


def upgrade():
    # ── Projects table ──
    op.create_table(
        "projects",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("matter_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("template_type", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("priority", sa.String(32), server_default="medium", nullable=True),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("lead_attorney_id", sa.BigInteger(), nullable=True),
        sa.Column("members", JSONB, server_default="[]", nullable=True),
        sa.Column("config", JSONB, server_default="{}", nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_tenant_matter", "projects", ["tenant_id", "matter_id"])
    op.create_index("ix_projects_tenant_status", "projects", ["tenant_id", "status"])

    # ── Enhance tasks table ──
    op.add_column("tasks", sa.Column("project_id", sa.dialects.postgresql.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("estimated_minutes", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("actual_minutes", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("task_type", sa.String(64), server_default="general", nullable=True))
    op.add_column("tasks", sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False))
    op.add_column("tasks", sa.Column("parent_task_id", sa.BigInteger(), nullable=True))
    op.add_column("tasks", sa.Column("tags", JSONB, server_default="[]", nullable=True))
    op.add_column("tasks", sa.Column("started_at", sa.DateTime(), nullable=True))

    op.create_index("ix_tasks_project", "tasks", ["project_id"])
    op.create_index("ix_tasks_tenant_status", "tasks", ["tenant_id", "status"])
    op.create_index("ix_tasks_parent", "tasks", ["parent_task_id"])


def downgrade():
    op.drop_index("ix_tasks_parent")
    op.drop_index("ix_tasks_tenant_status")
    op.drop_index("ix_tasks_project")
    op.drop_column("tasks", "started_at")
    op.drop_column("tasks", "tags")
    op.drop_column("tasks", "parent_task_id")
    op.drop_column("tasks", "sort_order")
    op.drop_column("tasks", "task_type")
    op.drop_column("tasks", "actual_minutes")
    op.drop_column("tasks", "estimated_minutes")
    op.drop_column("tasks", "project_id")
    op.drop_index("ix_projects_tenant_status")
    op.drop_index("ix_projects_tenant_matter")
    op.drop_table("projects")
