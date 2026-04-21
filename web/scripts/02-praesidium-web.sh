#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  02-praesidium-web.sh — v3.0
#  Target: MAIN-PRD-WEB-01 (10.10.60.10) — and MAIN-DEV-WEB-01 (10.10.60.15)
#  Role: FastAPI app container (Docker CE) + generate-env.sh + Alembic
#
#  What it does:
#    1. Python build dependencies (libpq-dev, libldap2-dev, libsasl2-dev)
#    2. Docker CE from official APT repo
#    3. UFW: 8000 from RPRX-01 and PROC-01 only
#    4. /opt/praesidium/ application directory
#    5. /opt/praesidium/scripts/ — provision scripts slot
#    6. /opt/praesidium/bundles/ — firmware bundle output
#    7. generate-env.sh prompt (optional)
#
#  v3.0 changes vs v2.0:
#    + /opt/praesidium/scripts/ directory (config generator reads from here)
#    + /opt/praesidium/bundles/ directory (bundle assembler writes here)
#    + git clone prompt replaced with SFTP deploy instructions
#    + bootstrap .env.bootstrap notes
#    + Alembic upgrade + seed reminder in summary
#
#  DEPLOY ORDER: WEB-01 provisioned AFTER RPRX-01, DB-01, and PROC-01.
#  Usage: sudo bash 02-praesidium-web.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; WHITE='\033[1;37m'; NC='\033[0m'; BOLD='\033[1m'

print_step()    { echo -e "${CYAN}  ▶  $1${NC}"; }
print_ok()      { echo -e "${GREEN}  ✓  $1${NC}"; }
print_warn()    { echo -e "${YELLOW}  ⚠  $1${NC}"; }
print_error()   { echo -e "${RED}  ✗  $1${NC}"; }
print_section() { echo ""; echo -e "${WHITE}${BOLD}── $1 ──────────────────────────────────────────────────────────${NC}"; echo ""; }

DEPLOY_USER="${SUDO_USER:-dmholmgren}"

clear
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Web Application  |  MAIN-PRD-WEB-01  |  v3.0${NC}     ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}FastAPI  |  Docker CE  |  Python 3.12  |  10.10.60.10${NC}           ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Deploy user: ${BOLD}${DEPLOY_USER}${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && print_error "Must run as root: sudo bash 02-praesidium-web.sh" && exit 1
! command -v fail2ban-client &>/dev/null && print_error "Run 00-praesidium-base.sh first" && exit 1

# ── 1. Python build dependencies ─────────────────────────────────────────────
print_section "Python Build Dependencies"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-dev python3-pip python3-venv \
    libpq-dev libldap2-dev libsasl2-dev build-essential pkg-config
print_ok "libpq-dev (asyncpg) | libldap2-dev (LDAP auth) | libsasl2-dev"

# ── 2. Docker CE ──────────────────────────────────────────────────────────────
print_section "Docker CE Installation"
print_warn "Installing from official Docker APT repo — NOT snap/ubuntu package"
apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 \
    podman-docker containerd runc 2>/dev/null || true
apt-get install -y -qq ca-certificates curl gnupg lsb-release
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

usermod -aG docker "${DEPLOY_USER}"
print_ok "User '${DEPLOY_USER}' added to docker group (re-login to activate)"

cat > /etc/docker/daemon.json << 'EOF'
{
  "default-address-pools": [{"base": "10.200.0.0/16", "size": 24}],
  "log-driver": "json-file",
  "log-opts": {"max-size": "100m", "max-file": "5"},
  "live-restore": true
}
EOF
systemctl enable docker > /dev/null 2>&1
systemctl restart docker
sleep 2
print_ok "$(docker --version)"
print_ok "$(docker compose version)"

# ── 3. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
read -r -p "  RPRX-01 IP [10.10.40.50]: " RPRX_IP; RPRX_IP="${RPRX_IP:-10.10.40.50}"
read -r -p "  PROC-01 IP [10.10.60.12]: " PROC_IP; PROC_IP="${PROC_IP:-10.10.60.12}"
ufw allow from "${RPRX_IP}" to any port 8000 comment 'App from RPRX-01' > /dev/null
ufw allow from "${PROC_IP}" to any port 8000 comment 'App from PROC-01 (RQ callbacks)' > /dev/null
ufw reload > /dev/null
print_ok "Port 8000: ${RPRX_IP} (RPRX-01) and ${PROC_IP} (PROC-01) only"

# ── 4. Application directories ────────────────────────────────────────────────
print_section "Application Directories"
# App root
mkdir -p /opt/praesidium
chown "${DEPLOY_USER}:${DEPLOY_USER}" /opt/praesidium

# Config generator: provision scripts go here (read by config_generator.py)
mkdir -p /opt/praesidium/scripts
chown "${DEPLOY_USER}:${DEPLOY_USER}" /opt/praesidium/scripts
chmod 750 /opt/praesidium/scripts

# Bundle assembler: output .tar.gz bundles go here
mkdir -p /opt/praesidium/bundles
chown "${DEPLOY_USER}:${DEPLOY_USER}" /opt/praesidium/bundles
chmod 750 /opt/praesidium/bundles

# Firmware bank directories: bank_a and bank_b
mkdir -p /opt/praesidium/firmware/bank_a
mkdir -p /opt/praesidium/firmware/bank_b
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" /opt/praesidium/firmware
chmod -R 750 /opt/praesidium/firmware

print_ok "/opt/praesidium/         — app root"
print_ok "/opt/praesidium/scripts/ — provision scripts (config generator reads here)"
print_ok "/opt/praesidium/bundles/ — bundle assembler output"
print_ok "/opt/praesidium/firmware/bank_a|bank_b — dual firmware banks"

# ── 5. Deploy provision scripts to scripts/ slot ─────────────────────────────
print_section "Provision Scripts Deployment"
print_warn "Deploy v3.0 provision scripts to /opt/praesidium/scripts/ via Bitvise SFTP:"
echo ""
echo -e "  ${CYAN}# Upload via Bitvise SFTP then:${NC}"
echo -e "  ${WHITE}sudo mv ~/00-praesidium-base.sh /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/01-praesidium-rprx.sh /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/02-praesidium-web.sh  /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/03-praesidium-db.sh   /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/04-praesidium-proc.sh /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/05-praesidium-fbrg.sh /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo mv ~/06-praesidium-wss.sh  /opt/praesidium/scripts/${NC}"
echo -e "  ${WHITE}sudo chmod 750 /opt/praesidium/scripts/*.sh${NC}"
echo -e "  ${WHITE}sudo chown ${DEPLOY_USER}:${DEPLOY_USER} /opt/praesidium/scripts/*.sh${NC}"
echo ""
read -r -p "  Press Enter to continue after deploying scripts (or skip)..."

# ── 6. Application code deployment ───────────────────────────────────────────
print_section "Application Code Deployment"
print_warn "Deploy application code via Bitvise SFTP:"
echo ""
echo -e "  ${CYAN}# Option A — SFTP zip upload:${NC}"
echo -e "  ${WHITE}Upload praesidium-app.zip → ~/praesidium-app.zip${NC}"
echo -e "  ${WHITE}unzip ~/praesidium-app.zip -d /opt/praesidium/${NC}"
echo ""
echo -e "  ${CYAN}# Option B — Git clone:${NC}"
echo -e "  ${WHITE}sudo -u ${DEPLOY_USER} git clone <GIT_URL> /opt/praesidium${NC}"
echo ""
read -r -p "  Press Enter to continue after code is in place..."

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}WEB-01 VM Setup Complete${NC}                                          ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Next steps (in order):${NC}                                          ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    1. cd /opt/praesidium                                      ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    2. sudo bash generate-env.sh   (creates .env)              ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    3. sudo docker compose build --no-cache                    ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    4. sudo docker compose run --rm web alembic upgrade head   ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    5. sudo docker compose up -d                               ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    6. docker logs -f praesidium-web                           ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Config generator note:${NC}                                         ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    Provision scripts must be in /opt/praesidium/scripts/      ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    POST /admin/api/config/generate reads from that directory  ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
