#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  06-praesidium-wss.sh — v3.0
#  Target: MAIN-PRD-WSS-01 (10.10.60.14)
#  Role: Whisper STT (Docker) — speech-to-text for deposition/dictation
#
#  v3.0 changes vs v2.0:
#    + /etc/praesidium/ bootstrap slot
#    + UFW: port 9000 restricted to WEB-01 only
#    + Model size configurable (default: large-v3)
#    + Compute type configurable (default: int8 — CPU optimized)
#
#  DEPLOY ORDER: WSS-01 is optional on first deployment — provision last.
#  Usage: sudo bash 06-praesidium-wss.sh
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
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Whisper STT  |  MAIN-PRD-WSS-01  |  v3.0${NC}          ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}faster-whisper  |  Docker  |  10.10.60.14${NC}                      ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && echo -e "${RED}Must run as root${NC}" && exit 1
! command -v fail2ban-client &>/dev/null && echo -e "${RED}Run 00-praesidium-base.sh first${NC}" && exit 1

# ── Inputs ────────────────────────────────────────────────────────────────────
read -r -p "  Whisper model [large-v3]: " WHISPER_MODEL; WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"
read -r -p "  Compute type [int8]: " COMPUTE_TYPE; COMPUTE_TYPE="${COMPUTE_TYPE:-int8}"
read -r -p "  WEB-01 IP [10.10.60.10]: " WEB_IP; WEB_IP="${WEB_IP:-10.10.60.10}"

# ── 1. Docker CE ──────────────────────────────────────────────────────────────
print_section "Docker CE"
! command -v docker &>/dev/null && {
    apt-get remove -y docker.io docker-compose 2>/dev/null || true
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
}
systemctl enable docker > /dev/null 2>&1; systemctl start docker
print_ok "$(docker --version)"

# ── 2. Model cache directory ─────────────────────────────────────────────────
print_section "Whisper Model Cache"
mkdir -p /opt/whisper/models
chown -R 1000:1000 /opt/whisper
print_ok "/opt/whisper/models/ — model cache (persisted across container restarts)"
print_warn "Model '${WHISPER_MODEL}' (~3GB) downloads on first container start"

# ── 3. Whisper STT container ─────────────────────────────────────────────────
print_section "Whisper STT Container (faster-whisper)"

# Use fedirz/faster-whisper-server — well-maintained, REST API compatible
docker pull fedirz/faster-whisper-server:latest-cpu 2>&1 | tail -3

docker run -d \
    --name praesidium-whisper \
    --restart=always \
    -p 9000:8000 \
    -e WHISPER__MODEL="${WHISPER_MODEL}" \
    -e WHISPER__COMPUTE_TYPE="${COMPUTE_TYPE}" \
    -e WHISPER__DEVICE=cpu \
    -v /opt/whisper/models:/root/.cache/huggingface \
    fedirz/faster-whisper-server:latest-cpu 2>/dev/null || \
    print_warn "Container may already exist — check: docker ps -a | grep whisper"

print_ok "Whisper STT started: port 9000 | model: ${WHISPER_MODEL} | compute: ${COMPUTE_TYPE}"
print_warn "First startup downloads the model (~3GB) — monitor: docker logs -f praesidium-whisper"

# ── 4. /etc/praesidium/ bootstrap slot ───────────────────────────────────────
print_section "Praesidium Config Slot"
mkdir -p /etc/praesidium; chmod 750 /etc/praesidium
if [ ! -f /etc/praesidium/.env.bootstrap ]; then
    cat > /etc/praesidium/.env.bootstrap << ENVEOF
# WSS-01 bootstrap .env
WHISPER_MODEL=${WHISPER_MODEL}
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=${COMPUTE_TYPE}
BRIDGE_SECRET=__FILL_IN__
ENVEOF
    chmod 600 /etc/praesidium/.env.bootstrap
    print_ok "/etc/praesidium/.env.bootstrap written"
fi

# ── 5. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
ufw allow from "${WEB_IP}" to any port 9000 comment 'Whisper STT — WEB-01 only' > /dev/null
ufw reload > /dev/null
print_ok "Port 9000: ${WEB_IP} (WEB-01) only"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}WSS-01 Setup Complete${NC}                                             ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Model:${NC} ${WHISPER_MODEL} | ${WHITE}Port:${NC} 9000 | ${WHITE}Compute:${NC} ${COMPUTE_TYPE}      ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Monitor first boot:${NC} docker logs -f praesidium-whisper      ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
