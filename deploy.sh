#!/usr/bin/env bash
# deploy.sh — Download TideTracker from GitHub and deploy the sensor files.
#
# Usage:
#   chmod +x deploy.sh
#   sudo ./deploy.sh
#
# What it does:
#   1. Clones the repo (or pulls if it already exists) into REPO_DIR.
#   2. Copies the four sensor files into DEPLOY_DIR.
#   3. Re-installs Python dependencies into the existing virtualenv.
#   4. Restarts the pi-sensor-logger systemd service (if it is running).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/andrewrist/TideTracker.git"
REPO_BRANCH="main"
REPO_DIR="/opt/pi-sensor-logger-src"   # local clone location
DEPLOY_DIR="/opt/pi-sensor-logger"     # where the service runs from
VENV_DIR="$DEPLOY_DIR/venv"
SERVICE_NAME="pi-sensor-logger"

# Files inside the repo's main directory to deploy
DEPLOY_FILES=(
    main.py
    requirements.txt
    sensors.py
    uploader.py
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[deploy] $*"; }
error() { echo "[deploy] ERROR: $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || error "This script must be run as root (sudo ./deploy.sh)"
}

require_cmd() {
    command -v "$1" &>/dev/null || error "'$1' not found. Install it with: apt-get install $2"
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
[[ -d "$DEPLOY_DIR" ]] || error "$DEPLOY_DIR does not exist. Run install.sh first."

# 3. Copy files
info "Copying files to $DEPLOY_DIR …"
for f in "${DEPLOY_FILES[@]}"; do
    src="$REPO_DIR/$f"
    [[ -f "$src" ]] || error "Expected file not found in repo: $f"
    cp "$src" "$DEPLOY_DIR/$f"
    info "  $f"
done

# 4. Re-install dependencies
if [[ -d "$VENV_DIR" ]]; then
    info "Installing Python dependencies into $VENV_DIR …"
    "$VENV_DIR/bin/pip" install --quiet -r "$DEPLOY_DIR/requirements.txt"
else
    info "WARNING: virtualenv not found at $VENV_DIR — skipping pip install."
    info "         Run install.sh to create the venv, then re-run this script."
fi

# 5. Restart service (if it exists and systemd is available)
if command -v systemctl &>/dev/null && systemctl list-unit-files "$SERVICE_NAME.service" &>/dev/null; then
    info "Restarting $SERVICE_NAME …"
    systemctl restart "$SERVICE_NAME"
    sleep 2
    STATUS=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
    if [[ "$STATUS" == "active" ]]; then
        info "Service is running."
    else
        echo ""
        info "WARNING: service did not come up cleanly. Check logs with:"
        info "  journalctl -u $SERVICE_NAME -n 50"
    fi
else
    info "Systemd service '$SERVICE_NAME' not found — skipping restart."
fi

info "Deploy complete."
