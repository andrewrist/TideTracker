#!/usr/bin/env bash
# deploy.sh — Download TideTracker from GitHub and deploy the web server files.
#
# Usage:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
#
# What it does:
#   1. Clones the repo (or pulls if it already exists) into REPO_DIR.
#   2. Copies server files into DEPLOY_DIR.
#   3. Applies schema.sql against MySQL (runs CREATE TABLE IF NOT EXISTS
#      and the ALTER TABLE migration — safe to run repeatedly).
#   4. Re-installs Python dependencies into the existing virtualenv.
#   5. Reloads Apache so mod_wsgi picks up the new code.
#
# Prerequisites:
#   - Run server/install.sh once first to create DEPLOY_DIR and the venv.
#   - MySQL credentials must be in /etc/pi-sensor-server/env (loaded by app.wsgi).
#     The deploy script reads the same file to run the schema migration.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/andrewrist/TideTracker.git"
REPO_BRANCH="main"
REPO_DIR="/opt/pi-sensor-server-src"   # local clone location
DEPLOY_DIR="/opt/pi-sensor-server"     # where the WSGI app runs from
VENV_DIR="$DEPLOY_DIR/venv"
ENV_FILE="/etc/pi-sensor-server/env"   # MySQL credentials (KEY=value lines)
APACHE_SERVICE="apache2"

# Files inside the repo's outputs/server/ subdirectory to deploy
DEPLOY_FILES=(
    app.py
    app.wsgi
    requirements.txt
    schema.sql
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[deploy-server] $*"; }
error() { echo "[deploy-server] ERROR: $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || error "This script must be run as root (sudo ./deploy.sh)"
}

require_cmd() {
    command -v "$1" &>/dev/null || error "'$1' not found. Install it with: apt-get install $2"
}

# Read a KEY=value env file and export the variables into this shell.
load_env() {
    local file="$1"
    [[ -f "$file" ]] || { info "WARNING: $file not found — skipping schema migration."; return 1; }
    while IFS='=' read -r key value; do
        # Skip blank lines and comments
        [[ -z "$key" || "$key" == \#* ]] && continue
        # Strip surrounding quotes if present
        value="${value%\"}"
        value="${value#\"}"
        export "$key=$value"
    done < "$file"
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root
require_cmd git git

# 1. Clone or pull
if [[ -d "$REPO_DIR/.git" ]]; then
    info "Updating existing clone at $REPO_DIR …"
    git -C "$REPO_DIR" fetch --quiet origin "$REPO_BRANCH"
    git -C "$REPO_DIR" reset --hard "origin/$REPO_BRANCH" --quiet
    info "Repo updated to $(git -C "$REPO_DIR" rev-parse --short HEAD)"
else
    info "Cloning $REPO_URL into $REPO_DIR …"
    git clone --quiet --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR"
    info "Cloned at $(git -C "$REPO_DIR" rev-parse --short HEAD)"
fi

# 2. Verify deploy directory exists
[[ -d "$DEPLOY_DIR" ]] || error "$DEPLOY_DIR does not exist. Run server/install.sh first."

# 3. Copy files
info "Copying files to $DEPLOY_DIR …"
for f in "${DEPLOY_FILES[@]}"; do
    src="$REPO_DIR/outputs/server/$f"
    [[ -f "$src" ]] || error "Expected file not found in repo: outputs/server/$f"
    cp "$src" "$DEPLOY_DIR/$f"
    info "  $f"
done

# Copy templates directory
info "  templates/"
cp -r "$REPO_DIR/outputs/server/templates" "$DEPLOY_DIR/"

# 4. Apply schema migration
if load_env "$ENV_FILE"; then
    MYSQL_SOCKET="${MYSQL_SOCKET:-/var/run/mysqld/mysqld.sock}"
    MYSQL_DB="${MYSQL_DB:-pi_sensors}"
    MYSQL_USER="${MYSQL_USER:-sensor_writer}"
    MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"

    info "Applying schema to MySQL database '$MYSQL_DB' …"
    mysql \
        --socket="$MYSQL_SOCKET" \
        --user="$MYSQL_USER" \
        --password="$MYSQL_PASSWORD" \
        --database="$MYSQL_DB" \
        < "$DEPLOY_DIR/schema.sql" \
        && info "Schema applied." \
        || info "WARNING: schema migration failed — check MySQL credentials in $ENV_FILE"
fi

# 5. Re-install dependencies
if [[ -d "$VENV_DIR" ]]; then
    info "Installing Python dependencies into $VENV_DIR …"
    "$VENV_DIR/bin/pip" install --quiet -r "$DEPLOY_DIR/requirements.txt"
else
    info "WARNING: virtualenv not found at $VENV_DIR — skipping pip install."
    info "         Run server/install.sh to create the venv, then re-run this script."
fi

# 6. Reload Apache
if command -v systemctl &>/dev/null && systemctl list-unit-files "$APACHE_SERVICE.service" &>/dev/null 2>&1; then
    info "Reloading $APACHE_SERVICE …"
    systemctl reload "$APACHE_SERVICE"
    sleep 1
    STATUS=$(systemctl is-active "$APACHE_SERVICE" 2>/dev/null || true)
    if [[ "$STATUS" == "active" ]]; then
        info "Apache is running."
    else
        info "WARNING: Apache may not be running. Check with: systemctl status $APACHE_SERVICE"
    fi
else
    info "Apache not found via systemctl — skipping reload."
fi

info "Deploy complete."
