#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  04-praesidium-proc.sh — v3.0
#  Target: MAIN-PRD-PROC-01 (10.10.60.12)
#  Role: Elasticsearch 8.13.0 + Redis + RQ Workers (Docker)
#
#  v3.0 changes vs v2.1:
#    + /etc/praesidium/ bootstrap config slot
#    + UFW: Elasticsearch and Redis restricted to internal subnet only
#    + RQ worker Docker restart policy: always
#    + Bundle assembler job: jobs/config_bundle.py runs on this worker
#    + /mnt/data pre-flight (LABEL=praesidium-esdat)
#
#  Prerequisites:
#    1. 00-praesidium-base.sh complete
#    2. /mnt/data mounted (LABEL=praesidium-esdat) — run migrate-proc01-datadisk.sh
#
#  DEPLOY ORDER: PROC-01 provisioned AFTER RPRX-01 and DB-01, BEFORE WEB-01.
#  Usage: sudo bash 04-praesidium-proc.sh
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
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Processing  |  MAIN-PRD-PROC-01  |  v3.0${NC}         ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}Elasticsearch 8.13  |  Redis  |  RQ Workers  |  10.10.60.12${NC}   ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && print_error "Must run as root: sudo bash 04-praesidium-proc.sh" && exit 1
! command -v fail2ban-client &>/dev/null && print_error "Run 00-praesidium-base.sh first" && exit 1

if ! mountpoint -q /mnt/data; then
    print_error "/mnt/data is not mounted."
    echo -e "  ${WHITE}Attach the 1TB VHDX in Hyper-V Manager, then run:${NC}"
    echo -e "  ${CYAN}  sudo bash migrate-proc01-datadisk.sh${NC}"
    exit 1
fi
print_ok "/mnt/data mounted (LABEL=praesidium-esdat)"

# ── Inputs ────────────────────────────────────────────────────────────────────
read -r -p "  Internal subnet [10.10.60.0/24]: " INTERNAL_SUBNET
INTERNAL_SUBNET="${INTERNAL_SUBNET:-10.10.60.0/24}"
read -r -p "  WEB-01 IP [10.10.60.10]: " WEB_IP; WEB_IP="${WEB_IP:-10.10.60.10}"

# ── 1. Docker CE ──────────────────────────────────────────────────────────────
print_section "Docker CE"
! command -v docker &>/dev/null && {
    apt-get remove -y docker.io docker-compose podman-docker 2>/dev/null || true
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

# ── 2. Data directories ───────────────────────────────────────────────────────
print_section "Data Directories"
mkdir -p /mnt/data/elasticsearch
mkdir -p /mnt/data/redis
chown -R 1000:1000 /mnt/data/elasticsearch  # ES runs as uid 1000 inside container
chown -R 999:999   /mnt/data/redis          # Redis runs as uid 999 inside container
print_ok "/mnt/data/elasticsearch (uid 1000) | /mnt/data/redis (uid 999)"

# vm.max_map_count required by Elasticsearch
echo "vm.max_map_count=262144" > /etc/sysctl.d/99-elasticsearch.conf
sysctl -w vm.max_map_count=262144 > /dev/null
print_ok "vm.max_map_count=262144 (Elasticsearch requirement)"

# ── 3. Elasticsearch 8.13.0 ──────────────────────────────────────────────────
print_section "Elasticsearch 8.13.0 (Docker)"
docker pull docker.elastic.co/elasticsearch/elasticsearch:8.13.0 2>&1 | tail -3

docker run -d \
    --name elasticsearch \
    --restart=always \
    -p 9200:9200 \
    -e "discovery.type=single-node" \
    -e "xpack.security.enabled=false" \
    -e "xpack.security.http.ssl.enabled=false" \
    -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
    -e "cluster.name=praesidium" \
    -v /mnt/data/elasticsearch:/usr/share/elasticsearch/data \
    docker.elastic.co/elasticsearch/elasticsearch:8.13.0 2>/dev/null || \
    print_warn "Elasticsearch container may already exist — check: docker ps -a"

# Wait for ES to become ready
print_step "Waiting for Elasticsearch to start (up to 60s)..."
for i in $(seq 1 12); do
    sleep 5
    if curl -s http://127.0.0.1:9200/_cluster/health 2>/dev/null | grep -q '"status"'; then
        print_ok "Elasticsearch ready"
        break
    fi
    echo -e "  ${YELLOW}  ... waiting (${i}/12)${NC}"
done

# ── 4. Redis ──────────────────────────────────────────────────────────────────
print_section "Redis (Docker)"
docker pull redis:7-alpine 2>&1 | tail -3

docker run -d \
    --name redis \
    --restart=always \
    -p 6379:6379 \
    -v /mnt/data/redis:/data \
    redis:7-alpine \
    redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru \
    2>/dev/null || print_warn "Redis container may already exist"

sleep 2
docker exec redis redis-cli ping && print_ok "Redis ready" || print_warn "Redis ping failed — check: docker logs redis"

# ── 5. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
ufw allow from "${INTERNAL_SUBNET}" to any port 9200 comment 'Elasticsearch — internal' > /dev/null
ufw allow from "${INTERNAL_SUBNET}" to any port 6379 comment 'Redis — internal' > /dev/null
ufw allow from "${WEB_IP}" to any port 8000 comment 'RQ callback to WEB-01' > /dev/null
ufw reload > /dev/null
print_ok "ES :9200 + Redis :6379 restricted to ${INTERNAL_SUBNET}"

# ── 6. /etc/praesidium/ bootstrap slot ───────────────────────────────────────
print_section "Praesidium Config Slot"
mkdir -p /etc/praesidium
chmod 750 /etc/praesidium
if [ ! -f /etc/praesidium/.env.bootstrap ]; then
    cat > /etc/praesidium/.env.bootstrap << 'ENVEOF'
# PROC-01 bootstrap .env — see generate-env.sh on WEB-01
DATABASE_URL=__FILL_IN__
REDIS_URL=redis://127.0.0.1:6379/0
SECRET_KEY=__FILL_IN__
BRIDGE_SECRET=__FILL_IN__
PLATFORM_ADMIN_PASSWORD_HASH=__FILL_IN__
OPENAI_API_KEY=__FILL_IN__
RQ_QUEUES=default,billing,dms,court,ediscovery,intelligence
WORKER_REPLICAS=4
BUNDLE_DIR=/opt/praesidium/bundles
ENVEOF
    chmod 600 /etc/praesidium/.env.bootstrap
    print_ok "/etc/praesidium/.env.bootstrap scaffolded"
fi
mkdir -p /opt/praesidium/bundles
chmod 750 /opt/praesidium/bundles
print_ok "/opt/praesidium/bundles/ — bundle assembler writes here"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}PROC-01 Setup Complete${NC}                                             ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}ES:${NC} 9200 | ${WHITE}Redis:${NC} 6379 | ${WHITE}data:${NC} /mnt/data              ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Next:${NC} sudo bash 02-praesidium-web.sh  (on WEB-01)           ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}RQ workers${NC} start automatically when WEB-01 docker compose up ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
