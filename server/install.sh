#!/usr/bin/env bash
# install.sh - install pi-sensor-server on a Debian/Ubuntu VPS running
#              Apache 2.4 with mod_wsgi (daemon mode) + MySQL/MariaDB.
#
# Run as root. Assumes you ALREADY have a MySQL (or MariaDB) server running
# locally with a database + user prepared. See README.md for the SQL.

set -euo pipefail

APP_DIR="/opt/pi-sensor-server"
CFG_DIR="/etc/pi-sensor-server"
SVC_USER="sensor-server"
SITE_NAME="pi-sensor-server"
APACHE_SITE="/etc/apache2/sites-available/${SITE_NAME}.conf"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo ./install.sh"
  exit 1
fi

echo "==> Installing system packages"
apt-get update
apt-get install -y \
  apache2 libapache2-mod-wsgi-py3 \
  python3 python3-venv python3-pip \
  default-mysql-client

echo "==> Enabling required Apache modules"
a2enmod wsgi headers rewrite ssl >/dev/null

echo "==> Creating service user"
if ! id "${SVC_USER}" >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${SVC_USER}"
fi

echo "==> Creating directories"
install -d -o "${SVC_USER}" -g "${SVC_USER}" "${APP_DIR}"
install -d -m 0750 -o root  -g "${SVC_USER}" "${CFG_DIR}"

echo "==> Copying app files"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 -o "${SVC_USER}" -g "${SVC_USER}" \
  "${SRC_DIR}/app.py" "${SRC_DIR}/schema.sql" "${SRC_DIR}/requirements.txt" \
  "${APP_DIR}/"
install -m 0644 -o "${SVC_USER}" -g "${SVC_USER}" \
  "${SRC_DIR}/app.wsgi" "${APP_DIR}/"
install -d -o "${SVC_USER}" -g "${SVC_USER}" "${APP_DIR}/templates"
install -m 0644 -o "${SVC_USER}" -g "${SVC_USER}" \
  "${SRC_DIR}/templates/dashboard.html" "${APP_DIR}/templates/"

if [[ ! -f "${CFG_DIR}/env" ]]; then
  echo "==> Installing default env file"
  install -m 0640 -o root -g "${SVC_USER}" "${SRC_DIR}/.env.example" "${CFG_DIR}/env"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s|REPLACE_WITH_LONG_RANDOM_TOKEN|${TOKEN}|" "${CFG_DIR}/env"
  echo
  echo "    +----------------------------------------------------------------+"
  echo "    | A random API token was generated:                              |"
  echo "    |   ${TOKEN}"
  echo "    | Paste this into the Pi's config.yaml under upload.auth.token   |"
  echo "    +----------------------------------------------------------------+"
  echo
  echo "    >>> You MUST also edit ${CFG_DIR}/env and set MYSQL_PASSWORD"
  echo "    >>> (and adjust MYSQL_SOCKET / MYSQL_DB / MYSQL_USER if needed)"
  echo "    >>> before starting Apache."
  echo
else
  echo "==> Existing ${CFG_DIR}/env left in place"
fi

echo "==> Creating Python venv and installing deps"
sudo -u "${SVC_USER}" python3 -m venv "${APP_DIR}/venv"
sudo -u "${SVC_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip wheel
sudo -u "${SVC_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Installing Apache VirtualHost"
install -m 0644 "${SRC_DIR}/${SITE_NAME}.conf" "${APACHE_SITE}"

if grep -q "sensors.example.com" "${APACHE_SITE}"; then
  echo "    NOTE: ${APACHE_SITE} still has the placeholder hostname"
  echo "          'sensors.example.com'. Edit it to your real hostname"
  echo "          BEFORE running 'sudo certbot --apache'."
fi

a2dissite 000-default >/dev/null 2>&1 || true
a2ensite "${SITE_NAME}" >/dev/null

echo "==> Validating Apache config"
apachectl configtest

echo "==> NOT reloading Apache yet -- ${CFG_DIR}/env still needs MYSQL_PASSWORD"
echo "    When you're done editing, run: sudo systemctl reload apache2"

cat <<EOF

==============================================================
Install complete. Remaining steps:

  1. Make sure MySQL/MariaDB is running and that you have created:
       - a database (default name: pi_sensors)
       - a user with privileges on it (default: sensor_writer)
     The required SQL is in README.md ("MySQL setup").

  2. Edit ${CFG_DIR}/env and set:
       MYSQL_SOCKET   (default already set for Debian/Ubuntu)
       MYSQL_DB
       MYSQL_USER
       MYSQL_PASSWORD

  3. Reload Apache:
       sudo systemctl reload apache2
       sudo tail -F /var/log/apache2/pi-sensor-server-error.log

  4. (Optional) edit ${APACHE_SITE} to set ServerName, then:
       sudo certbot --apache -d sensors.example.com

  5. Sanity test:
       curl -s http://127.0.0.1/health
       curl -X POST http://127.0.0.1/api/readings \\
         -H "Authorization: Bearer ${TOKEN:-<TOKEN>}" \\
         -H "Content-Type: application/json" \\
         -d '{"device_id":"test","temperature_f":22.1}'
==============================================================
EOF
