"""exhibit_list document type + classification rule.

Adds 'exhibit_list' to the document taxonomy so the ingest classifier/router can
recognize an exhibit-list filing and dispatch it to the exhibit-list handler
(which resolves each listed Bates range to the produced document and creates the
Trial Center exhibits). A structural rule (regex over filename+content head)
gives the deterministic floor; the AI-on-residue path can also pick the code.

Idempotent.

Revision ID: 0123_exhibit_list_taxonomy
Revises: 0122_research_routes
"""
from alembic import op
import sqlalchemy as sa

revision = "0123_exhibit_list_taxonomy"
down_revision = "0122_research_routes"
branch_labels = None
depends_on = None

_CODE = "exhibit_list"
_RULE = "exhibit_list_title"
# matches "Exhibit List", "List of Exhibits", "Index of Exhibits", "Exhibit Index"
_RX = r"(?i)\b(exhibit\s+(list|index)|(list|index)\s+of\s+exhibits)\b"


def upgrade():
    conn = op.get_bind()
    tax_id = conn.execute(sa.text(
        "SELECT id::text FROM document_type_taxonomy WHERE code = :c"), {"c": _CODE}).scalar()
    if not tax_id:
        tax_id = conn.execute(sa.text(
            "INSERT INTO document_type_taxonomy (category, code, display_name, sort_order, is_active) "
            "VALUES ('other', :c, 'Exhibit List', 90, TRUE) RETURNING id::text"),
            {"c": _CODE}).scalar()
    exists = conn.execute(sa.text(
        "SELECT 1 FROM classification_rules WHERE rule_name = :r"), {"r": _RULE}).fetchone()
    if not exists:
        conn.execute(sa.text(
            "INSERT INTO classification_rules "
            "  (rule_name, priority, match_type, match_value, document_type_id, "
            "   confidence, is_active) "
            "VALUES (:r, 80, 'regex', :rx, CAST(:tid AS uuid), 0.9, TRUE)"),
            {"r": _RULE, "rx": _RX, "tid": tax_id})


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM classification_rules WHERE rule_name = :r"), {"r": _RULE})
    conn.execute(sa.text("DELETE FROM document_type_taxonomy WHERE code = :c"), {"c": _CODE})
