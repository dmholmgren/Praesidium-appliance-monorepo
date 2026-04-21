#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  03-praesidium-db.sh — v3.0
#  Target: MAIN-PRD-DB-01 (10.10.60.11) — BARE METAL INSTALL
#  Role: PostgreSQL 16 + PgBouncer + pgvector + pg_trgm + btree_gin
#
#  v3.0 changes vs v2.1:
#    + /etc/praesidium/ bootstrap config slot created
#    + Saves DB credentials to /root/.praesidium-db-credentials for
#      generate-env.sh to consume (auto-delete prompt added)
#    + UFW: restricts DB port 5432/6432 to internal subnet only
#    + pg_trgm + btree_gin + uuid-ossp explicitly confirmed
#    + Summary: explicit next steps for WEB-01
#
#  Prerequisites:
#    1. 00-praesidium-base.sh complete
#    2. /mnt/data mounted (LABEL=praesidium-pgdat) — run migrate-db01-datadisk.sh
#
#  DEPLOY ORDER: DB-01 provisioned AFTER RPRX-01, BEFORE PROC-01 and WEB-01.
#  Usage: sudo bash 03-praesidium-db.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; WHITE='\033[1;37m'; NC='\033[0m'; BOLD='\033[1m'

print_step()    { echo -e "${CYAN}  ▶  $1${NC}"; }
print_ok()      { echo -e "${GREEN}  ✓  $1${NC}"; }
print_warn()    { echo -e "${YELLOW}  ⚠  $1${NC}"; }
print_error()   { echo -e "${RED}  ✗  $1${NC}"; }
print_section() { echo ""; echo -e "${WHITE}${BOLD}── $1 ──────────────────────────────────────────────────────────${NC}"; echo ""; }

clear
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Database  |  MAIN-PRD-DB-01  |  v3.0${NC}             ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}PostgreSQL 16  |  PgBouncer  |  pgvector  |  10.10.60.11${NC}      ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

IP=$(hostname -I | awk '{print $1}')
echo -e "  Hostname:   ${BOLD}$(hostname)${NC}"
echo -e "  IP:         ${BOLD}${IP}${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && print_error "Must run as root: sudo bash 03-praesidium-db.sh" && exit 1
! command -v fail2ban-client &>/dev/null && print_error "Run 00-praesidium-base.sh first" && exit 1

# /mnt/data pre-flight
if ! mountpoint -q /mnt/data; then
    print_error "/mnt/data is not mounted."
    echo -e "  ${WHITE}Attach the 1.5TB VHDX in Hyper-V Manager, then run:${NC}"
    echo -e "  ${CYAN}  sudo bash migrate-db01-datadisk.sh${NC}"
    exit 1
fi
print_ok "/mnt/data mounted (LABEL=praesidium-pgdat)"

# ── Inputs ────────────────────────────────────────────────────────────────────
read -r -p "  Database name [praesidium_hjmm]: " DB_NAME
DB_NAME="${DB_NAME:-praesidium_hjmm}"
read -r -p "  DB user [praesidium_db]: " DB_USER
DB_USER="${DB_USER:-praesidium_db}"
read -r -s -p "  DB password (choose a strong password): " DB_PASSWORD; echo ""
[ -z "$DB_PASSWORD" ] && print_error "DB password cannot be empty" && exit 1

read -r -p "  Internal subnet allowed to connect [10.10.60.0/24]: " INTERNAL_SUBNET
INTERNAL_SUBNET="${INTERNAL_SUBNET:-10.10.60.0/24}"

# ── 1. PostgreSQL 16 ─────────────────────────────────────────────────────────
print_section "PostgreSQL 16"
apt-get install -y -qq curl ca-certificates
install -d /usr/share/postgresql-common/pgdg
curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc 2>/dev/null
bash /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y -v 16 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-16 postgresql-client-16
print_ok "PostgreSQL 16 installed"

# ── 2. Move data dir to /mnt/data ────────────────────────────────────────────
print_section "Data Directory → /mnt/data"
PG_DATA="/var/lib/postgresql/16/main"
PG_DATA_TARGET="/mnt/data/postgresql/16/main"

if [ -d "${PG_DATA_TARGET}" ]; then
    print_ok "Data already on /mnt/data — skipping copy"
else
    systemctl stop postgresql
    mkdir -p /mnt/data/postgresql
    chown postgres:postgres /mnt/data/postgresql
    rsync -av "${PG_DATA}/" "${PG_DATA_TARGET}/"
    # Symlink: /var/lib/postgresql → /mnt/data/postgresql
    mv /var/lib/postgresql /var/lib/postgresql.bak
    ln -s /mnt/data/postgresql /var/lib/postgresql
    chown -h postgres:postgres /var/lib/postgresql
    systemctl start postgresql
    print_ok "Data moved to ${PG_DATA_TARGET} | symlink: /var/lib/postgresql → /mnt/data/postgresql"
fi

# systemd override: require /mnt/data mount
mkdir -p /etc/systemd/system/postgresql.service.d
cat > /etc/systemd/system/postgresql.service.d/override.conf << 'EOF'
[Unit]
RequiresMountsFor=/mnt/data
After=mnt-data.mount
EOF
systemctl daemon-reload
print_ok "systemd override: postgresql requires /mnt/data"

# ── 3. Extensions ────────────────────────────────────────────────────────────
print_section "PostgreSQL Extensions"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    postgresql-16-pgvector 2>/dev/null || \
    print_warn "pgvector package not found — install from source after PG install"

# Enable extensions in the DB (will be created after DB creation below)
print_ok "pgvector pkg installed (or skipped — see warning)"

# ── 4. Create database and user ───────────────────────────────────────────────
print_section "Database and User Creation"
sudo -u postgres psql -c "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE USER ${DB_USER} WITH ENCRYPTED PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
"
sudo -u postgres psql -c "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}') THEN
    CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8';
  END IF;
END
\$\$;
"
sudo -u postgres psql -d "${DB_NAME}" << SQLEOF
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gin";
CREATE EXTENSION IF NOT EXISTS "vector";
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
GRANT ALL ON SCHEMA public TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ${DB_USER};
SQLEOF
print_ok "Database: ${DB_NAME} | User: ${DB_USER}"
print_ok "Extensions: uuid-ossp, pg_trgm, btree_gin, vector (pgvector)"

# ── 5. postgresql.conf tuning ────────────────────────────────────────────────
print_section "PostgreSQL Configuration Tuning"
PG_CONF="/etc/postgresql/16/main/postgresql.conf"
PG_HBA="/etc/postgresql/16/main/pg_hba.conf"

# Basic production tuning (conservative — adjust per RAM)
cat >> "${PG_CONF}" << 'PGEOF'

# PRAESIDIUM Series 2.0 tuning — v3.0
listen_addresses = '*'
max_connections = 200
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
log_min_duration_statement = 2000
log_line_prefix = '%t [%p]: [%l-1] user=%u,db=%d,app=%a,client=%h '
PGEOF

# pg_hba: allow praesidium_db from internal subnet
cat >> "${PG_HBA}" << HBAEOF

# PRAESIDIUM — allow app server subnet
host    ${DB_NAME}    ${DB_USER}    ${INTERNAL_SUBNET}    scram-sha-256
HBAEOF

systemctl restart postgresql
print_ok "postgresql.conf tuned | pg_hba.conf: ${INTERNAL_SUBNET} → ${DB_NAME}"

# ── 6. PgBouncer ─────────────────────────────────────────────────────────────
print_section "PgBouncer (connection pooler — port 6432)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pgbouncer

cat > /etc/pgbouncer/pgbouncer.ini << PBEOF
[databases]
${DB_NAME} = host=127.0.0.1 port=5432 dbname=${DB_NAME}

[pgbouncer]
logfile = /var/log/postgresql/pgbouncer.log
pidfile = /var/run/postgresql/pgbouncer.pid
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 200
default_pool_size = 25
reserve_pool_size = 5
server_reset_query = DISCARD ALL
ignore_startup_parameters = extra_float_digits
PBEOF

# PgBouncer userlist
HASHED_PASS=$(sudo -u postgres psql -At -c \
    "SELECT 'scram-sha-256:' || encode(sha256(concat('${DB_PASSWORD}', '${DB_USER}')::bytea), 'base64') || ':foo'" 2>/dev/null || \
    echo "\"${DB_USER}\" \"${DB_PASSWORD}\"")
echo "\"${DB_USER}\" \"${DB_PASSWORD}\"" > /etc/pgbouncer/userlist.txt
chmod 640 /etc/pgbouncer/userlist.txt

systemctl enable pgbouncer > /dev/null 2>&1
systemctl restart pgbouncer
print_ok "PgBouncer: port 6432, transaction mode, pool 25"

# ── 7. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
ufw allow from "${INTERNAL_SUBNET}" to any port 5432 comment 'PostgreSQL — internal' > /dev/null
ufw allow from "${INTERNAL_SUBNET}" to any port 6432 comment 'PgBouncer — internal' > /dev/null
ufw reload > /dev/null
print_ok "Ports 5432+6432 restricted to ${INTERNAL_SUBNET}"

# ── 8. Credentials file for generate-env.sh ──────────────────────────────────
print_section "Credentials File"
cat > /root/.praesidium-db-credentials << CREDEOF
# Auto-generated by 03-praesidium-db.sh v3.0
# Consumed by generate-env.sh on WEB-01 — DELETE after use.
DB_HOST=$(hostname -I | awk '{print $1}')
DB_PORT_PGBOUNCER=6432
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD}
CREDEOF
chmod 600 /root/.praesidium-db-credentials
print_ok "/root/.praesidium-db-credentials written (600)"
print_warn "Transfer this file to WEB-01 then DELETE it from DB-01:"
echo -e "  ${CYAN}  scp /root/.praesidium-db-credentials dmholmgren@10.10.60.10:/root/${NC}"
echo -e "  ${CYAN}  sudo rm /root/.praesidium-db-credentials${NC}"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}DB-01 Setup Complete${NC}                                              ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Database:${NC} ${DB_NAME} | ${WHITE}User:${NC} ${DB_USER}              ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Next:${NC} sudo bash 04-praesidium-proc.sh  (on PROC-01)           ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
