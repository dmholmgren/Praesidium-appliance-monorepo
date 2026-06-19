"""0113: seed the persisted document classifier (Court/Hearing build, Step 2 Gap #1).

classification_results existed but was never populated; classification_rules was
empty and ai_model_routing had no (classification, document_type) route. The
shared-core principle is structural-first / model-on-residue, so this migration
lays the deterministic floor (high-precision regex rules → document_type_taxonomy)
and registers the AI residue route used by modules.intelligence.document_classifier.

Rules are evaluated low-priority-number first; the FIRST hit wins, so more
specific rules (motion_to_dismiss, scheduling_order, amended_petition) carry
lower priority numbers than their generic parents (motion, order, petition).
match_value is a Python regex applied case-insensitively to filename + the head
of content_text. Everything seeded here is auto_generated=false, is_active=true.
"""
from alembic import op


revision = "0113_classifier_seed"
down_revision = "0112_hearing_primitives"
branch_labels = None
depends_on = None


# (rule_name, priority, taxonomy_code, regex, confidence)
# priority asc = checked first; specific before generic.
_RULES = [
    # ── court settings / orders / notices (hearing-critical) ──────────────
    ("scheduling_order_phrase", 10, "scheduling_order",
     r"docket control order|scheduling order|agreed scheduling order", 0.95),
    ("notice_of_setting",       12, "notice",
     r"notice of (hearing|setting|submission|trial setting|oral argument)", 0.95),
    ("notice_of_x",             14, "notice",
     r"notice of (filing|nonsuit|appeal|deposition|intent|hearing|"
     r"appearance|change of)", 0.90),
    ("subpoena_phrase",         16, "subpoena",
     r"\bsubpoena\b", 0.93),
    ("order_granting_denying",  20, "order",
     r"^\s*order\b|order (granting|denying|on|of|setting|of dismissal|"
     r"of nonsuit|appointing)", 0.88),
    # ── motion practice (specific before generic motion) ──────────────────
    ("motion_to_dismiss",       30, "motion_to_dismiss",
     r"motion to dismiss", 0.93),
    ("plea_to_jurisdiction",    31, "plea_to_jurisdiction",
     r"plea to the jurisdiction", 0.93),
    ("special_appearance",      32, "special_appearance",
     r"special appearance", 0.92),
    ("response_to_motion",      34, "response",
     r"response (to|in opposition)|response and objection", 0.88),
    ("reply_in_support",        35, "reply",
     r"reply (in support|brief in support)|reply to .*response", 0.88),
    ("brief_phrase",            36, "brief",
     r"trial brief|brief in support|memorandum (of law|in support)", 0.85),
    ("motion_generic",          40, "motion",
     r"\bmotion (to|for|in limine)\b", 0.85),
    # ── pleadings (specific petitions before generic) ─────────────────────
    ("amended_petition",        50, "amended_petition",
     r"(first|second|third|fourth|amended|supplemental)\s+amended\s+petition|"
     r"(first|second|third|fourth)\s+amended\s+(original\s+)?petition", 0.90),
    ("original_petition",       52, "original_petition",
     r"plaintiff'?s?\s+original\s+petition|original\s+petition", 0.90),
    ("complaint",               54, "complaint",
     r"\bcomplaint\b", 0.85),
    ("counterclaim",            56, "counterclaim",
     r"\bcounterclaim\b|counter-claim", 0.88),
    ("crossclaim",              57, "crossclaim",
     r"cross-?claim", 0.88),
    ("third_party_petition",    58, "third_party_petition",
     r"third-?party (petition|claim|complaint)", 0.88),
    ("original_answer",         60, "answer",
     r"original answer|defendant'?s?\s+answer|\banswer (to|and)\b", 0.85),
    # ── discovery responses ───────────────────────────────────────────────
    ("expert_disclosures",      70, "expert_disclosures",
     r"expert (designation|disclosure)|designation of (testifying )?experts", 0.90),
    ("initial_disclosures",     72, "initial_disclosures",
     r"\binitial disclosures\b|rule 194", 0.88),
    ("supplemental_disclosures",73, "supplemental_disclosures",
     r"supplemental disclosures", 0.88),
    ("interrogatory_responses", 74, "interrogatory_responses",
     r"responses? to .*interrogator|answers? to .*interrogator", 0.88),
    ("rfp_responses",           75, "rfp_responses",
     r"responses? to .*requests? for production", 0.88),
    ("rfa_responses",           76, "rfa_responses",
     r"responses? to .*requests? for admission", 0.88),
    # ── transcripts / correspondence ──────────────────────────────────────
    ("deposition_phrase",       80, "deposition",
     r"(oral|videotaped) deposition of|deposition of [A-Z]", 0.85),
    ("correspondence_phrase",   90, "correspondence",
     r"\b(dear|via email|via facsimile|re:)\b", 0.70),
]


def upgrade():
    # Deterministic rule floor. document_type_id resolved by taxonomy code so
    # the seed is independent of taxonomy UUIDs.
    for rule_name, priority, code, regex, conf in _RULES:
        op.execute(f"""
            INSERT INTO classification_rules
                (rule_name, priority, match_type, match_value,
                 document_type_id, confidence, is_active, auto_generated)
            SELECT :rn, :pr, 'regex', :mv, t.id, :cf, true, false
            FROM document_type_taxonomy t
            WHERE t.code = :code AND t.is_active
            ON CONFLICT DO NOTHING
        """.replace(":rn", _q(rule_name)).replace(":pr", str(priority))
           .replace(":mv", _q(regex)).replace(":cf", str(conf))
           .replace(":code", _q(code)))

    # AI residue route. tenant_id NULL = platform default; the adapter falls
    # back to this when no tenant-scoped row exists for (classification,
    # document_type). Sonnet primary, Haiku fallback (cheap, high-volume lane).
    op.execute("""
        INSERT INTO ai_model_routing
            (tenant_id, module, purpose, primary_model, fallback_model,
             max_tokens, status)
        SELECT NULL, 'classification', 'document_type',
               'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 400, 'published'
        WHERE NOT EXISTS (
            SELECT 1 FROM ai_model_routing
            WHERE module = 'classification' AND purpose = 'document_type'
              AND tenant_id IS NULL)
    """)


def downgrade():
    op.execute("DELETE FROM ai_model_routing "
               "WHERE module = 'classification' AND purpose = 'document_type'")
    op.execute("DELETE FROM classification_rules "
               "WHERE auto_generated = false AND learned_from IS NULL "
               "AND rule_name IN (" +
               ",".join(_q(r[0]) for r in _RULES) + ")")


def _q(s: str) -> str:
    """Single-quote + escape a literal for inline SQL."""
    return "'" + str(s).replace("'", "''") + "'"
