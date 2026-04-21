"""Module 8 — Platform Control, Infrastructure & Admin Console schema

Creates all new tables required by Module 8 before any other Module 8
components are built. Also seeds HJMM LDAP stubs in tenant_auth_config
and credentials_vault (inactive — .env still authoritative until cutover).

Revision ID: 0010_m8_platform_control
Revises: 0001_s20_initial
Create Date: 2026-03-29

HJMM LDAP NOTE:
  The tenant_auth_config and credentials_vault rows seeded here are
  INACTIVE (is_active=False, credential_ref=NULL on vault row).
  The auth loader checks tenant_auth_config first; if is_active=False
  or no matching row, it falls back to .env LDAP vars.
  HJMM production login is NOT affected.

  When ready to cut over:
    1. Run hjmm_ldap_cutover.sql to populate the vault secret and flip
       is_active=True on tenant_auth_config.
    2. Verify logins succeed against the DB row.
    3. Zero out LDAP vars in /etc/praesidium/.env.bootstrap.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, INET
import uuid
from datetime import datetime, timezone

# revision identifiers
revision = "0010_m8_platform_control"
down_revision = "0001_s20_initial"
branch_labels = None
depends_on = None

# ── Fixed UUIDs for seed rows (deterministic, safe to reference later) ────────
HJMM_TENANT_ID_PLACEHOLDER = "hjmm-prod"
HJMM_VAULT_STUB_ID = str(uuid.UUID("00000000-0001-0001-0001-000000000001"))
HJMM_AUTH_CONFIG_STUB_ID = str(uuid.UUID("00000000-0001-0001-0002-000000000001"))

# HJMM initial middleware server seed
HJMM_WEB01_ID = str(uuid.UUID("00000000-0008-0001-0001-000000000001"))
HJMM_DB01_ID  = str(uuid.UUID("00000000-0008-0001-0002-000000000001"))
HJMM_PROC01_ID = str(uuid.UUID("00000000-0008-0001-0003-000000000001"))
HJMM_RPRX01_ID = str(uuid.UUID("00000000-0008-0001-0004-000000000001"))
HJMM_FBRG01_ID = str(uuid.UUID("00000000-0008-0001-0005-000000000001"))
HJMM_WSS01_ID  = str(uuid.UUID("00000000-0008-0001-0006-000000000001"))


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: middleware_servers
    # Registry of all app VMs known to the platform.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "middleware_servers",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("name", sa.String(100), nullable=False, unique=True,
                  comment="MAIN-{ENV}-{ROLE}-{SEQ} e.g. MAIN-PRD-WEB-01"),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="8000"),
        sa.Column("role", sa.String(20), nullable=False,
                  comment="web | web-dev | rprx | proc | fbrg | wss | db"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active",
                  comment="active | draining | offline"),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_middleware_servers_name", "middleware_servers", ["name"])
    op.create_index("ix_middleware_servers_status", "middleware_servers", ["status"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: firmware_versions
    # Registry of built firmware bundles.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "firmware_versions",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("version_tag", sa.String(100), nullable=False, unique=True,
                  comment="e.g. v1.0.0-20260329"),
        sa.Column("manifest_hash", sa.String(64), nullable=False,
                  comment="SHA256 of manifest.json"),
        sa.Column("alembic_head", sa.String(40), nullable=False),
        sa.Column("bundle_path", sa.String(500), nullable=True,
                  comment="/opt/praesidium/firmware/bundles/{tag}.tar.gz"),
        sa.Column("openssl_sig", sa.String(500), nullable=True),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("built_by", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("bundle_includes_scripts", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("env_template", sa.Text, nullable=True,
                  comment="Bootstrap .env template with secrets blanked — stored for reference"),
    )
    op.create_index("ix_firmware_versions_tag", "firmware_versions", ["version_tag"])
    op.create_index("ix_firmware_versions_built_at", "firmware_versions", ["built_at"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: firmware_banks
    # Per-VM dual-bank model (bank_a / bank_b) — Cisco dual-flash pattern.
    # Bank swap: set → review → manual confirm → Docker rebuild → health check
    #            → commit or auto-rollback.
    # A config_backup is always auto-created before any bank operation.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "firmware_banks",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("vm_name", sa.String(100), nullable=False,
                  comment="Matches middleware_servers.name"),
        sa.Column("bank", sa.String(10), nullable=False,
                  comment="bank_a | bank_b"),
        sa.Column("firmware_version_id", sa.String(36),
                  sa.ForeignKey("firmware_versions.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("loaded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("loaded_by", sa.String(200), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("pending_active", sa.Boolean, nullable=False, server_default="false",
                  comment="True after bank set, before manual confirm"),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(200), nullable=True),
        sa.UniqueConstraint("vm_name", "bank", name="uq_firmware_banks_vm_bank"),
    )
    op.create_index("ix_firmware_banks_vm_name", "firmware_banks", ["vm_name"])
    op.create_index("ix_firmware_banks_is_active", "firmware_banks", ["is_active"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: firmware_events
    # Audit log for all firmware / infrastructure events.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "firmware_events",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("event_type", sa.String(50), nullable=False,
                  comment="deploy|bank_set|bank_confirmed|bank_rollback|rebuild|"
                          "snapshot|failover|config_save|config_restore|"
                          "docker_start|docker_stop|docker_restart|config_generate"),
        sa.Column("vm_name", sa.String(100), nullable=True),
        sa.Column("from_version", sa.String(100), nullable=True),
        sa.Column("to_version", sa.String(100), nullable=True),
        sa.Column("triggered_by", sa.String(200), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_firmware_events_type", "firmware_events", ["event_type"])
    op.create_index("ix_firmware_events_vm_name", "firmware_events", ["vm_name"])
    op.create_index("ix_firmware_events_created_at", "firmware_events", ["created_at"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: platform_manifest
    # Point-in-time snapshots of the full platform state (JSONB).
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "platform_manifest",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("manifest", JSONB, nullable=False),
        sa.Column("triggered_by", sa.String(200), nullable=True),
        sa.Column("alembic_head", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_platform_manifest_snapshot_at", "platform_manifest", ["snapshot_at"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: config_backups
    # Config snapshots — created automatically before every firmware operation
    # and on demand via admin panel or praesidium-ctl.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "config_backups",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("label", sa.String(200), nullable=True,
                  comment="e.g. pre-upgrade-v1.1.0 | post-hjmm-onboard"),
        sa.Column("backup_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("triggered_by", sa.String(200), nullable=True,
                  comment="admin_panel | praesidium-ctl | auto-pre-upgrade"),
        sa.Column("config_archive_path", sa.String(500), nullable=True,
                  comment="Path to .tar.gz on disk"),
        sa.Column("db_export_path", sa.String(500), nullable=True,
                  comment="Path to config-snapshot/ dir within archive"),
        sa.Column("config_hash", sa.String(64), nullable=True,
                  comment="SHA256 of archive"),
        sa.Column("openssl_sig", sa.String(500), nullable=True),
        sa.Column("alembic_head", sa.String(40), nullable=True),
        sa.Column("tenant_count", sa.Integer, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_auto", sa.Boolean, nullable=False, server_default="false",
                  comment="True = auto-created before firmware change"),
        sa.Column("pre_event_type", sa.String(50), nullable=True,
                  comment="Which event triggered auto-backup"),
    )
    op.create_index("ix_config_backups_backup_at", "config_backups", ["backup_at"])
    op.create_index("ix_config_backups_is_auto", "config_backups", ["is_auto"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: config_generated_scripts
    # Scripts produced by the config generator endpoint — one per VM role target.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "config_generated_scripts",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("source_vm", sa.String(100), nullable=False,
                  comment="VM the config was captured from"),
        sa.Column("target_role", sa.String(20), nullable=False,
                  comment="web | web-dev | rprx | proc | fbrg | wss | db"),
        sa.Column("target_hostname", sa.String(100), nullable=True,
                  comment="e.g. MAIN-DEV-WEB-01"),
        sa.Column("target_ip", sa.String(45), nullable=True),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("generated_by", sa.String(200), nullable=True),
        sa.Column("firmware_version", sa.String(100), nullable=True),
        sa.Column("script_content", sa.Text, nullable=False,
                  comment="The generated .sh provision script"),
        sa.Column("env_template", sa.Text, nullable=False,
                  comment="Bootstrap .env template (secrets blanked)"),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_config_generated_scripts_generated_at", "config_generated_scripts", ["generated_at"])
    op.create_index("ix_config_generated_scripts_target_role", "config_generated_scripts", ["target_role"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: praesidium_connect_sites
    # Registry of deployed instances for Praesidium Connect.
    # Off by default. No client data transmitted — firmware version, VM health,
    # license check-in only.
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "praesidium_connect_sites",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("site_code", sa.String(50), nullable=False, unique=True),
        sa.Column("site_name", sa.String(200), nullable=True),
        sa.Column("firmware_version", sa.String(100), nullable=True),
        sa.Column("alembic_head", sa.String(40), nullable=True),
        sa.Column("last_checkin", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("checkin_ip", sa.String(45), nullable=True),
        sa.Column("health_status", JSONB, nullable=True),
        sa.Column("license_status", sa.String(20), nullable=True, server_default="active"),
        sa.Column("connect_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("site_cert_fingerprint", sa.String(200), nullable=True),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_praesidium_connect_sites_site_code", "praesidium_connect_sites", ["site_code"])
    op.create_index("ix_praesidium_connect_sites_last_checkin", "praesidium_connect_sites", ["last_checkin"])

    # ──────────────────────────────────────────────────────────────────────────
    # TABLE: storage_health_snapshots
    # Periodic snapshots of CIFS / local mount health per VM.
    # Not tenant-scoped at platform level (tenant_id is optional — NULL means
    # platform-level storage, non-NULL means tenant storage mount).
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "storage_health_snapshots",
        sa.Column("id", sa.String(36), primary_key=True, server_default=sa.text("uuid_generate_v4()::text")),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("vm_name", sa.String(100), nullable=True),
        sa.Column("mount_point", sa.String(200), nullable=False),
        sa.Column("share_path", sa.String(500), nullable=True),
        sa.Column("is_mounted", sa.Boolean, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=True),
        sa.Column("used_bytes", sa.BigInteger, nullable=True),
        sa.Column("free_bytes", sa.BigInteger, nullable=True),
        sa.Column("tenant_id", sa.String(36), nullable=True,
                  comment="NULL = platform storage; non-NULL = tenant CIFS mount"),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index("ix_storage_health_snapshots_snapshot_at", "storage_health_snapshots", ["snapshot_at"])
    op.create_index("ix_storage_health_snapshots_mount_point", "storage_health_snapshots", ["mount_point"])
    op.create_index("ix_storage_health_snapshots_tenant_id", "storage_health_snapshots", ["tenant_id"])

    # ──────────────────────────────────────────────────────────────────────────
    # COLUMN ADDITIONS to existing tables
    # ──────────────────────────────────────────────────────────────────────────
    op.add_column(
        "tenants",
        sa.Column("middleware_server_id", sa.String(36),
                  sa.ForeignKey("middleware_servers.id", ondelete="SET NULL"),
                  nullable=True,
                  comment="Primary WEB VM for this tenant"),
    )
    op.add_column(
        "tenants",
        sa.Column("connect_site_id", sa.String(36),
                  sa.ForeignKey("praesidium_connect_sites.id", ondelete="SET NULL"),
                  nullable=True,
                  comment="Praesidium Connect registration (if opted in)"),
    )
    op.create_index("ix_tenants_middleware_server_id", "tenants", ["middleware_server_id"])

    # ──────────────────────────────────────────────────────────────────────────
    # SEED: middleware_servers — HJMM production VMs
    # ──────────────────────────────────────────────────────────────────────────
    op.execute(
        sa.text("""
        INSERT INTO middleware_servers (id, name, ip, port, role, status, is_default, notes)
        VALUES
          (:web01_id,  'MAIN-PRD-WEB-01',  '10.10.60.10', 8000, 'web',   'active', true,
           'HJMM primary app server — seeded by M8 C0a migration'),
          (:db01_id,   'MAIN-PRD-DB-01',   '10.10.60.11', 5432, 'db',    'active', false,
           'HJMM PostgreSQL 16 / PgBouncer :6432'),
          (:proc01_id, 'MAIN-PRD-PROC-01', '10.10.60.12', 9200, 'proc',  'active', false,
           'HJMM RQ workers + Redis :6379 + Elasticsearch :9200'),
          (:rprx01_id, 'MAIN-DMZ-RPRX-01', '10.10.40.50', 443,  'rprx',  'active', false,
           'HJMM DMZ reverse proxy — sidecar :8099'),
          (:fbrg01_id, 'MAIN-PRD-FBRG-01', '10.10.60.13', 8001, 'fbrg',  'active', false,
           'HJMM CIFS file bridge — legacy storage_mode'),
          (:wss01_id,  'MAIN-PRD-WSS-01',  '10.10.60.14', 9000, 'wss',   'active', false,
           'HJMM Whisper STT server')
        ON CONFLICT (name) DO NOTHING
        """),
        {
            "web01_id": HJMM_WEB01_ID,
            "db01_id": HJMM_DB01_ID,
            "proc01_id": HJMM_PROC01_ID,
            "rprx01_id": HJMM_RPRX01_ID,
            "fbrg01_id": HJMM_FBRG01_ID,
            "wss01_id": HJMM_WSS01_ID,
        }
    )

    # ──────────────────────────────────────────────────────────────────────────
    # SEED: firmware_banks — bank_a / bank_b for each HJMM VM (empty banks)
    # bank_a = active (no firmware loaded yet — will be set at first bundle build)
    # ──────────────────────────────────────────────────────────────────────────
    vms = [
        ("MAIN-PRD-WEB-01",  HJMM_WEB01_ID),
        ("MAIN-PRD-DB-01",   HJMM_DB01_ID),
        ("MAIN-PRD-PROC-01", HJMM_PROC01_ID),
        ("MAIN-DMZ-RPRX-01", HJMM_RPRX01_ID),
        ("MAIN-PRD-FBRG-01", HJMM_FBRG01_ID),
        ("MAIN-PRD-WSS-01",  HJMM_WSS01_ID),
    ]
    for vm_name, _vm_id in vms:
        for bank in ("bank_a", "bank_b"):
            is_active = bank == "bank_a"
            op.execute(
                sa.text("""
                INSERT INTO firmware_banks
                  (id, vm_name, bank, firmware_version_id, is_active, pending_active)
                VALUES
                  (uuid_generate_v4()::text, :vm_name, :bank, NULL, :is_active, false)
                ON CONFLICT (vm_name, bank) DO NOTHING
                """),
                {"vm_name": vm_name, "bank": bank, "is_active": is_active}
            )

    # ──────────────────────────────────────────────────────────────────────────
    # SEED: credentials_vault — HJMM LDAP stub
    #
    # This row is a STUB. encrypted_value is NULL.
    # The vault entry will be populated by hjmm_ldap_cutover.sql when you are
    # ready to move LDAP auth from .env to the database.
    #
    # The auth loader will not use this row while tenant_auth_config.is_active
    # is False. HJMM logins continue to use .env LDAP vars.
    # ──────────────────────────────────────────────────────────────────────────
    op.execute(
        sa.text("""
        INSERT INTO credentials_vault
          (id, tenant_id, credential_type, encrypted_value, notes, created_at, updated_at)
        VALUES
          (
            :vault_id,
            :tenant_id_placeholder,
            'ldap_bind_password',
            NULL,
            'STUB — HJMM LDAP bind password. Populate and activate via hjmm_ldap_cutover.sql. '
            'Auth loader ignores this row while tenant_auth_config.is_active=False.',
            NOW(),
            NOW()
          )
        ON CONFLICT (id) DO NOTHING
        """),
        {
            "vault_id": HJMM_VAULT_STUB_ID,
            "tenant_id_placeholder": HJMM_TENANT_ID_PLACEHOLDER,
        }
    )

    # ──────────────────────────────────────────────────────────────────────────
    # SEED: tenant_auth_config — HJMM LDAP stub
    #
    # is_active = False  → auth loader ignores this row, falls back to .env
    # credential_ref = NULL  → no vault secret linked yet
    # config JSONB pre-populated with correct HJMM LDAP structure (non-secret
    # values only). Secrets (bind password) live in credentials_vault via
    # credential_ref, which is set by hjmm_ldap_cutover.sql at cutover time.
    # ──────────────────────────────────────────────────────────────────────────
    op.execute(
        sa.text("""
        INSERT INTO tenant_auth_config
          (id, tenant_id, adapter, config, credential_ref, is_active, created_at, updated_at)
        VALUES
          (
            :auth_config_id,
            :tenant_id_placeholder,
            'ldaps',
            :config_json::jsonb,
            NULL,
            false,
            NOW(),
            NOW()
          )
        ON CONFLICT (tenant_id) DO NOTHING
        """),
        {
            "auth_config_id": HJMM_AUTH_CONFIG_STUB_ID,
            "tenant_id_placeholder": HJMM_TENANT_ID_PLACEHOLDER,
            "config_json": """{
                "server_url": "ldaps://10.10.10.1:636",
                "base_dn": "DC=hjmmlegal,DC=com",
                "bind_dn": "CN=praesidium,CN=Users,DC=hjmmlegal,DC=com",
                "user_search_base": "OU=HJMM Users,DC=hjmmlegal,DC=com",
                "user_filter": "(sAMAccountName={username})",
                "group_search_base": "DC=hjmmlegal,DC=com",
                "verify_cert": true,
                "stub_note": "INACTIVE STUB — activate via hjmm_ldap_cutover.sql after admin panel is live. .env LDAP vars remain authoritative until cutover."
            }""",
        }
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Seed a firmware_event recording this migration
    # ──────────────────────────────────────────────────────────────────────────
    op.execute(
        sa.text("""
        INSERT INTO firmware_events
          (event_type, vm_name, triggered_by, detail, created_at)
        VALUES
          ('deploy', NULL, 'alembic-migration-0010',
           '{"note": "M8 C0a schema migration complete — platform control tables created, HJMM middleware servers seeded, HJMM LDAP stubs seeded (inactive)"}',
           NOW())
        """)
    )


def downgrade() -> None:
    # Remove column additions first (FK deps)
    op.drop_index("ix_tenants_middleware_server_id", table_name="tenants")
    op.drop_column("tenants", "connect_site_id")
    op.drop_column("tenants", "middleware_server_id")

    # Drop tables in reverse dependency order
    op.drop_table("storage_health_snapshots")
    op.drop_table("praesidium_connect_sites")
    op.drop_table("config_generated_scripts")
    op.drop_table("config_backups")
    op.drop_table("platform_manifest")
    op.drop_table("firmware_events")
    op.drop_table("firmware_banks")
    op.drop_table("firmware_versions")
    op.drop_table("middleware_servers")

    # Note: credentials_vault and tenant_auth_config HJMM stub rows are NOT
    # removed on downgrade — they are inactive and harmless. Remove manually
    # if needed: DELETE FROM tenant_auth_config WHERE id = '00000000-0001-0001-0002-000000000001';
    #            DELETE FROM credentials_vault WHERE id = '00000000-0001-0001-0001-000000000001';
