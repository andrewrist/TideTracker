"""
app.py - Flask receiver + dashboard for the pi-sensor-logger.

Storage: MySQL / MariaDB via mysql-connector-python over a Unix socket.

Endpoints
---------
POST /api/readings        Accept a JSON reading (bearer-auth required).
GET  /api/devices         List known devices and their latest reading.
GET  /api/readings        Recent readings; query params:
                            device_id=<id>           (filter)
                            since=<iso8601 or epoch> (filter)
                            limit=<int>              (default 500, max 5000)
GET  /                    HTML dashboard.
GET  /health              Liveness probe (no auth).

Configuration is read from environment variables (see .env.example).
Under Apache + mod_wsgi these are loaded from /etc/pi-sensor-server/env by
app.wsgi; for local development you can `export` them.

    MYSQL_SOCKET         Path to mysqld.sock (required for socket connect)
    MYSQL_DB             Database (schema) name
    MYSQL_USER           DB user
    MYSQL_PASSWORD       DB password
    API_TOKENS           Comma-separated bearer tokens accepted on POST
    DASHBOARD_PASSWORD   Optional HTTP-basic password for the dashboard.
                         Username is always "admin". Leave unset to disable.
    BIND_HOST / BIND_PORT  Only used when running `python3 app.py` directly
                           for local development.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional

import mysql.connector
from mysql.connector import errorcode
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template,
    request,
)

# ---------------------------------------------------------------------------#
# Config / logging
# ---------------------------------------------------------------------------#
log = logging.getLogger("sensor_server")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

MYSQL_SOCKET   = os.environ.get("MYSQL_SOCKET", "/var/run/mysqld/mysqld.sock")
MYSQL_DB       = os.environ.get("MYSQL_DB", "pi_sensors")
MYSQL_USER     = os.environ.get("MYSQL_USER", "sensor_writer")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")

API_TOKENS = {
    t.strip()
    for t in os.environ.get("API_TOKENS", "").split(",")
    if t.strip()
}
DASHBOARD_PASSWORD   = os.environ.get("DASHBOARD_PASSWORD", "").strip()
TIDE_THRESHOLD_FT    = float(os.environ.get("TIDE_THRESHOLD_FT", "0.9"))

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _connect_kwargs() -> Dict[str, Any]:
    return {
        "unix_socket": MYSQL_SOCKET,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": MYSQL_DB,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
        "use_pure": True,            # pure-Python; avoids C-ext build issues
        "autocommit": False,
        "connection_timeout": 5,
    }


# ---------------------------------------------------------------------------#
# Database helpers
# ---------------------------------------------------------------------------#
def _new_connection() -> "mysql.connector.MySQLConnection":
    conn = mysql.connector.connect(**_connect_kwargs())
    # Force this session into UTC so DATETIME columns and CURRENT_TIMESTAMP
    # are interpreted consistently regardless of server-level time_zone.
    cur = conn.cursor()
    cur.execute("SET time_zone = '+00:00'")
    cur.close()
    return conn


def get_db() -> "mysql.connector.MySQLConnection":
    """Per-request MySQL connection, attached to Flask's request context."""
    if "db" not in g:
        g.db = _new_connection()
    return g.db


def close_db(_err: Optional[BaseException] = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def init_db() -> None:
    """Run schema.sql against the target database."""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"schema not found at {SCHEMA_PATH}")

    # Strip line-level `-- ...` comments and split on `;` so we can run
    # statements one at a time with the standard cursor API.
    raw = SCHEMA_PATH.read_text()
    cleaned_lines = []
    for line in raw.splitlines():
        stripped = line.split("--", 1)[0]
        if stripped.strip():
            cleaned_lines.append(stripped)
    statements = [s.strip() for s in " ".join(cleaned_lines).split(";") if s.strip()]

    conn = _new_connection()
    try:
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    log.info("Database ready at %s (db=%s, socket=%s)",
             MYSQL_DB, MYSQL_DB, MYSQL_SOCKET)


# ---------------------------------------------------------------------------#
# Auth helpers
# ---------------------------------------------------------------------------#
def require_bearer(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not API_TOKENS:
            log.error("API_TOKENS env var is empty; rejecting POST")
            abort(503, description="Server has no API tokens configured")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            abort(401, description="Missing Bearer token")
        token = header[len("Bearer "):].strip()
        if token not in API_TOKENS:
            log.warning("Rejected upload from %s: bad token", request.remote_addr)
            abort(401, description="Invalid token")
        return view(*args, **kwargs)
    return wrapper


def require_dashboard_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != "admin" or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="sensor-dashboard"'},
            )
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#
def _parse_timestamp_dt(raw: Any) -> datetime:
    """Accept ISO8601 / epoch / None and return a naive UTC datetime.

    MySQL DATETIME stores naive values; we always store UTC so we strip the
    tzinfo after converting.
    """
    if raw is None or raw == "":
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).replace(tzinfo=None)
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        abort(400, description=f"Invalid timestamp: {raw!r}")


def _coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Make a MySQL dict-cursor row JSON-friendly (datetime -> ISO string)."""
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat(timespec="seconds")
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------#
# App factory
# ---------------------------------------------------------------------------#
def create_app() -> Flask:
    app = Flask(__name__)
    app.teardown_appcontext(close_db)

    @app.route("/health")
    def health():
        return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    @app.post("/api/readings")
    @require_bearer
    def post_reading():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, description="Body must be a JSON object")

        device_id = data.get("device_id")
        if not device_id or not isinstance(device_id, str):
            abort(400, description="device_id is required (string)")

        ts = _parse_timestamp_dt(data.get("timestamp"))

        wifi = data.get("wifi") or {}
        wifi_ssid = wifi.get("ssid") if isinstance(wifi, dict) else None
        wifi_ip = wifi.get("ip") if isinstance(wifi, dict) else None
        wifi_connected = (
            int(bool(wifi.get("connected"))) if isinstance(wifi, dict) else None
        )

        row = (
            device_id,
            ts,
            _coerce_number(data.get("distance_mm")),
            _coerce_number(data.get("tide_height_ft")),
            _coerce_number(data.get("temperature_f")),
            _coerce_number(data.get("humidity_pct")),
            _coerce_number(data.get("pressure_hpa")),
            _coerce_number(data.get("aqi")),
            _coerce_number(data.get("tvoc_ppb")),
            _coerce_number(data.get("eco2_ppm")),
            _coerce_number(data.get("ens160_data_validity")),
            wifi_ssid,
            wifi_ip,
            wifi_connected,
            request.remote_addr,
            json.dumps(data, separators=(",", ":")),
        )

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                INSERT INTO readings (
                    device_id, ts, distance_mm, tide_height_ft,
                    temperature_f, humidity_pct, pressure_hpa,
                    aqi, tvoc_ppb, eco2_ppm, ens160_data_validity,
                    wifi_ssid, wifi_ip, wifi_connected,
                    remote_ip, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                row,
            )
            db.commit()
        except mysql.connector.Error:
            db.rollback()
            raise
        finally:
            cur.close()

        log.info("Stored reading from %s @ %s", device_id, ts.isoformat())
        return jsonify({
            "ok": True,
            "device_id": device_id,
            "ts": ts.replace(tzinfo=timezone.utc).isoformat(timespec="seconds"),
        }), 201

    @app.get("/api/devices")
    def list_devices():
        cur = get_db().cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT
                    device_id,
                    MAX(ts)  AS last_ts,
                    COUNT(*) AS reading_count
                FROM readings
                GROUP BY device_id
                ORDER BY last_ts DESC
                """
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        return jsonify([_serialize_row(r) for r in rows])

    @app.get("/api/readings")
    def list_readings():
        device_id = request.args.get("device_id")
        since = request.args.get("since")
        try:
            limit = min(int(request.args.get("limit", 500)), 5000)
        except ValueError:
            abort(400, description="limit must be an integer")

        clauses: list[str] = []
        params: list[Any] = []
        if device_id:
            clauses.append("device_id = %s")
            params.append(device_id)
        if since:
            clauses.append("ts >= %s")
            params.append(_parse_timestamp_dt(since))

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT id, device_id, ts, distance_mm, tide_height_ft,
                   temperature_f, humidity_pct, pressure_hpa,
                   aqi, tvoc_ppb, eco2_ppm, ens160_data_validity,
                   wifi_ssid, wifi_ip, wifi_connected
            FROM readings
            {where}
            ORDER BY ts DESC
            LIMIT %s
        """
        params.append(limit)
        cur = get_db().cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            cur.close()
        return jsonify([_serialize_row(r) for r in rows])

    @app.get("/")
    @require_dashboard_auth
    def dashboard():
        return render_template("dashboard.html", tide_threshold_ft=TIDE_THRESHOLD_FT)

    @app.get("/tides")
    @require_dashboard_auth
    def tides():
        return render_template("pelicantides.html", tide_threshold_ft=TIDE_THRESHOLD_FT)

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(404)
    @app.errorhandler(503)
    def _err(e):
        if request.path.startswith("/api/") or request.path == "/health":
            return (
                jsonify({"error": e.description, "code": e.code}),
                e.code,
                {"WWW-Authenticate": "Bearer"} if e.code == 401 else {},
            )
        return (e.description or "error", e.code)

    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("BIND_PORT", "8000"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
