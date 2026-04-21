#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Praesidium eDiscovery — Pre-Flight Check
# Run this on BOTH WEB-01 and PROC-01 before deployment.
# It will identify every known issue from DMS/Billing/Dashboard
# deployments before they bite you.
# ═══════════════════════════════════════════════════════════════

set -euo pipefail
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo -e "  ${GRN}✓${NC} $desc"; ((PASS++))
  else
    echo -e "  ${RED}✗${NC} $desc"; ((FAIL++))
  fi
}
warn_check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo -e "  ${GRN}✓${NC} $desc"; ((PASS++))
  else
    echo -e "  ${YLW}⚠${NC} $desc"; ((WARN++))
  fi
}

echo "═══════════════════════════════════════════════════════════"
echo " Praesidium eDiscovery Pre-Flight Check"
echo " Running on: $(hostname) ($(hostname -I | awk '{print $1}'))"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ─── 1. .env file ────────────────────────────────────────────
echo "1. ENVIRONMENT FILE"

if [ -f /opt/praesidium/.env ]; then
  echo -e "  ${GRN}✓${NC} .env exists at /opt/praesidium/.env"; ((PASS++))
  # Lesson: values must be single-quoted
  UNQUOTED=$(grep -cP "^[A-Z_]+=(?!')[^\s]" /opt/praesidium/.env 2>/dev/null || echo 0)
  if [ "$UNQUOTED" -gt 0 ]; then
    echo -e "  ${RED}✗${NC} $UNQUOTED unquoted values in .env — will cause bash parse errors"; ((FAIL++))
  else
    echo -e "  ${GRN}✓${NC} All .env values appear properly quoted"; ((PASS++))
  fi
  # Check for REPLACE placeholders
  PLACEHOLDERS=$(grep -c 'REPLACE\|<.*>' /opt/praesidium/.env 2>/dev/null || echo 0)
  if [ "$PLACEHOLDERS" -gt 0 ]; then
    echo -e "  ${RED}✗${NC} $PLACEHOLDERS unfilled placeholders in .env"; ((FAIL++))
  else
    echo -e "  ${GRN}✓${NC} No unfilled placeholders"; ((PASS++))
  fi
  # Source it to check individual vars
  set -a; source /opt/praesidium/.env 2>/dev/null || true; set +a
else
  echo -e "  ${RED}✗${NC} .env NOT FOUND at /opt/praesidium/.env"; ((FAIL++))
  echo "     Cannot continue without .env. Copy from master and fill in values."
  exit 1
fi
echo ""

# ─── 2. Database connectivity ────────────────────────────────
echo "2. DATABASE (MariaDB on 10.10.60.11)"
check "Can reach DB port 3306" bash -c "echo | nc -w3 10.10.60.11 3306"
if command -v mysql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
  # Extract host/user/pass/db from DATABASE_URL
  DB_HOST=$(echo "$DATABASE_URL" | grep -oP '@\K[^:]+')
  DB_NAME=$(echo "$DATABASE_URL" | grep -oP '/\K[^?]+$')
  warn_check "ediscovery_collections table exists" \
    bash -c "mysql -h $DB_HOST -u praesidium -p'${CIFS_PASSWORD:-}' $DB_NAME -e 'DESC ediscovery_collections' 2>/dev/null"
fi
echo ""

# ─── 3. Redis ────────────────────────────────────────────────
echo "3. REDIS (10.10.60.12:6379)"
check "Can reach Redis port" bash -c "echo | nc -w3 10.10.60.12 6379"
warn_check "redis-cli PING" bash -c "redis-cli -h 10.10.60.12 ping 2>/dev/null | grep -q PONG"
echo ""

# ─── 4. LDAPS ────────────────────────────────────────────────
echo "4. LDAPS AUTHENTICATION"
LDAP_IP="10.10.10.21"
# Lesson: LDAP_URL may use hostname that doesn't resolve from VLAN 60
if [ -n "${LDAP_URL:-}" ]; then
  LDAP_HOST=$(echo "$LDAP_URL" | grep -oP '://\K[^:]+')
  echo "   LDAP_URL=$LDAP_URL"
  if echo "$LDAP_HOST" | grep -qP '^\d+\.\d+\.\d+\.\d+$'; then
    echo -e "  ${GRN}✓${NC} LDAP_URL uses IP address (recommended)"; ((PASS++))
    LDAP_IP="$LDAP_HOST"
  else
    echo -e "  ${YLW}⚠${NC} LDAP_URL uses hostname '$LDAP_HOST' — may not resolve from VLAN 60"; ((WARN++))
    if host "$LDAP_HOST" >/dev/null 2>&1; then
      RESOLVED_IP=$(host "$LDAP_HOST" | head -1 | awk '{print $NF}')
      echo "     Resolves to: $RESOLVED_IP"
    else
      echo -e "  ${RED}✗${NC} Cannot resolve '$LDAP_HOST' — LDAP auth will fail!"; ((FAIL++))
      echo "     FIX: Use LDAP_URL='ldaps://10.10.10.21:636' (IP) instead of hostname"
      echo "     OR: Add '$LDAP_HOST' to /etc/hosts: echo '10.10.10.21 $LDAP_HOST' >> /etc/hosts"
    fi
  fi
fi
# Also check LDAP_SERVER (some configs have both)
if [ -n "${LDAP_SERVER:-}" ]; then
  echo "   LDAP_SERVER=$LDAP_SERVER (IP-based fallback)"
fi
check "Can reach LDAPS port 636" bash -c "echo | nc -w3 $LDAP_IP 636"
# Check LDAP_BIND_DN format
if [ -n "${LDAP_BIND_DN:-}" ]; then
  if echo "$LDAP_BIND_DN" | grep -q "CN="; then
    echo -e "  ${GRN}✓${NC} LDAP_BIND_DN is full DN format"; ((PASS++))
  else
    echo -e "  ${YLW}⚠${NC} LDAP_BIND_DN='$LDAP_BIND_DN' is short form — may need full DN"; ((WARN++))
    echo "     Full DN example: CN=svc-praesidium,OU=Service Accounts,DC=hjmmlegal,DC=com"
    echo "     Some LDAP servers accept short form, some don't. Test with ldapwhoami."
  fi
fi
# Check CA cert path
if [ -n "${LDAP_CA_CERT_PATH:-}" ] && [ -f "${LDAP_CA_CERT_PATH}" ]; then
  echo -e "  ${GRN}✓${NC} LDAP CA cert exists at $LDAP_CA_CERT_PATH"; ((PASS++))
elif [ "${LDAP_VERIFY_CERT:-}" = "true" ]; then
  echo -e "  ${YLW}⚠${NC} LDAP_VERIFY_CERT=true but no CA cert path set — cert validation may fail"; ((WARN++))
  echo "     Set LDAP_CA_CERT_PATH or use LDAP_VERIFY_CERT='false' for testing"
fi
echo ""

# ─── 5. File paths and mounts ────────────────────────────────
echo "5. FILE PATHS & CIFS MOUNTS"
# Lesson: mount paths differ across docs — verify what's actually mounted
echo "   Checking actual mounts..."
for MP in /mnt/clients /mnt/docsend /mnt/matters /mnt/ediscovery; do
  if mountpoint -q "$MP" 2>/dev/null; then
    SIZE=$(df -h "$MP" 2>/dev/null | tail -1 | awk '{print $2}')
    echo -e "  ${GRN}✓${NC} $MP is mounted ($SIZE total)"; ((PASS++))
  elif [ -d "$MP" ]; then
    echo -e "  ${YLW}⚠${NC} $MP directory exists but is NOT mounted"; ((WARN++))
  else
    # Only fail for ediscovery mount — others are DMS
    if [ "$MP" = "/mnt/ediscovery" ]; then
      echo -e "  ${RED}✗${NC} $MP does not exist — eDiscovery storage missing!"; ((FAIL++))
      echo "     FIX: sudo mkdir -p /mnt/ediscovery && mount per install guide Section 3.7"
    else
      echo -e "  ${YLW}⚠${NC} $MP does not exist (DMS mount — not required for eDiscovery)"; ((WARN++))
    fi
  fi
done

# Check CIFS bridge port
CIFS_PORT=$(echo "${CIFS_URL:-}" | grep -oP ':\K\d+$' || echo "unknown")
echo "   CIFS_URL=${CIFS_URL:-not set} (port: $CIFS_PORT)"
# Lesson: port mismatch between docs (8080 vs 8001)
if [ "$CIFS_PORT" != "unknown" ]; then
  check "File bridge reachable on port $CIFS_PORT" \
    bash -c "curl -sf http://10.10.60.13:$CIFS_PORT/health >/dev/null 2>&1"
fi
echo ""

# ─── 6. Platform code location ───────────────────────────────
echo "6. PLATFORM CODE LOCATION"
# Lesson: some chats deploy to /opt/praesidium/, others to /opt/praesidium/praesidium-platform/
for LOC in \
  "/opt/praesidium/praesidium-platform/core/audit.py" \
  "/opt/praesidium/core/audit.py" \
  "/opt/praesidium/praesidium-platform/modules/" \
  "/opt/praesidium/modules/"; do
  if [ -e "$LOC" ]; then
    echo -e "  ${GRN}✓${NC} Found: $LOC"
  fi
done
# Determine actual code root
if [ -f "/opt/praesidium/praesidium-platform/core/audit.py" ]; then
  CODE_ROOT="/opt/praesidium/praesidium-platform"
  echo "   → Code root: $CODE_ROOT (praesidium-platform subdirectory)"
elif [ -f "/opt/praesidium/core/audit.py" ]; then
  CODE_ROOT="/opt/praesidium"
  echo "   → Code root: $CODE_ROOT (flat layout)"
else
  echo -e "  ${RED}✗${NC} Cannot find core/audit.py — Chat 0 foundation may not be deployed"; ((FAIL++))
  CODE_ROOT="/opt/praesidium"
fi

# Check write_audit signature
if [ -f "$CODE_ROOT/core/audit.py" ]; then
  if grep -q "def write_audit(session" "$CODE_ROOT/core/audit.py" 2>/dev/null; then
    echo -e "  ${GRN}✓${NC} write_audit() uses positional session arg (expected)"; ((PASS++))
  elif grep -q "def write_audit" "$CODE_ROOT/core/audit.py" 2>/dev/null; then
    ACTUAL_SIG=$(grep "def write_audit" "$CODE_ROOT/core/audit.py" | head -1)
    echo -e "  ${YLW}⚠${NC} write_audit() signature: $ACTUAL_SIG"; ((WARN++))
    echo "     eDiscovery uses try/except around all audit calls — won't break, but check."
  fi
fi
echo ""

# ─── 7. System packages (PROC-01 checks) ────────────────────
echo "7. SYSTEM PACKAGES"
for CMD in tesseract antiword python3 pip; do
  warn_check "$CMD installed" command -v $CMD
done
warn_check "poppler-utils (pdftotext)" command -v pdftotext
warn_check "Pillow (Python)" python3 -c "import PIL"
warn_check "pdfplumber (Python)" python3 -c "import pdfplumber"
echo ""

# ─── 8. Meilisearch ──────────────────────────────────────────
echo "8. MEILISEARCH (10.10.60.12:7700)"
check "Meilisearch health" bash -c "curl -sf http://10.10.60.12:7700/health | grep -q available"
echo ""

# ─── Summary ─────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════"
echo -e " Results: ${GRN}$PASS passed${NC}  ${RED}$FAIL failed${NC}  ${YLW}$WARN warnings${NC}"
if [ $FAIL -gt 0 ]; then
  echo -e " ${RED}FIX ALL FAILURES before deploying eDiscovery.${NC}"
  exit 1
elif [ $WARN -gt 0 ]; then
  echo -e " ${YLW}Review warnings — deployment may work but verify.${NC}"
else
  echo -e " ${GRN}All checks passed — safe to deploy.${NC}"
fi
echo "═══════════════════════════════════════════════════════════"
