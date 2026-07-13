"""
app.wsgi - entry point for Apache + mod_wsgi.

Apache will load this file once per daemon process (see WSGIDaemonProcess in
the VirtualHost). It does three things:

  1. Loads /etc/pi-sensor-server/env into os.environ so that app.py picks up
     API_TOKENS, MYSQL_*, etc. (mod_wsgi does NOT inherit the shell env, and
     SetEnv in the VHost only affects request environ, not the Python
     process environ -- so we read the file ourselves.)
  2. Adds the application directory to sys.path so Flask templates resolve.
  3. Imports the Flask app and exposes it as `application` (the name mod_wsgi
     expects by default).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("PI_SENSOR_ENV_FILE", "/etc/pi-sensor-server/env"))


def _load_env_file(path: Path) -> None:
    """Parse a simple KEY=VALUE env file and populate os.environ.

    Lines starting with '#' or blank are ignored. Values may be optionally
    wrapped in single or double quotes. Existing env vars are NOT overwritten,
    so you can override with `SetEnv` (for WSGI request scope) or pass an
    explicit value when testing.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(ENV_FILE)

# Ensure the app's directory is importable
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Import after env vars are in place -- app.py reads them at import time.
from app import app as application  # noqa: E402  (mod_wsgi convention)
