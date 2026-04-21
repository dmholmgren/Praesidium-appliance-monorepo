#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  05-praesidium-fbrg.sh — v3.0
#  Target: MAIN-PRD-FBRG-01 (10.10.60.13)
#  Role: CIFS File Bridge (Docker) — legacy DMS access
#
#  v3.0 changes vs v2.0:
#    + /etc/praesidium/ bootstrap slot
#    + UFW: port 8001 restricted to WEB-01 only
#    + Credential file written to /etc/samba/ with 600 perms
#    + Multi-mount support (four HJMM CIFS shares)
#
#  DEPLOY ORDER: FBRG-01 provisioned AFTER WEB-01.
#  Usage: sudo bash 05-praesidium-fbrg.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; WHITE='\033[1;37m'; NC='\033[0m'; BOLD='\033[1m'

print_step()    { echo -e "${CYAN}  ▶  $1${NC}"; }
print_ok()      { echo -e "${GREEN}  ✓  $1${NC}"; }
print_warn()    { echo -e "${YELLOW}  ⚠  $1${NC}"; }
print_section() { echo ""; echo -e "${WHITE}${BOLD}── $1 ──────────────────────────────────────────────────────────${NC}"; echo ""; }

DEPLOY_USER="${SUDO_USER:-dmholmgren}"

clear
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  File Bridge  |  MAIN-PRD-FBRG-01  |  v3.0${NC}         ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}CIFS mounts  |  Docker  |  10.10.60.13${NC}                        ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && echo -e "${RED}Must run as root${NC}" && exit 1
! command -v fail2ban-client &>/dev/null && echo -e "${RED}Run 00-praesidium-base.sh first${NC}" && exit 1

# ── Inputs ────────────────────────────────────────────────────────────────────
echo -e "  ${WHITE}AD service account: praesidium (CN=Users,DC=hjmmlegal,DC=com)${NC}"
read -r -p "  CIFS file server IP [10.10.10.10]: " FS_IP; FS_IP="${FS_IP:-10.10.10.10}"
read -r -p "  AD domain [hjmmlegal]: " CIFS_DOMAIN; CIFS_DOMAIN="${CIFS_DOMAIN:-hjmmlegal}"
read -r -p "  AD username [praesidium]: " CIFS_USER; CIFS_USER="${CIFS_USER:-praesidium}"
read -r -s -p "  AD password for '${CIFS_USER}': " CIFS_PASS; echo ""
read -r -p "  WEB-01 IP [10.10.60.10]: " WEB_IP; WEB_IP="${WEB_IP:-10.10.60.10}"

# ── 1. CIFS client ────────────────────────────────────────────────────────────
print_section "CIFS Client"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cifs-utils keyutils
print_ok "cifs-utils and keyutils installed"

# ── 2. Credentials file ───────────────────────────────────────────────────────
print_section "CIFS Credentials"
mkdir -p /etc/samba
cat > /etc/samba/praesidium.creds << EOF
username=${CIFS_USER}
password=${CIFS_PASS}
domain=${CIFS_DOMAIN}
EOF
chmod 600 /etc/samba/praesidium.creds
print_ok "/etc/samba/praesidium.creds (600)"

# ── 3. CIFS mounts — HJMM four shares ────────────────────────────────────────
print_section "CIFS Mounts"
CIFS_OPTS="credentials=/etc/samba/praesidium.creds,uid=1000,gid=1000,iocharset=utf8,vers=3.0,nofail,_netdev"

declare -A SHARES=(
    ["/mnt/matters"]="${FS_IP}/Matters"
    ["/mnt/clients"]="${FS_IP}/Clients"
    ["/mnt/docsend"]="${FS_IP}/DocSend"
    ["/mnt/archive"]="${FS_IP}/Archive"
)

for MOUNT_POINT in "${!SHARES[@]}"; do
    SHARE="//${SHARES[$MOUNT_POINT]}"
    mkdir -p "${MOUNT_POINT}"
    FSTAB_ENTRY="${SHARE} ${MOUNT_POINT} cifs ${CIFS_OPTS} 0 0"
    if ! grep -qF "${MOUNT_POINT}" /etc/fstab; then
        echo "${FSTAB_ENTRY}" >> /etc/fstab
        print_ok "fstab: ${SHARE} → ${MOUNT_POINT}"
    else
        print_ok "${MOUNT_POINT} already in fstab"
    fi
done

# Try to mount now (non-fatal if file server not reachable)
mount -a --types cifs 2>/dev/null && print_ok "CIFS mounts active" || \
    print_warn "CIFS mount failed — file server may be unreachable. Mounts will activate on next boot."

# ── 4. /etc/praesidium/ bootstrap slot ───────────────────────────────────────
print_section "Praesidium Config Slot"
mkdir -p /etc/praesidium
chmod 750 /etc/praesidium
if [ ! -f /etc/praesidium/.env.bootstrap ]; then
    cat > /etc/praesidium/.env.bootstrap << ENVEOF
# FBRG-01 bootstrap .env
CIFS_SHARE_PATH=//${FS_IP}/Matters
CIFS_DOMAIN=${CIFS_DOMAIN}
CIFS_USERNAME=${CIFS_USER}
CIFS_PASSWORD=${CIFS_PASS}
BRIDGE_SECRET=__FILL_IN__
ENVEOF
    chmod 600 /etc/praesidium/.env.bootstrap
    print_ok "/etc/praesidium/.env.bootstrap written"
fi

# ── 5. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
ufw allow from "${WEB_IP}" to any port 8001 comment 'File bridge — WEB-01 only' > /dev/null
ufw reload > /dev/null
print_ok "Port 8001: ${WEB_IP} (WEB-01) only"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}FBRG-01 Setup Complete${NC}                                            ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Mounts:${NC} /mnt/matters /mnt/clients /mnt/docsend /mnt/archive   ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
