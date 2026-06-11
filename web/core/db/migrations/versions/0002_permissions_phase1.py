"""permissions phase 1 — seed permission_matrix and document role enum drift

Phase 1 of the permissions system.

WHAT THIS MIGRATION DOES:

1. Seeds permission_matrix rows for HJMM tenant covering the cases where
   default-allow-for-internal-roles is NOT what we want:

     - client.*               → DENY everything
     - deal_room_guest.*      → DENY everything
     - tenant_admin.*         → admin + partner only (deny attorney/paralegal/staff)
     - bill_run.finalize      → admin + partner only
     - bill_run.delete        → admin only
     - audit_log.view         → admin only
     - permission_matrix.edit → admin only
     - widgets.view           → ALLOW for all internal roles (explicit row so
                                admin UI shows the matrix entry)

   Everything else is left to default-allow / default-deny by the service.
   This means new modules added in the future "just work" for internal users
   without requiring matrix seeds — the firm admin can later add deny rows
   if they want to lock something down.

2. Does NOT touch the schema. permission_matrix, matter_access_grants,
   external_user_scopes, and matter_timekeepers all already exist.

3. Does NOT add the missing role enum values to the ORM. The DB enum already
   has all 9 values (super_admin, admin, attorney, paralegal, staff, read_only,
   partner, client, deal_room_guest). The Python ORM in core/models/user.py
   omits partner/client/deal_room_guest — that file needs an edit OUTSIDE
   this migration to add those three enum values to the SQLAlchemy Column.

Revision ID: 0002_permissions_phase1
Revises: 0001_initial_canonical
Create Date: 2026-04-30
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_permissions_phase1"
down_revision = "0001_initial_canonical"
branch_labels = None
depends_on = None


HJMM_TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


# Each tuple: (role, module, action, allowed).
# Only rows that DEVIATE from the service's default-allow-for-internal /
# default-deny-for-external behavior are seeded. Default-allow rows are
# included only where being explicit aids the admin UI (widgets.view).
SEEDS: list[tuple[str, str, str, bool]] = [
    # ── External roles: default-deny is the service default, but seed
    #    explicit rows for the modules they ARE allowed to touch. Phase 1
    #    seeds none — clients and deal-room-guests get nothing until the
    #    portal modules ship. Service's default-deny handles them safely.

    # ── Internal roles: deviations from default-allow ─────────────────────

    # tenant_admin module — admin/partner only
    ("attorney",  "tenant_admin", "view",   False),
    ("attorney",  "tenant_admin", "edit",   False),
    ("paralegal", "tenant_admin", "view",   False),
    ("paralegal", "tenant_admin", "edit",   False),
    ("staff",     "tenant_admin", "view",   False),
    ("staff",     "tenant_admin", "edit",   False),
    ("read_only", "tenant_admin", "view",   False),
    ("read_only", "tenant_admin", "edit",   False),

    # bill_run.finalize — partner+ only
    ("attorney",  "bill_run", "finalize", False),
    ("paralegal", "bill_run", "finalize", False),
    ("staff",     "bill_run", "finalize", False),
    ("read_only", "bill_run", "finalize", False),

    # bill_run.delete — admin only (partner can't delete a finalized bill)
    ("partner",   "bill_run", "delete", False),
    ("attorney",  "bill_run", "delete", False),
    ("paralegal", "bill_run", "delete", False),
    ("staff",     "bill_run", "delete", False),
    ("read_only", "bill_run", "delete", False),

    # audit_log.view — admin only
    ("partner",   "audit_log", "view", False),
    ("attorney",  "audit_log", "view", False),
    ("paralegal", "audit_log", "view", False),
    ("staff",     "audit_log", "view", False),
    ("read_only", "audit_log", "view", False),

    # permission_matrix.edit — admin only (the firm admin is the only one
    # who can rewrite the permission rules for their tenant).
    ("partner",   "permission_matrix", "edit", False),
    ("attorney",  "permission_matrix", "edit", False),
    ("paralegal", "permission_matrix", "edit", False),
    ("staff",     "permission_matrix", "edit", False),
    ("read_only", "permission_matrix", "edit", False),

    # ── Explicit-allow rows for visibility in admin UI ─────────────────────
    # widgets.view — internal roles all see widgets by default; the admin UI
    # benefits from showing the explicit allow rows rather than relying on
    # invisible default-allow behavior.
    ("admin",     "widgets", "view", True),
    ("partner",   "widgets", "view", True),
    ("attorney",  "widgets", "view", True),
    ("paralegal", "widgets", "view", True),
    ("staff",     "widgets", "view", True),
    ("read_only", "widgets", "view", True),

    # External roles: explicit deny rows so the admin UI can see and toggle.
    ("client",          "widgets", "view", False),
    ("deal_room_guest", "widgets", "view", False),
]


def upgrade() -> None:
    """Insert seed rows for HJMM tenant. Idempotent via ON CONFLICT."""
    for role, module, action, allowed in SEEDS:
        op.execute(
            sa.text("""
                INSERT INTO permission_matrix
                    (tenant_id, role, module, action, allowed,
                     locked_by_admin, created_at, updated_at)
                VALUES
                    (:tid, :role, :module, :action, :allowed,
                     FALSE, NOW(), NOW())
                ON CONFLICT (tenant_id, role, module, action) DO NOTHING
            """).bindparams(
                tid=HJMM_TENANT_ID,
                role=role,
                module=module,
                action=action,
                allowed=allowed,
            )
        )


def downgrade() -> None:
    """Remove only the seed rows this migration created."""
    for role, module, action, _allowed in SEEDS:
        op.execute(
            sa.text("""
                DELETE FROM permission_matrix
                 WHERE TRIM(tenant_id) = :tid
                   AND role = :role
                   AND module = :module
                   AND action = :action
            """).bindparams(
                tid=HJMM_TENANT_ID,
                role=role,
                module=module,
                action=action,
            )
        )
