#!/usr/bin/env python3
"""
main.py - Pi Zero 2 W sensor logger.

Reads distance + full environmental panel from the Qwiic sensor chain every
`measurement.interval_seconds` and POSTs the readings as JSON to the URL in
the config file.

Run manually:
    python3 main.py --config /etc/pi-sensor-logger/config.yaml

Or via systemd (see pi-sensor-logger.service).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

import yaml

from sensors import SensorBus
from uploader import Uploader

log = logging.getLogger("sensor_logger")

DEFAULT_CONFIG_PATH = "/etc/pi-sensor-logger/config.yaml"

# ---------------------------------------------------------------------- #
# Signal handling
# ---------------------------------------------------------------------- #
_shutdown = False


def _signal_handler(signum, _frame):  # noqa: D401
    global _shutdown
    log.info("Received signal %d, shutting down...", signum)
    _shutdown = True


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def configure_logging(cfg: Dict[str, Any]) -> None:
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    logfile = cfg.get("file")
    if logfile:
        try:
            from logging.handlers import TimedRotatingFileHandler

            handlers.append(
                TimedRotatingFileHandler(
                    logfile,
                    when="midnight",
                    backupCount=int(cfg.get("rotate_keep_days", 30)),
                    encoding="utf-8",
                )
            )
        except (PermissionError, FileNotFoundError, OSError) as exc:
            print(f"warning: cannot open log file {logfile}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def wifi_status() -> Dict[str, Any]:
    """Report WiFi SSID + local IP without managing the connection."""
    info: Dict[str, Any] = {"connected": False, "ssid": None, "ip": None}
    # IP address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # We never actually send; this just picks the local interface.
        s.connect(("8.8.8.8", 80))
        info["ip"] = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    # Current SSID via iwgetid (present on Raspberry Pi OS)
    try:
        out = subprocess.check_output(
            ["iwgetid", "-r"], timeout=3, stderr=subprocess.DEVNULL
        )
        ssid = out.decode().strip()
        if ssid:
            info["ssid"] = ssid
            info["connected"] = True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # If we can't run iwgetid, fall back to "connected if we have an IP".
        info["connected"] = bool(info["ip"])

    return info


def interruptible_sleep(total_seconds: float) -> None:
    """Sleep, but wake quickly on shutdown signal."""
    end = time.monotonic() + total_seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(min(0.5, end - time.monotonic()))


# ---------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Raspberry Pi sensor logger")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH),
        help=f"Path to config.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Take a single reading, upload it, and exit (useful for testing)",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"error: config file not found: {args.config}", file=sys.stderr)
        return 2

    configure_logging(config.get("logging", {}))
    log.info("pi-sensor-logger starting (config=%s)", args.config)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    meas_cfg = config.get("measurement", {}) or {}
    interval = int(meas_cfg.get("interval_seconds", 60))
    sensor_height_mm = float(meas_cfg.get("sensor_height_mm", 3200))
    up_cfg = config.get("upload", {})
    device_id = str(up_cfg.get("device_id") or socket.gethostname())
    expected_ssid = config.get("wifi", {}).get("expected_ssid")

    # ------------------------------------------------------------------ #
    # Sensor init
    # ------------------------------------------------------------------ #
    try:
        bus = SensorBus()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to open I2C bus: %s", exc)
        return 3

    s_cfg = config.get("sensors", {}) or {}

    # Distance sensor priority: RCWL-1655 GPIO → HC-SR04 Qwiic → VL53L1X ToF.
    # First sensor to init successfully is used; the rest are skipped.
    distance_found = False

    rcwl_gpio_cfg = s_cfg.get("rcwl1655_gpio", {}) or {}
    if not distance_found and rcwl_gpio_cfg.get("enabled", True):
        distance_found = bus.init_rcwl1655_gpio(
            trig_pin=int(rcwl_gpio_cfg.get("trig_pin", 23)),
            echo_pin=int(rcwl_gpio_cfg.get("echo_pin", 24)),
            max_distance_m=float(rcwl_gpio_cfg.get("max_distance_m", 4.0)),
        )

    hcsr04_cfg = s_cfg.get("hcsr04", {}) or {}
    if not distance_found and hcsr04_cfg.get("enabled", True):
        distance_found = bus.init_hcsr04(address=int(hcsr04_cfg.get("address", 0x00)))

    vl_cfg = s_cfg.get("vl53l1x", {}) or {}
    if not distance_found and vl_cfg.get("enabled", True):
        bus.init_vl53l1x(
            address=int(vl_cfg.get("address", 0x29)),
            distance_mode=str(vl_cfg.get("distance_mode", "long")),
            timing_budget_ms=int(vl_cfg.get("timing_budget_ms", 100)),
        )

    bme_cfg = s_cfg.get("bme280", {}) or {}
    if bme_cfg.get("enabled", True):
        bus.init_bme280(address=int(bme_cfg.get("address", 0x77)))

    ens_cfg = s_cfg.get("ens160", {}) or {}
    if ens_cfg.get("enabled", True):
        bus.init_ens160(address=int(ens_cfg.get("address", 0x53)))

    # ------------------------------------------------------------------ #
    # Uploader
    # ------------------------------------------------------------------ #
    if not up_cfg.get("url"):
        log.error("upload.url not set in config; aborting")
        return 4

    uploader = Uploader(
        url=str(up_cfg["url"]),
        auth=up_cfg.get("auth", {}) or {},
        timeout=int(up_cfg.get("timeout_seconds", 10)),
        retries=int(up_cfg.get("retries", 3)),
    )

    log.info(
        "Measurement loop starting (interval=%ds, device_id=%s, url=%s)",
        interval,
        device_id,
        up_cfg["url"],
    )

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    while not _shutdown:
        loop_start = time.monotonic()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        distance_mm = bus.read_distance_mm()
        tide_height_ft = (
            round(((sensor_height_mm - distance_mm) / 25.4) / 12, 3)
            if distance_mm is not None
            else None
        )
        bme = bus.read_bme280()
        ens = bus.read_ens160(
            temp_c=bme.get("temperature_c"),
            hum_pct=bme.get("humidity_pct"),
        )
        wifi = wifi_status()

        payload: Dict[str, Any] = {
            "device_id": device_id,
            "timestamp": ts,
            "distance_mm": distance_mm,
            "tide_height_ft": tide_height_ft,
            "temperature_f": (
                None if bme["temperature_c"] is None
                else round(bme["temperature_c"] * 9.0 / 5.0 + 32.0, 2)
            ),
            "humidity_pct": bme["humidity_pct"],
            "pressure_hpa": bme["pressure_hpa"],
            "aqi": ens["aqi"],
            "tvoc_ppb": ens["tvoc_ppb"],
            "eco2_ppm": ens["eco2_ppm"],
            "ens160_data_validity": ens["data_validity"],
            "wifi": wifi,
        }

        log.info(
            "Reading: %s",
            json.dumps({k: v for k, v in payload.items() if k != "wifi"}),
        )

        if expected_ssid and wifi.get("ssid") and wifi["ssid"] != expected_ssid:
            log.warning(
                "Connected to SSID %r but config expects %r",
                wifi["ssid"],
                expected_ssid,
            )

        if wifi["connected"]:
            ok = uploader.post(payload)
            if not ok:
                log.error("Upload ultimately failed for timestamp %s", ts)
        else:
            log.warning("WiFi not connected; skipping upload for %s", ts)

        if args.once:
            log.info("--once specified; exiting after first reading")
            break

        # Sleep the remainder of the interval
        elapsed = time.monotonic() - loop_start
        interruptible_sleep(max(0.0, interval - elapsed))

    log.info("Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
