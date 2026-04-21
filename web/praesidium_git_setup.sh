# Praesidium Git Repository Setup
# Run on WEB-01 (or wherever /opt/praesidium/ is the bind mount source)
# ============================================================

# ---- STEP 1: Clean up junk files from shell mishaps ----
cd /opt/praesidium

# Remove accidental files created by mistyped shell commands
rm -f "Accept:" "GET" "Host:" "User-Agent:" "WHERE" "END" \
      "Checking" "Verifying" "Waiting" "26.0.1" ".status_code" \
      "available" "use" "async" "tid" "op" "for" "if" "else:" \
      "=" "exporting" "naming" "resolving" "unpacking" "{name," \
      "--include=*.env*"

# ---- STEP 2: Install git (if not already installed) ----
sudo apt-get update && sudo apt-get install -y git

# ---- STEP 3: Configure git identity ----
git config --global user.name "Dennis Holmgren"
git config --global user.email "dennis@hjmmlegal.com"  # adjust as needed

# ---- STEP 4: Replace .gitignore with the comprehensive version ----
# SFTP the new .gitignore file from Claude to /opt/praesidium/.gitignore
# (or paste the contents manually with nano)

# ---- STEP 5: Initialize the repo ----
cd /opt/praesidium
git init
git add .
git status  # Review what's being tracked — should NOT include .env, venv/, __pycache__/, etc.

# ---- STEP 6: Initial commit ----
git commit -m "Initial commit: Praesidium platform as of 2026-04-21

Platform state:
- Alembic head: 0044_ai_layer_foundation
- 152 database tables
- Modules: billing, DMS, eDiscovery (M5i complete), drafting, calendar
- Widget Registry: 201 entries, Layout Registry: 14 entries
- 112,846 DMS documents, 94,448 time slips, 5,273 invoices
- Infrastructure: WEB-01, PROC-01 (8 workers), DB-01, RPRX-01, FBRG-01, WSS-01
- MCP server operational (DB + SSH tools live)"

# ---- STEP 7: Create a private GitHub repo and push ----
# On GitHub: create a new private repo called "praesidium" under your account
# Then:
git remote add origin git@github.com:YOUR_GITHUB_USERNAME/praesidium.git
git branch -M main
git push -u origin main

# ---- STEP 8 (optional): Set up SSH key for GitHub on the server ----
# If you don't have a GitHub SSH key on this server:
ssh-keygen -t ed25519 -C "dennis@hjmmlegal.com" -f ~/.ssh/id_ed25519_github
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519_github
cat ~/.ssh/id_ed25519_github.pub
# Add the public key to GitHub → Settings → SSH and GPG keys

# ---- STEP 9: MCP server as separate repo ----
cd /opt/praesidium_MCP
git init

cat > .gitignore << 'EOF'
.env
.env.bak
__pycache__/
*.py[cod]
venv/
.venv/
*.log
EOF

git add .
git commit -m "Initial commit: Praesidium MCP server

Tools: 20 (DB queries, Docker status/logs, Elasticsearch, SSH file access, nginx config)
Hosts: web-01 (10.10.60.10), proc-01 (10.10.60.12), adm-01 (10.10.60.25)
Auth: svc-mcp service account, ed25519 key"

git remote add origin git@github.com:YOUR_GITHUB_USERNAME/praesidium-mcp.git
git branch -M main
git push -u origin main
