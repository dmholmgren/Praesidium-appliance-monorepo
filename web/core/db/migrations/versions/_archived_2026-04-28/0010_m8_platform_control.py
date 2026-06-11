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
    # (Patched: op.bulk_insert replaces broken op.execute(text, params) call)
    # ──────────────────────────────────────────────────────────────────────────
    middleware_servers_seed = sa.table(
        "middleware_servers",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("ip", sa.String),
        sa.column("port", sa.Integer),
        sa.column("role", sa.String),
        sa.column("status", sa.String),
        sa.column("is_default", sa.Boolean),
        sa.column("notes", sa.Text),
    )
    op.bulk_insert(middleware_servers_seed, [
        {"id": HJMM_WEB01_ID,  "name": "MAIN-PRD-WEB-01",  "ip": "10.10.60.10", "port": 8000, "role": "web",   "status": "active", "is_default": True,  "notes": "HJMM primary app server — seeded by M8 C0a migration"},
        {"id": HJMM_DB01_ID,   "name": "MAIN-PRD-DB-01",   "ip": "10.10.60.11", "port": 5432, "role": "db",    "status": "active", "is_default": False, "notes": "HJMM PostgreSQL 16 / PgBouncer :6432"},
        {"id": HJMM_PROC01_ID, "name": "MAIN-PRD-PROC-01", "ip": "10.10.60.12", "port": 9200, "role": "proc",  "status": "active", "is_default": False, "notes": "HJMM RQ workers + Redis :6379 + Elasticsearch :9200"},
        {"id": HJMM_RPRX01_ID, "name": "MAIN-DMZ-RPRX-01", "ip": "10.10.40.50", "port": 443,  "role": "rprx",  "status": "active", "is_default": False, "notes": "HJMM DMZ reverse proxy — sidecar :8099"},
        {"id": HJMM_FBRG01_ID, "name": "MAIN-PRD-FBRG-01", "ip": "10.10.60.13", "port": 8001, "role": "fbrg",  "status": "active", "is_default": False, "notes": "HJMM CIFS file bridge — legacy storage_mode"},
        {"id": HJMM_WSS01_ID,  "name": "MAIN-PRD-WSS-01",  "ip": "10.10.60.14", "port": 9000, "role": "wss",   "status": "active", "is_default": False, "notes": "HJMM Whisper STT server"},
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # SEED: firmware_banks — bank_a / bank_b for each HJMM VM (empty banks)
    # bank_a = active (no firmware loaded yet — set at first bundle build)
    # ──────────────────────────────────────────────────────────────────────────
    firmware_banks_seed = sa.table(
        "firmware_banks",
        sa.column("id", sa.String),
        sa.column("vm_name", sa.String),
        sa.column("bank", sa.String),
        sa.column("firmware_version_id", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("pending_active", sa.Boolean),
    )
    vms = [
        ("MAIN-PRD-WEB-01",  HJMM_WEB01_ID),
        ("MAIN-PRD-DB-01",   HJMM_DB01_ID),
        ("MAIN-PRD-PROC-01", HJMM_PROC01_ID),
        ("MAIN-DMZ-RPRX-01", HJMM_RPRX01_ID),
        ("MAIN-PRD-FBRG-01", HJMM_FBRG01_ID),
        ("MAIN-PRD-WSS-01",  HJMM_WSS01_ID),
    ]
    firmware_banks_rows = []
    for vm_name, _vm_id in vms:
        for bank in ("bank_a", "bank_b"):
            firmware_banks_rows.append({
                "id": str(uuid.uuid4()),
                "vm_name": vm_name,
                "bank": bank,
                "firmware_version_id": None,
                "is_active": (bank == "bank_a"),
                "pending_active": False,
            })
    op.bulk_insert(firmware_banks_seed, firmware_banks_rows)


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
