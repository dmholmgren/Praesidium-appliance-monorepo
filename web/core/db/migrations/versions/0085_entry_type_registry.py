"""0084: entry_types registry -- one classification layer for tasks / projects /
deadlines / calendar_events so each entity carries a *type* that drives consistent
scoping (filter a surface to one type) and reporting (group/count by type).

Resolution mirrors ui_nav_items / layout_tabs: platform defaults (tenant_id IS NULL)
+ tenant overrides, is_active filter, ordered by display_order.

v1 keying decision (minimal, non-destructive): entities keep their existing *_type
STRING column and its value IS the registry `code`. No data migration; existing rows
validate against the seeded codes. Only schema change to an entity: add
calendar_events.event_type (it had no type column).

Deferred hardening (tracked as debt, not done here): normalize entities to *_type_id
FKs into entry_types and retire the dangling deadlines.deadline_type_id.
"""
from alembic import op


revision = "0085_entry_type_registry"
down_revision = "0084_brief_sections"
branch_labels = None
depends_on = None


# (entity_kind, code, display_name, icon, color)  -- ordered; display_order assigned by position.
# Existing live values are kept as codes so current rows stay valid; additions marked (+).
_SEED = [
    # ---- task ----  (live: ai_suggested, drafting, filing, general, hearing, milestone, research, review)
    ("task", "general",        "General",          "\U0001F4CB", "#64748B"),
    ("task", "drafting",       "Drafting",         "✍️", "#2563EB"),
    ("task", "filing",         "Filing",           "\U0001F4C1", "#0891B2"),
    ("task", "review",         "Review",           "\U0001F50D", "#7C3AED"),
    ("task", "research",       "Research",         "\U0001F4DA", "#0369A1"),
    ("task", "hearing",        "Hearing",          "⚖️", "#B45309"),
    ("task", "milestone",      "Milestone",        "\U0001F3C1", "#16A34A"),
    ("task", "deposition_prep","Deposition Prep",  "\U0001F3A4", "#9333EA"),   # (+)
    ("task", "discovery",      "Discovery",        "\U0001F50E", "#0D9488"),   # (+)
    ("task", "ai_suggested",   "AI Suggested",     "✨",     "#6366F1"),   # live value; keep valid
    # ---- project ----  (live: case_plan, general)
    ("project", "general",          "General",            "\U0001F4C2", "#64748B"),
    ("project", "case_plan",        "Case Plan",          "\U0001F5C2️", "#2563EB"),
    ("project", "document_assembly","Document Assembly",  "\U0001F4C4", "#0891B2"),
    ("project", "litigation",       "Litigation",         "⚖️", "#B91C1C"),  # (+)
    ("project", "transaction",      "Transaction",        "\U0001F91D", "#15803D"),    # (+)
    ("project", "deposition",       "Deposition",         "\U0001F3A4", "#9333EA"),    # (+)
    ("project", "appeal",           "Appeal",             "\U0001F3DB️", "#7C3AED"),  # (+)
    # ---- deadline ----  (live: discovery, expert, mediation, motion, pleading, pretrial, trial)
    ("deadline", "pleading",  "Pleading",   "\U0001F4DD", "#2563EB"),
    ("deadline", "discovery", "Discovery",  "\U0001F50E", "#0D9488"),
    ("deadline", "motion",    "Motion",     "\U0001F4C4", "#0891B2"),
    ("deadline", "expert",    "Expert",     "\U0001F9EA", "#7C3AED"),
    ("deadline", "mediation", "Mediation",  "\U0001F54A️", "#15803D"),
    ("deadline", "pretrial",  "Pretrial",   "\U0001F5C3️", "#B45309"),
    ("deadline", "trial",     "Trial",      "⚖️", "#B91C1C"),
    ("deadline", "sol",       "Statute of Limitations", "⏳", "#DC2626"),  # (+)
    ("deadline", "appellate", "Appellate",  "\U0001F3DB️", "#7C3AED"),    # (+)
    # ---- calendar_event ---- (new column; from v14.9 meeting-binder types)
    ("calendar_event", "hearing",         "Hearing",         "⚖️", "#B45309"),
    ("calendar_event", "deposition",      "Deposition",      "\U0001F3A4", "#9333EA"),
    ("calendar_event", "client_meeting",  "Client Meeting",  "\U0001F465", "#2563EB"),
    ("calendar_event", "court_zoom",      "Court Zoom",      "\U0001F4F9", "#0891B2"),
    ("calendar_event", "mediation",       "Mediation",       "\U0001F54A️", "#15803D"),
    ("calendar_event", "internal",        "Internal",        "\U0001F3E2", "#64748B"),  # (+)
    ("calendar_event", "court_appearance","Court Appearance","\U0001F3DB️", "#B91C1C"),  # (+)
]


def _q(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS entry_types (
            id            bigserial PRIMARY KEY,
            tenant_id     char(36),                       -- NULL = platform default; TRIM in comparisons
            entity_kind   text NOT NULL,                  -- task | project | deadline | calendar_event
            code          text NOT NULL,                  -- slug; matches the entity's *_type string value
            display_name  text NOT NULL,
            display_order int NOT NULL DEFAULT 100,
            color         text,
            icon          text,
            parent_code   text,
            matter_types  text[],
            is_active     boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    # partial-unique on (entity_kind, code, tenant-or-platform) so a tenant can override a default.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_types_kind_code "
        "ON entry_types (entity_kind, code, COALESCE(tenant_id, '__platform__'))")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_types_lookup "
        "ON entry_types (entity_kind, is_active, display_order)")

    # calendar_events had no type column -- the only entity schema change in v1.
    op.execute("ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS event_type text")

    # Seed platform defaults (tenant_id IS NULL). display_order = position*10 within entity_kind.
    order = {}
    for (kind, code, name, icon, color) in _SEED:
        order[kind] = order.get(kind, 0) + 10
        op.execute(
            "INSERT INTO entry_types "
            "  (tenant_id, entity_kind, code, display_name, display_order, icon, color, is_active) "
            "VALUES (NULL, %s, %s, %s, %s, %s, %s, true) "
            "ON CONFLICT (entity_kind, code, COALESCE(tenant_id, '__platform__')) DO NOTHING" %
            (_q(kind), _q(code), _q(name), order[kind], _q(icon), _q(color)))


def downgrade():
    op.execute("DROP TABLE IF EXISTS entry_types")
    op.execute("ALTER TABLE calendar_events DROP COLUMN IF EXISTS event_type")
