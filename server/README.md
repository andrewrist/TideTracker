# Pi Sensor Server (Apache 2.4 + mod_wsgi + MySQL)

Flask + MySQL + Chart.js receiver and dashboard for the
[pi-sensor-logger](../README.md) running on your Raspberry Pi Zero 2 W.

The Pi POSTs JSON to `/api/readings` every minute; Apache hands the request
to a Python WSGI daemon, which writes to a local MySQL/MariaDB instance over
a Unix socket and serves a live dashboard at `/`.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/readings` | `Authorization: Bearer <token>` | Ingest one reading |
| GET  | `/api/devices`  | none | List devices + last-seen + reading count |
| GET  | `/api/readings` | none | Query readings: `?device_id=&since=&limit=` |
| GET  | `/`             | optional HTTP basic | Dashboard |
| GET  | `/health`       | none | Liveness probe |

`since` accepts ISO8601 (`2026-05-21T14:00:00Z`) or a Unix epoch seconds value.

## File layout (after install)

```
/opt/pi-sensor-server/
    app.py                # Flask app (uses mysql-connector-python)
    app.wsgi              # mod_wsgi entry point
    schema.sql
    templates/dashboard.html
    venv/                 # Python virtualenv
/etc/pi-sensor-server/
    env                   # MYSQL_*, API_TOKENS, etc.
/etc/apache2/sites-available/
    pi-sensor-server.conf
/var/log/apache2/
    pi-sensor-server-{access,error}.log
```

The readings live in the MySQL database you point the app at — not in any
file on the local filesystem.

## MySQL setup

The app expects an existing MySQL or MariaDB server with a database and a
user prepared. The `readings` table itself is auto-created by the app on
first run, so the user just needs CREATE + INDEX + SELECT + INSERT privs
on that schema.

Run this **as the MySQL root user**, replacing the password:

```sql
CREATE DATABASE IF NOT EXISTS pi_sensors
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'sensor_writer'@'localhost'
    IDENTIFIED BY 'choose-a-strong-password';

GRANT SELECT, INSERT, CREATE, INDEX
    ON pi_sensors.*
    TO 'sensor_writer'@'localhost';

FLUSH PRIVILEGES;
```

If you want to use a different database/user name, just update the
matching env vars below.

## Configuration

All settings come from environment variables in `/etc/pi-sensor-server/env`,
which `app.wsgi` loads into `os.environ` before importing Flask:

| Var | Required | Default | Purpose |
| --- | --- | --- | --- |
| `MYSQL_SOCKET` | no | `/var/run/mysqld/mysqld.sock` | Path to mysqld.sock |
| `MYSQL_DB` | no | `pi_sensors` | Database name |
| `MYSQL_USER` | no | `sensor_writer` | DB user |
| `MYSQL_PASSWORD` | **yes** | — | DB password |
| `API_TOKENS` | yes | — | Comma-separated bearer tokens accepted on POST |
| `DASHBOARD_PASSWORD` | no | `` | If set, dashboard requires HTTP basic auth (user: `admin`) |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

Common Unix-socket locations by distro:

| Distro | Default socket |
| --- | --- |
| Debian / Ubuntu (MariaDB or MySQL 8) | `/var/run/mysqld/mysqld.sock` |
| RHEL / Rocky / Alma (MariaDB)       | `/var/lib/mysql/mysql.sock`    |

To rotate or add a Pi, append another token to `API_TOKENS` and reload
Apache:

```bash
sudo nano /etc/pi-sensor-server/env
sudo systemctl reload apache2     # WSGI daemons get the new env on reload
```

## Quick install on a Debian / Ubuntu VPS

```bash
scp -r server you@vps:~/pi-sensor-server
ssh you@vps
cd ~/pi-sensor-server
chmod +x install.sh
sudo ./install.sh                  # generates a random API token
sudo nano /etc/pi-sensor-server/env   # set MYSQL_PASSWORD (and others if needed)
sudo systemctl reload apache2
```

The installer:

1. Installs `apache2`, `libapache2-mod-wsgi-py3`, `python3-venv`, `default-mysql-client`
2. Enables `wsgi`, `headers`, `rewrite`, `ssl` modules
3. Creates the `sensor-server` system user
4. Lays down `/opt/pi-sensor-server/` (incl. virtualenv with Flask + mysql-connector-python)
5. Drops `/etc/apache2/sites-available/pi-sensor-server.conf`, disables
   `000-default`, runs `apachectl configtest`

It deliberately does **not** reload Apache at the end — you need to fill in
`MYSQL_PASSWORD` first or the app will fail at import time.

Sanity check on the box itself after reload:

```bash
curl -s http://127.0.0.1/health
curl -X POST http://127.0.0.1/api/readings \
  -H 'Authorization: Bearer <THE_TOKEN_THE_INSTALLER_PRINTED>' \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"test","temperature_f":22.1}'

# Confirm it landed in MySQL:
mysql -u sensor_writer -p pi_sensors -e \
  "SELECT id, device_id, ts, temperature_f FROM readings ORDER BY id DESC LIMIT 5;"
```

## Add TLS with certbot

```bash
sudo apt install certbot python3-certbot-apache
sudo nano /etc/apache2/sites-available/pi-sensor-server.conf   # set ServerName
sudo certbot --apache -d sensors.example.com
sudo systemctl reload apache2
```

## Point the Pi at it

In the Pi's `/etc/pi-sensor-logger/config.yaml`:

```yaml
upload:
  url: "https://sensors.example.com/api/readings"
  device_id: "pi-zero-01"
  auth:
    type: "bearer"
    token: "PASTE-THE-TOKEN-THE-SERVER-INSTALLER-PRINTED"
```

Then `sudo systemctl restart pi-sensor-logger` on the Pi.

## Local dev (no Apache)

```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export MYSQL_SOCKET=/var/run/mysqld/mysqld.sock
export MYSQL_DB=pi_sensors
export MYSQL_USER=sensor_writer
export MYSQL_PASSWORD='your-dev-password'
export API_TOKENS=devtoken

python3 app.py        # -> http://127.0.0.1:8000

# in another terminal:
curl -X POST http://127.0.0.1:8000/api/readings \
  -H 'Authorization: Bearer devtoken' \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"test","timestamp":"2026-05-21T14:00:00Z","temperature_f":22.1,"humidity_pct":45.0,"pressure_hpa":1013.2,"distance_mm":1234,"aqi":2,"tvoc_ppb":120,"eco2_ppm":540,"wifi":{"connected":true,"ssid":"home","ip":"192.168.1.42"}}'
```

## Schema notes

The `readings` table is created with InnoDB + utf8mb4 and these columns:

```
id                    BIGINT UNSIGNED  PK AUTO_INCREMENT
device_id             VARCHAR(64)      NOT NULL
ts                    DATETIME(3)      NOT NULL   -- always stored as UTC
distance_mm           DOUBLE
temperature_f         DOUBLE
humidity_pct          DOUBLE
pressure_hpa          DOUBLE
aqi                   DOUBLE
tvoc_ppb              DOUBLE
eco2_ppm              DOUBLE
ens160_data_validity  DOUBLE
wifi_ssid             VARCHAR(64)
wifi_ip               VARCHAR(45)      -- fits IPv6
wifi_connected        TINYINT          -- 0/1
remote_ip             VARCHAR(45)
raw_json              JSON             -- the full payload, for forensics
received_at           DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3)

Indexes: (device_id, ts), (ts)
```

The connection runs `SET time_zone = '+00:00'` so DATETIME values and
`CURRENT_TIMESTAMP` are always UTC regardless of the MySQL server's setting.

## Backups

```bash
mysqldump -u sensor_writer -p pi_sensors readings \
  | gzip > readings-$(date +%F).sql.gz
```

For point-in-time recovery, enable binary logs in MySQL and use the usual
mysqldump + binlog workflow.

## Troubleshooting

- **`Access denied for user 'sensor_writer'@'localhost'`** — the user or
  password in `/etc/pi-sensor-server/env` doesn't match what's in MySQL.
  Test from the shell: `mysql -u sensor_writer -p pi_sensors`. Reload Apache
  after fixing the env file.
- **`Can't connect to local MySQL server through socket`** — `MYSQL_SOCKET`
  is wrong. Find the real path with `mysqladmin -p variables | grep socket`
  or `ss -xl | grep mysql`.
- **`Unknown database 'pi_sensors'`** — you skipped the `CREATE DATABASE`
  step above.
- **`CREATE command denied to user`** — the grant is too narrow; the app
  needs CREATE + INDEX (just on first run) in addition to INSERT/SELECT.
- **401 on every POST** — confirm the token in `/etc/pi-sensor-server/env`
  matches what the Pi is sending, and that the VirtualHost has
  `WSGIPassAuthorization On`. Reload Apache after editing env.
- **Changes to `env` don't take effect** — mod_wsgi caches the daemon
  process. `sudo systemctl reload apache2` recycles WSGI daemons.
- **`Lost connection to MySQL server during query`** — MySQL idle-timed-out
  the connection. The app opens a fresh one per request, so this should
  only happen if a single request hangs longer than `wait_timeout`. Bump
  it in `/etc/mysql/mariadb.conf.d/50-server.cnf` if needed.

## Disk usage

A reading is roughly 0.5 KB stored (incl. the JSON copy of the payload in
the `raw_json` column). One Pi posting every 60s is about 45 MB/year.
