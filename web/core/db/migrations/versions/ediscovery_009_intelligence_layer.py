"""ediscovery_009_intelligence_layer

Revision ID: ediscovery_009
Revises: ediscovery_008
Create Date: 2026-03-31

Creates all M5i intelligence layer tables:
  - issue_map_versions
  - drift_events
  - kg_entities
  - kg_relationships
  - wiam_sessions
  - wiam_findings
"""

from alembic import op
import sqlalchemy as sa

revision = 'ediscovery_009'
down_revision = 'ediscovery_008'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ------------------------------------------------------------------ #
    # issue_map_versions                                                   #
    # Immutable — never overwrite or delete a version record              #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS issue_map_versions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id       UUID NOT NULL,
            tenant_id       CHAR(36) NOT NULL,
            version_num     INTEGER NOT NULL,
            trigger_type    VARCHAR(32) NOT NULL
                            CHECK (trigger_type IN (
                                'manual','document_added',
                                'document_updated','scheduled'
                            )),
            source_doc_ids  UUID[],
            issue_map       JSONB NOT NULL DEFAULT '{}',
            differential    JSONB NOT NULL DEFAULT '{}',
            magnitude       FLOAT NOT NULL DEFAULT 0.0
                            CHECK (magnitude >= 0.0 AND magnitude <= 1.0),
            created_by      BIGINT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (matter_id, version_num)
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_issue_map_versions_matter
            ON issue_map_versions (matter_id, version_num DESC)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_issue_map_versions_tenant
            ON issue_map_versions (tenant_id)
    """))

    # ------------------------------------------------------------------ #
    # drift_events                                                         #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS drift_events (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id       UUID NOT NULL,
            tenant_id       CHAR(36) NOT NULL,
            version_before  INTEGER,
            version_after   INTEGER NOT NULL,
            dimension       VARCHAR(32) NOT NULL
                            CHECK (dimension IN (
                                'theory_drift','factual_drift',
                                'custodian_drift','damages_drift'
                            )),
            drift_type      VARCHAR(32) NOT NULL
                            CHECK (drift_type IN (
                                'addition','removal','modification',
                                'escalation','de-escalation','reversal'
                            )),
            description     TEXT NOT NULL,
            source_doc_id   UUID,
            magnitude       FLOAT NOT NULL DEFAULT 0.0
                            CHECK (magnitude >= 0.0 AND magnitude <= 1.0),
            detection_source VARCHAR(64) NOT NULL DEFAULT 'ai',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_drift_events_matter
            ON drift_events (matter_id, created_at DESC)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_drift_events_tenant
            ON drift_events (tenant_id)
    """))

    # ------------------------------------------------------------------ #
    # kg_entities                                                          #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS kg_entities (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id       UUID NOT NULL,
            tenant_id       CHAR(36) NOT NULL,
            entity_type     VARCHAR(32) NOT NULL
                            CHECK (entity_type IN (
                                'person','organization','contract',
                                'event','fact','admission','contradiction'
                            )),
            canonical_name  VARCHAR(512) NOT NULL,
            properties      JSONB NOT NULL DEFAULT '{}',
            source_doc_id   UUID,
            confidence      FLOAT NOT NULL DEFAULT 1.0
                            CHECK (confidence >= 0.0 AND confidence <= 1.0),
            attribution     VARCHAR(16) NOT NULL DEFAULT 'ai'
                            CHECK (attribution IN ('ai','human')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_entities_matter
            ON kg_entities (matter_id, entity_type)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_entities_tenant
            ON kg_entities (tenant_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_entities_name
            ON kg_entities (matter_id, canonical_name)
    """))

    # ------------------------------------------------------------------ #
    # kg_relationships                                                     #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS kg_relationships (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id           UUID NOT NULL,
            tenant_id           CHAR(36) NOT NULL,
            entity_a_id         UUID NOT NULL,
            entity_b_id         UUID NOT NULL,
            relationship_type   VARCHAR(64) NOT NULL,
            source_doc_id       UUID,
            confidence          FLOAT NOT NULL DEFAULT 1.0
                                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_relationships_matter
            ON kg_relationships (matter_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_relationships_entity_a
            ON kg_relationships (entity_a_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_kg_relationships_entity_b
            ON kg_relationships (entity_b_id)
    """))

    # ------------------------------------------------------------------ #
    # wiam_sessions                                                        #
    # Immutable case record — never delete                                 #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wiam_sessions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id       UUID NOT NULL,
            tenant_id       CHAR(36) NOT NULL,
            triggered_by    VARCHAR(32) NOT NULL DEFAULT 'manual'
                            CHECK (triggered_by IN (
                                'manual','pre_filing',
                                'post_depo','scheduled'
                            )),
            status          VARCHAR(16) NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','complete','failed')),
            created_by      BIGINT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_wiam_sessions_matter
            ON wiam_sessions (matter_id, created_at DESC)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_wiam_sessions_tenant
            ON wiam_sessions (tenant_id)
    """))

    # ------------------------------------------------------------------ #
    # wiam_findings                                                        #
    # citations JSONB NOT NULL — enforced at app layer too                #
    # ------------------------------------------------------------------ #
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wiam_findings (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      UUID NOT NULL,
            matter_id       UUID NOT NULL,
            tenant_id       CHAR(36) NOT NULL,
            finding_type    VARCHAR(32) NOT NULL
                            CHECK (finding_type IN (
                                'opp_gap','own_gap',
                                'external_gap','drift_gap'
                            )),
            claim_element   VARCHAR(256),
            description     TEXT NOT NULL,
            citations       JSONB NOT NULL DEFAULT '[]',
            confidence      FLOAT NOT NULL DEFAULT 1.0
                            CHECK (confidence >= 0.0 AND confidence <= 1.0),
            priority        VARCHAR(16) NOT NULL DEFAULT 'medium'
                            CHECK (priority IN ('high','medium','low')),
            suggested_action TEXT,
            disposition     VARCHAR(16)
                            CHECK (disposition IN ('accepted','dismissed')),
            disposed_by     BIGINT,
            disposed_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_wiam_findings_session
            ON wiam_findings (session_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_wiam_findings_matter
            ON wiam_findings (matter_id, priority, created_at DESC)
    """))

    # ------------------------------------------------------------------ #
    # GRANT permissions to app user                                        #
    # ------------------------------------------------------------------ #
    for table in [
        'issue_map_versions', 'drift_events',
        'kg_entities', 'kg_relationships',
        'wiam_sessions', 'wiam_findings',
    ]:
        conn.execute(sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO praesidium_db"
        ))


def downgrade():
    conn = op.get_bind()
    for table in [
        'wiam_findings', 'wiam_sessions',
        'kg_relationships', 'kg_entities',
        'drift_events', 'issue_map_versions',
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))
