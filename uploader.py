"""
uploader.py - Posts JSON readings to an HTTPS endpoint.

Supports three auth styles (configurable in config.yaml):
  * none
  * bearer    -> Authorization: Bearer <token>
  * api_key   -> <header_name>: <key>
  * basic     -> HTTP basic auth
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


class Uploader:
    def __init__(
        self,
        url: str,
        auth: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
        retries: int = 3,
    ) -> None:
        self.url = url
        self.timeout = int(timeout)
        self.retries = max(1, int(retries))
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._configure_auth(auth or {})

    def _configure_auth(self, auth: Dict[str, Any]) -> None:
        atype = str(auth.get("type") or "none").lower()
        if atype == "bearer":
            token = str(auth.get("token", ""))
            if not token:
                log.warning("Bearer auth selected but token is empty")
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif atype == "api_key":
            header = str(auth.get("header_name", "X-API-Key"))
            key = str(auth.get("key", ""))
            if not key:
                log.warning("API-key auth selected but key is empty")
            self.session.headers[header] = key
        elif atype == "basic":
            user = str(auth.get("username", ""))
            pw = str(auth.get("password", ""))
            self.session.auth = (user, pw)
        elif atype == "none":
            pass
        else:
            log.warning("Unknown auth type %r; sending unauthenticated", atype)

    def post(self, payload: Dict[str, Any]) -> bool:
        """POST JSON with exponential backoff. Returns True on 2xx."""
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout)
                if 200 <= resp.status_code < 300:
                    log.info("Upload OK (HTTP %d)", resp.status_code)
                    return True
                log.warning(
                    "Upload returned HTTP %d (attempt %d/%d): %s",
                    resp.status_code,
                    attempt,
                    self.retries,
                    resp.text[:200],
                )
                # 4xx (other than 429) is unlikely to succeed on retry
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return False
            except requests.RequestException as exc:
                log.warning(
                    "Upload attempt %d/%d failed: %s", attempt, self.retries, exc
                )
            if attempt < self.retries:
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        return False
