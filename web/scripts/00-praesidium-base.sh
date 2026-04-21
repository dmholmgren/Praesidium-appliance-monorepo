#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  00-praesidium-base.sh — v3.0
#  Target: ALL VMs — run first, no exceptions
#  OS: Ubuntu 24.04 LTS (server, minimized, OpenSSH)
#
#  What it does:
#    1. System update + upgrade
#    2. Base packages (build tools, monitoring, networking, etc.)
#    3. UFW baseline (deny incoming, allow outgoing, allow SSH)
#    4. fail2ban
#    5. /etc/praesidium/ directory + .env.bootstrap slot
#    6. praesidium system user (no login shell)
#    7. Log rotation for /var/log/praesidium/
#
#  v3.0 changes vs v2.0:
#    + /etc/praesidium/ directory created (bootstrap .env slot)
#    + praesidium system user
#    + /var/log/praesidium/ with logrotate
#    + NTP/chrony instead of ntp package (Ubuntu 24 default)
#    + DEPLOY ORDER comment block
#
#  Usage: sudo bash 00-praesidium-base.sh
#  DEPLOY ORDER: Run on EVERY VM before the role-specific script.
#                RPRX-01 must be fully provisioned before any internal VM
#                is exposed to external traffic.
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
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Base VM Setup  |  ALL VMs  |  v3.0${NC}               ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}\"It's not what you know, it's what you're missing.\"${NC}            ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Hostname:    ${BOLD}$(hostname)${NC}"
echo -e "  IP:          ${BOLD}$(hostname -I | awk '{print $1}')${NC}"
echo -e "  Deploy user: ${BOLD}${DEPLOY_USER}${NC}"
echo ""
echo -e "  ${YELLOW}DEPLOY ORDER REMINDER${NC}"
echo -e "  ─────────────────────────────────────────────────────────"
echo -e "  Run 00-praesidium-base.sh on ALL VMs first, then:"
echo -e "    1. MAIN-DMZ-RPRX-01   sudo bash 01-praesidium-rprx.sh"
echo -e "    2. MAIN-PRD-DB-01     sudo bash 03-praesidium-db.sh"
echo -e "    3. MAIN-PRD-PROC-01   sudo bash 04-praesidium-proc.sh"
echo -e "    4. MAIN-PRD-WEB-01    sudo bash 02-praesidium-web.sh"
echo -e "    5. MAIN-PRD-FBRG-01   sudo bash 05-praesidium-fbrg.sh"
echo -e "    6. MAIN-PRD-WSS-01    sudo bash 06-praesidium-wss.sh"
echo -e "  ─────────────────────────────────────────────────────────"
echo ""

[ "$(id -u)" -ne 0 ] && print_error "Must run as root: sudo bash 00-praesidium-base.sh" && exit 1

# ── 1. System update ──────────────────────────────────────────────────────────
print_section "System Update"
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
print_ok "System updated"

# ── 2. Base packages ─────────────────────────────────────────────────────────
print_section "Base Packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl wget gnupg2 ca-certificates lsb-release \
    apt-transport-https software-properties-common \
    build-essential git unzip zip \
    htop iotop iftop nload \
    net-tools dnsutils iputils-ping traceroute \
    ufw fail2ban logrotate \
    vim nano tmux screen \
    python3 python3-pip python3-venv \
    cron rsync chrony \
    jq openssl
print_ok "Base packages installed"

# ── 3. UFW baseline ───────────────────────────────────────────────────────────
print_section "UFW Firewall Baseline"
ufw --force reset > /dev/null 2>&1
ufw default deny incoming > /dev/null
ufw default allow outgoing > /dev/null
ufw allow ssh comment 'SSH management' > /dev/null
ufw --force enable > /dev/null
print_ok "UFW: deny incoming, allow outgoing, SSH open"
print_warn "Role-specific ports opened by the role provision script"

# ── 4. fail2ban ───────────────────────────────────────────────────────────────
print_section "fail2ban"
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
backend  = auto

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
EOF
systemctl enable fail2ban > /dev/null 2>&1
systemctl restart fail2ban
print_ok "fail2ban configured: 5 retries / 10 min window / 1 hour ban"

# ── 5. /etc/praesidium/ — bootstrap config slot ──────────────────────────────
print_section "Praesidium Config Directory"
mkdir -p /etc/praesidium
chmod 750 /etc/praesidium

# Placeholder bootstrap .env — filled in by generate-env.sh or config generator
if [ ! -f /etc/praesidium/.env.bootstrap ]; then
    cat > /etc/praesidium/.env.bootstrap << 'ENVEOF'
# PRAESIDIUM BOOTSTRAP .env
# Fill in __FILL_IN__ values before first boot.
# See: POST /admin/api/config/generate for the config generator.
# Or:  bash generate-env.sh on WEB-01
#
# This file holds ONLY 6 bootstrap values needed before the database is
# reachable.  All other config lives in the database.
# Written ONCE at first provision.  Never overwritten by firmware ops.

DATABASE_URL=__FILL_IN__
REDIS_URL=__FILL_IN__
SECRET_KEY=__FILL_IN__
BRIDGE_SECRET=__FILL_IN__
PLATFORM_ADMIN_PASSWORD_HASH=__FILL_IN__
OPENAI_API_KEY=__FILL_IN__
ENVEOF
    chmod 600 /etc/praesidium/.env.bootstrap
    print_ok "/etc/praesidium/.env.bootstrap created (placeholder — fill before boot)"
else
    print_ok "/etc/praesidium/.env.bootstrap already exists — skipped"
fi

# Bundles directory
mkdir -p /opt/praesidium/bundles
chmod 750 /opt/praesidium/bundles

print_ok "/etc/praesidium/ (750) | /opt/praesidium/bundles/ (750)"

# ── 6. praesidium system user ────────────────────────────────────────────────
print_section "Praesidium System User"
if ! id praesidium &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin praesidium
    print_ok "System user 'praesidium' created (no login shell)"
else
    print_ok "System user 'praesidium' already exists — skipped"
fi
chown -R praesidium:praesidium /etc/praesidium 2>/dev/null || true

# ── 7. Log directory + rotation ──────────────────────────────────────────────
print_section "Log Directory"
mkdir -p /var/log/praesidium
chown praesidium:praesidium /var/log/praesidium
chmod 755 /var/log/praesidium

cat > /etc/logrotate.d/praesidium << 'EOF'
/var/log/praesidium/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0644 praesidium praesidium
    sharedscripts
    postrotate
        systemctl is-active praesidium-* > /dev/null 2>&1 && \
            kill -HUP $(systemctl show -p MainPID praesidium-* | cut -d= -f2) 2>/dev/null || true
    endscript
}
EOF
print_ok "/var/log/praesidium/ | logrotate: 14 days, daily, compressed"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}Base VM Setup Complete${NC}  |  $(hostname)${NC}  ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Next:${NC} Run the role-specific provision script.              ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
