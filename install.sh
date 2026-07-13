#!/usr/bin/env bash
# install.sh - one-shot installer for pi-sensor-logger on Raspberry Pi OS.
# Run this on the Pi (NOT cross-platform). Re-running is safe.

set -euo pipefail

APP_DIR="/opt/pi-sensor-logger"
CFG_DIR="/etc/pi-sensor-logger"
LOG_FILE="/var/log/pi-sensor-logger.log"
SERVICE_NAME="pi-sensor-logger"
RUN_USER="${SUDO_USER:-pi}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo ./install.sh"
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip \
  i2c-tools libgpiod2 wireless-tools

echo "==> Enabling I2C interface (idempotent)"
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
fi

echo "==> Creating ${APP_DIR}"
install -d -o "${RUN_USER}" -g "${RUN_USER}" "${APP_DIR}"
install -d -m 0755 "${CFG_DIR}"

echo "==> Copying application files"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 -o "${RUN_USER}" -g "${RUN_USER}" \
  "${SRC_DIR}/main.py" "${SRC_DIR}/sensors.py" "${SRC_DIR}/uploader.py" \
  "${SRC_DIR}/requirements.txt" \
  "${APP_DIR}/"

if [[ ! -f "${CFG_DIR}/config.yaml" ]]; then
  echo "==> Installing default config (edit it before starting the service!)"
  install -m 0640 "${SRC_DIR}/config.yaml" "${CFG_DIR}/config.yaml"
  chown root:"${RUN_USER}" "${CFG_DIR}/config.yaml"
else
  echo "==> Existing ${CFG_DIR}/config.yaml left in place"
fi

echo "==> Creating log file"
touch "${LOG_FILE}"
chown "${RUN_USER}":"${RUN_USER}" "${LOG_FILE}"
chmod 0644 "${LOG_FILE}"

echo "==> Setting up Python virtual environment"
sudo -u "${RUN_USER}" python3 -m venv "${APP_DIR}/venv"
sudo -u "${RUN_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel
sudo -u "${RUN_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Installing systemd service"
install -m 0644 "${SRC_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
# Patch the User= line if a non-default user was used
if [[ "${RUN_USER}" != "pi" ]]; then
  sed -i "s/^User=pi$/User=${RUN_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
  sed -i "s/^Group=pi$/Group=${RUN_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
fi

# Make sure the user can read I2C (usually the i2c group exists after enabling I2C)
if getent group i2c >/dev/null; then
  usermod -aG i2c "${RUN_USER}" || true
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

cat <<EOF

==============================================================
Install complete.

  1. Edit ${CFG_DIR}/config.yaml and set:
       - upload.url
       - upload.auth.token  (or whichever auth fields you need)
       - upload.device_id
       - wifi.expected_ssid (optional, for logging only)

  2. Start the service:
       sudo systemctl start ${SERVICE_NAME}

  3. Follow the logs:
       sudo journalctl -u ${SERVICE_NAME} -f
       tail -f ${LOG_FILE}

  4. Test once without the service running:
       sudo -u ${RUN_USER} ${APP_DIR}/venv/bin/python \\
            ${APP_DIR}/main.py --config ${CFG_DIR}/config.yaml --once

  NOTE: If you just added ${RUN_USER} to the i2c group, that user
        needs to log out and back in (or reboot) before the
        service can talk to the sensors.
==============================================================
EOF
