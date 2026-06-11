"""0040 canonical spine + standing enrichment.

ADDITIVE ONLY -- safe on the live appliance, touches no existing data:
  1. Canonical-entity spine (firm-wide, tenant-scoped, uuid):
       canonical_entities, entity_identifiers, entity_relationships,
       entity_contact_channels
  2. canonical_entity_id crosswalk columns on identity-bearing tables
  3. SALI/FOLIO IRIs + definition/exemplar anchors on the standing libraries
  4. chunk -> primitive linkage columns
  5. document_sections: embedding + open enrichment jsonb

DEFERRED to 0041 (coupled with the reparse, next session):
  - causes_of_action / coa_elements bigint -> uuid recreate
  - case_topics / allegations / document_topic_segments (FK the recreated primitives)
  - contacts -> spine data seed + dedup hygiene run

IMPORTANT: statements are executed individually. The asyncpg driver cannot run
multiple commands in a single prepared statement, so a multi-statement string
passed to op.execute() raises 'cannot insert multiple commands into a prepared
statement'. Each DDL statement (incl. each DO block) is therefore its own
op.execute(). All statements are guarded (IF [NOT] EXISTS / DO-block loops) so
re-running is a no-op.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0040_canonical_spine"
down_revision = "0039_reconcile"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    # ---- 1. canonical entity spine -------------------------------------
    """
    CREATE TABLE IF NOT EXISTS canonical_entities (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id       text NOT NULL,
        entity_type     varchar(32) NOT NULL,
        canonical_name  text NOT NULL,
        name_normalized text,
        attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
        -- embedding vector(1024) deferred to 0041 (pgvector .so missing post-rebuild)
        status          varchar(16) NOT NULL DEFAULT 'active',
        merged_into_id  uuid REFERENCES canonical_entities(id),
        confidence      double precision,
        attribution     varchar(32),
        first_seen_at   timestamptz,
        last_seen_at    timestamptz,
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_canonical_entities_tenant ON canonical_entities (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_canonical_entities_type   ON canonical_entities (tenant_id, entity_type)",
    "CREATE INDEX IF NOT EXISTS ix_canonical_entities_norm   ON canonical_entities (tenant_id, name_normalized)",
    """
    CREATE TABLE IF NOT EXISTS entity_identifiers (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id           text NOT NULL,
        canonical_entity_id uuid NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
        identifier_type     varchar(32) NOT NULL,
        identifier_value    text NOT NULL,
        is_primary          boolean NOT NULL DEFAULT false,
        source              varchar(32),
        confidence          double precision,
        created_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_entity_identifier UNIQUE (tenant_id, identifier_type, identifier_value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_entity_identifiers_entity ON entity_identifiers (canonical_entity_id)",
    """
    CREATE TABLE IF NOT EXISTS entity_relationships (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         text NOT NULL,
        entity_a_id       uuid NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
        entity_b_id       uuid NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
        relationship_type varchar(48) NOT NULL,
        matter_id         uuid,
        confidence        double precision,
        attribution       varchar(32),
        source_doc_id     uuid,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_entity_rel_a ON entity_relationships (entity_a_id)",
    "CREATE INDEX IF NOT EXISTS ix_entity_rel_b ON entity_relationships (entity_b_id)",
    """
    CREATE TABLE IF NOT EXISTS entity_contact_channels (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id           text NOT NULL,
        canonical_entity_id uuid NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
        channel_type        varchar(16) NOT NULL,
        label               varchar(32),
        value               text,
        addr_line1          text,
        addr_line2          text,
        city                text,
        state               text,
        zip                 text,
        country             text,
        is_primary          boolean NOT NULL DEFAULT false,
        verified            boolean NOT NULL DEFAULT false,
        source              varchar(32),
        valid_from          date,
        valid_to            date,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_contact_channels_entity ON entity_contact_channels (canonical_entity_id)",

    # ---- 2. canonical_entity_id crosswalk ------------------------------
    "ALTER TABLE IF EXISTS kg_entities              ADD COLUMN IF NOT EXISTS canonical_entity_id uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS contacts                 ADD COLUMN IF NOT EXISTS canonical_entity_id uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS contact_dedup_candidates ADD COLUMN IF NOT EXISTS canonical_entity_id uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS judges                   ADD COLUMN IF NOT EXISTS canonical_entity_id uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS expert_witnesses         ADD COLUMN IF NOT EXISTS canonical_entity_id uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS matter_properties        ADD COLUMN IF NOT EXISTS owner_entity_id         uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS matter_properties        ADD COLUMN IF NOT EXISTS broker_entity_id        uuid REFERENCES canonical_entities(id)",
    "ALTER TABLE IF EXISTS matter_properties        ADD COLUMN IF NOT EXISTS title_company_entity_id uuid REFERENCES canonical_entities(id)",
    "CREATE INDEX IF NOT EXISTS ix_kg_entities_canonical ON kg_entities (canonical_entity_id)",
    "CREATE INDEX IF NOT EXISTS ix_contacts_canonical    ON contacts (canonical_entity_id)",

    # ---- 3. SALI/FOLIO + extraction anchors on libraries ---------------
    """
    DO $$
    DECLARE t text;
    BEGIN
      FOREACH t IN ARRAY ARRAY[
        'cause_of_action_library','clause_type_library','document_type_taxonomy',
        'matter_type_library','party_role_library','contact_role_library','deadline_type_library'
      ] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=t) THEN
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS sali_iri  text',  t);
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS folio_iri text',  t);
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS definition text', t);
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS exemplars jsonb', t);
        END IF;
      END LOOP;
    END $$
    """,

    # ---- 4. chunk -> primitive linkage ---------------------------------
    """
    DO $$
    DECLARE t text;
    BEGIN
      FOREACH t IN ARRAY ARRAY[
        'dms_chunks','document_chunks','email_chunks','ediscovery_chunks','drafting_chunks','billing_chunks'
      ] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=t) THEN
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS section_id     uuid',        t);
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS primitive_type varchar(32)', t);
          EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS primitive_id   uuid',        t);
          EXECUTE format('CREATE INDEX IF NOT EXISTS ix_%s_section ON %I (section_id)', t, t);
        END IF;
      END LOOP;
    END $$
    """,

    # ---- 5. document_sections enrichment (embedding deferred to 0041 -- pgvector .so missing) ----
    "ALTER TABLE IF EXISTS document_sections ADD COLUMN IF NOT EXISTS attributes  jsonb NOT NULL DEFAULT '{}'::jsonb",

    # ---- 6. grants on new tables ---------------------------------------
    "GRANT ALL ON canonical_entities      TO praesidium",
    "GRANT ALL ON entity_identifiers      TO praesidium",
    "GRANT ALL ON entity_relationships    TO praesidium",
    "GRANT ALL ON entity_contact_channels TO praesidium",
]


DOWNGRADE_STATEMENTS = [
    "ALTER TABLE IF EXISTS matter_properties        DROP COLUMN IF EXISTS title_company_entity_id",
    "ALTER TABLE IF EXISTS matter_properties        DROP COLUMN IF EXISTS broker_entity_id",
    "ALTER TABLE IF EXISTS matter_properties        DROP COLUMN IF EXISTS owner_entity_id",
    "ALTER TABLE IF EXISTS expert_witnesses         DROP COLUMN IF EXISTS canonical_entity_id",
    "ALTER TABLE IF EXISTS judges                   DROP COLUMN IF EXISTS canonical_entity_id",
    "ALTER TABLE IF EXISTS contact_dedup_candidates DROP COLUMN IF EXISTS canonical_entity_id",
    "ALTER TABLE IF EXISTS contacts                 DROP COLUMN IF EXISTS canonical_entity_id",
    "ALTER TABLE IF EXISTS kg_entities              DROP COLUMN IF EXISTS canonical_entity_id",
    "DROP TABLE IF EXISTS entity_contact_channels",
    "DROP TABLE IF EXISTS entity_relationships",
    "DROP TABLE IF EXISTS entity_identifiers",
    "DROP TABLE IF EXISTS canonical_entities",
    # library / chunk / section additive columns intentionally left in place
]


def upgrade():
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade():
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
