#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  PRAESIDIUM — Legal Practice Intelligence Platform
#  01-praesidium-rprx.sh — v3.0
#  Target: MAIN-DMZ-RPRX-01 (10.10.40.50)
#  Role: Nginx reverse proxy + SSL termination + RPRX sidecar
#
#  What it does:
#    1. Nginx + Certbot
#    2. UFW: open 80, 443 (public); 8099 (sidecar, internal only)
#    3. /etc/praesidium/ slot_map.conf + failover config
#    4. Python 3.12 venv for rprx_sidecar.py
#    5. systemd service: praesidium-sidecar
#    6. /etc/praesidium/.env.bootstrap slot (bootstrap 6 values)
#
#  v3.0 changes vs v2.0:
#    + sidecar venv setup + systemd service (was manual in v2.0)
#    + slot_map.conf scaffold
#    + /etc/praesidium/ bootstrap config slot
#    + 8099 sidecar port restricted to internal network (10.10.x.x)
#    + python-pam + six dependency noted
#
#  DEPLOY ORDER: RPRX-01 ALWAYS first — SSL termination before anything exposed.
#  Usage: sudo bash 01-praesidium-rprx.sh
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
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${BOLD}${WHITE}PRAESIDIUM  |  Reverse Proxy  |  MAIN-DMZ-RPRX-01  |  v3.0${NC}       ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}   ${CYAN}Nginx  |  Certbot  |  RPRX Sidecar  |  10.10.40.50${NC}             ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && print_error "Must run as root: sudo bash 01-praesidium-rprx.sh" && exit 1
! command -v fail2ban-client &>/dev/null && print_error "Run 00-praesidium-base.sh first" && exit 1

# ── Inputs ────────────────────────────────────────────────────────────────────
read -r -p "  Domain (e.g. hjmmlegal.com): " DOMAIN
read -r -p "  WEB-01 backend IP [10.10.60.10]: " WEB_IP; WEB_IP="${WEB_IP:-10.10.60.10}"
read -r -p "  Internal subnet for sidecar [10.10.0.0/16]: " INTERNAL_NET
INTERNAL_NET="${INTERNAL_NET:-10.10.0.0/16}"

# ── 1. Nginx + Certbot ───────────────────────────────────────────────────────
print_section "Nginx and Certbot"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx certbot python3-certbot-nginx
systemctl enable nginx > /dev/null 2>&1
systemctl start nginx
print_ok "Nginx installed"

# ── 2. UFW rules ─────────────────────────────────────────────────────────────
print_section "UFW Rules"
ufw allow 80/tcp comment 'HTTP → redirect to HTTPS' > /dev/null
ufw allow 443/tcp comment 'HTTPS — public' > /dev/null
# Sidecar port 8099 — internal subnet only
ufw allow from "${INTERNAL_NET}" to any port 8099 comment 'RPRX sidecar — internal only' > /dev/null
ufw reload > /dev/null
print_ok "UFW: 80, 443 public | 8099 restricted to ${INTERNAL_NET}"

# ── 3. /etc/praesidium/ — slot_map.conf ─────────────────────────────────────
print_section "Praesidium Config Slot"
mkdir -p /etc/praesidium
chmod 750 /etc/praesidium

# slot_map.conf — managed by rprx_sidecar; scaffold only if not present
if [ ! -f /etc/praesidium/slot_map.conf ]; then
    cat > /etc/praesidium/slot_map.conf << EOF
# PRAESIDIUM RPRX SLOT MAP
# Managed by praesidium-sidecar — do not edit manually.
# Format: default_server=HOST:PORT
default_server=${WEB_IP}:8000
EOF
    chmod 640 /etc/praesidium/slot_map.conf
    print_ok "/etc/praesidium/slot_map.conf scaffolded"
else
    print_ok "/etc/praesidium/slot_map.conf already exists — skipped"
fi

# ── 4. Nginx site config ──────────────────────────────────────────────────────
print_section "Nginx Site Configuration"

# Passive failover: try primary, fall back to maintenance page
cat > /etc/nginx/sites-available/praesidium << NGINXEOF
# PRAESIDIUM — Nginx site config v3.0
# Domain: ${DOMAIN}
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

limit_req_zone \$binary_remote_addr zone=praesidium:10m rate=30r/m;

upstream praesidium_app {
    server ${WEB_IP}:8000 max_fails=3 fail_timeout=30s;
    keepalive 32;
}

# HTTP → HTTPS redirect
server {
    listen 80;
    server_name *.${DOMAIN} ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

# HTTPS
server {
    listen 443 ssl http2;
    server_name *.${DOMAIN} ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options SAMEORIGIN;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";

    limit_req zone=praesidium burst=20 nodelay;

    # Passive failover — serve maintenance page if WEB-01 is down
    error_page 502 503 504 /maintenance.html;
    location = /maintenance.html {
        root /var/www/praesidium;
        internal;
    }

    location / {
        proxy_pass http://praesidium_app;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
        proxy_next_upstream error timeout http_502 http_503 http_504;
    }
}
NGINXEOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/praesidium /etc/nginx/sites-enabled/

# Maintenance page
mkdir -p /var/www/praesidium
cat > /var/www/praesidium/maintenance.html << 'HTMLEOF'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Praesidium — Temporarily Unavailable</title>
  <style>
    body { font-family: Arial, sans-serif; background: #1a1a2e; color: #eee;
           display: flex; align-items: center; justify-content: center;
           height: 100vh; margin: 0; }
    .box { text-align: center; max-width: 420px; }
    h1 { font-size: 1.4rem; margin-bottom: 0.5rem; color: #7eb8d4; }
    p  { color: #aaa; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Praesidium</h1>
    <p>The platform is temporarily unavailable.</p>
    <p>Please try again in a moment.</p>
  </div>
</body>
</html>
HTMLEOF

nginx -t && systemctl reload nginx
print_ok "Nginx configured: ${DOMAIN} → ${WEB_IP}:8000 | passive failover active"

# ── 5. Python venv for RPRX sidecar ─────────────────────────────────────────
print_section "RPRX Sidecar Python Environment"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-dev python3-pip python3-venv \
    libpam-dev pkg-config
print_ok "Python dev packages installed"

SIDECAR_VENV="/opt/praesidium/sidecar-venv"
python3 -m venv "${SIDECAR_VENV}"
"${SIDECAR_VENV}/bin/pip" install --quiet --upgrade pip
# python-pam 2.0.2 requires 'six' as an unlisted dependency (C0d lesson)
"${SIDECAR_VENV}/bin/pip" install --quiet \
    fastapi uvicorn[standard] httpx psutil python-pam six pydantic
print_ok "Sidecar venv: ${SIDECAR_VENV}"
print_ok "Installed: fastapi uvicorn httpx psutil python-pam six pydantic"
print_warn "python-pam requires 'six' — already installed above (Module 8 lesson)"

# ── 6. Sidecar deployment ────────────────────────────────────────────────────
print_section "RPRX Sidecar Deployment"
mkdir -p /opt/praesidium
print_warn "Deploy rprx_sidecar.py manually:"
echo ""
echo -e "  ${CYAN}# From your workstation (Bitvise SFTP):${NC}"
echo -e "  ${WHITE}Upload rprx_sidecar.py → /home/dmholmgren/rprx_sidecar.py${NC}"
echo ""
echo -e "  ${CYAN}# On RPRX-01:${NC}"
echo -e "  ${WHITE}sudo mv ~/rprx_sidecar.py /opt/praesidium/rprx_sidecar.py${NC}"
echo -e "  ${WHITE}sudo chown praesidium:praesidium /opt/praesidium/rprx_sidecar.py${NC}"
echo -e "  ${WHITE}sudo chmod 640 /opt/praesidium/rprx_sidecar.py${NC}"
echo ""

# ── 7. systemd service for sidecar ──────────────────────────────────────────
print_section "systemd: praesidium-sidecar"
cat > /etc/systemd/system/praesidium-sidecar.service << UNITEOF
[Unit]
Description=Praesidium RPRX Glass-Break Sidecar
After=network.target nginx.service
Wants=network.target

[Service]
Type=simple
User=praesidium
Group=praesidium
WorkingDirectory=/opt/praesidium
ExecStart=${SIDECAR_VENV}/bin/uvicorn rprx_sidecar:app --host 0.0.0.0 --port 8099 --workers 1
Restart=always
RestartSec=5
StandardOutput=append:/var/log/praesidium/sidecar.log
StandardError=append:/var/log/praesidium/sidecar.err
EnvironmentFile=-/etc/praesidium/.env.bootstrap
# Harden
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/praesidium /var/log/praesidium /etc/praesidium /etc/nginx

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable praesidium-sidecar > /dev/null 2>&1
print_ok "praesidium-sidecar.service enabled"
print_warn "Service will NOT start until rprx_sidecar.py is deployed to /opt/praesidium/"
echo ""
echo -e "  ${WHITE}After deploying rprx_sidecar.py:${NC}"
echo -e "  ${CYAN}  sudo systemctl start praesidium-sidecar${NC}"
echo -e "  ${CYAN}  sudo systemctl status praesidium-sidecar${NC}"
echo -e "  ${CYAN}  curl http://127.0.0.1:8099/health${NC}"

# ── 8. SSL certificate ───────────────────────────────────────────────────────
print_section "SSL Certificate (wildcard — DNS-01 challenge)"
print_warn "You will need to add a TXT record at your DNS registrar"
read -r -p "  Obtain SSL certificate now? [y/N]: " SSL_NOW
if echo "$SSL_NOW" | grep -qi "^y"; then
    certbot certonly --manual --preferred-challenges dns \
        -d "*.${DOMAIN}" -d "${DOMAIN}"
    nginx -t && systemctl reload nginx
    print_ok "SSL certificate obtained"
else
    print_warn "Run when ready:"
    echo -e "  ${CYAN}sudo certbot certonly --manual --preferred-challenges dns -d '*.${DOMAIN}' -d '${DOMAIN}'${NC}"
    echo -e "  ${CYAN}sudo nginx -t && sudo systemctl reload nginx${NC}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}  ${GREEN}${BOLD}RPRX Setup Complete${NC}  |  ${DOMAIN} → ${WEB_IP}:8000         ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}  ${WHITE}Next steps:${NC}                                               ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    1. Deploy rprx_sidecar.py → /opt/praesidium/              ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    2. sudo systemctl start praesidium-sidecar                ${BLUE}║${NC}"
echo -e "${BLUE}║${NC}    3. sudo bash 03-praesidium-db.sh   (on DB-01)             ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""
