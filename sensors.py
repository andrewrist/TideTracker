"""
sensors.py - Sensor wrappers for the SparkFun Qwiic chain.

Hardware:
  - Raspberry Pi Zero 2 W with a Qwiic SHIM (exposes I2C on GPIO2/SDA + GPIO3/SCL)
  Distance sensors (tried in priority order — first to init successfully wins):
    1. RCWL-1655 ultrasonic — GPIO  TRIG=GPIO23 (Pin 16), ECHO=GPIO24 (Pin 18)
    2. SparkFun Ultrasonic Distance Sensor - TCT40 (Qwiic) / HC-SR04 — I2C addr 0x00
    3. SparkFun Qwiic Distance Sensor VL53L1X                         — I2C addr 0x29
  - SparkFun Qwiic Environmental Combo ENS160 + BME280
        BME280  default 0x77
        ENS160  default 0x53 (0x52 if address-jumper is closed)

All I2C sensors share the Qwiic bus.  We use the Adafruit CircuitPython drivers
via Blinka for the environmental sensors, SparkFun's qwiic_ultrasonic library for
the HC-SR04 Qwiic sensor, and gpiozero for the GPIO-connected RCWL-1655.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class SensorBus:
    """Lazy-initialised wrapper around the sensors on the Qwiic bus."""

    def __init__(self) -> None:
        # Import board/busio inside __init__ so importing this module on a
        # non-Pi machine (e.g. for syntax checks) doesn't blow up.
        import board
        import busio

        self._board = board
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self._rcwl_gpio = None  # RCWL-1655 via GPIO (highest priority)
        self._hcsr04 = None     # HC-SR04 Qwiic via I2C
        self.distance = None    # VL53L1X ToF (fallback)  # type: ignore[assignment]
        self.bme = None       # type: ignore[assignment]
        self.ens = None       # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #
    def init_rcwl1655_gpio(
        self,
        trig_pin: int = 23,
        echo_pin: int = 24,
        max_distance_m: float = 4.0,
    ) -> bool:
        """Initialise the RCWL-1655 ultrasonic sensor connected directly to GPIO pins.

        TRIG → GPIO23 (Pin 16), ECHO → GPIO24 (Pin 18) by default.
        Uses gpiozero.DistanceSensor which handles timing in a background thread.
        Returns True if init succeeds; False otherwise.
        """
        try:
            from gpiozero import DistanceSensor

            sensor = DistanceSensor(
                echo=echo_pin,
                trigger=trig_pin,
                max_distance=max_distance_m,
            )
            self._rcwl_gpio = sensor
            log.info(
                "RCWL-1655 GPIO ready (TRIG=GPIO%d, ECHO=GPIO%d, max=%.1fm)",
                trig_pin,
                echo_pin,
                max_distance_m,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("RCWL-1655 GPIO init failed: %s", exc)
            self._rcwl_gpio = None
            return False

    def init_hcsr04(self, address: int = 0x00) -> bool:
        """Initialise the SparkFun Ultrasonic Distance Sensor - TCT40 (Qwiic) / HC-SR04.

        Returns True if the sensor is found and responds; False otherwise.
        Uses address 0x00 (SparkFun Qwiic Ultrasonic default).
        """
        try:
            import qwiic_ultrasonic  # sparkfun-qwiic-ultrasonic

            sensor = qwiic_ultrasonic.QwiicUltrasonic(address=address)
            if not sensor.is_connected():
                log.info("HC-SR04 Qwiic not detected at 0x%02X; will try VL53L1X", address)
                return False
            sensor.begin()
            self._hcsr04 = sensor
            log.info("HC-SR04 Qwiic ready (addr=0x%02X)", address)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("HC-SR04 Qwiic init failed: %s", exc)
            self._hcsr04 = None
            return False

    def init_vl53l1x(
        self,
        address: int = 0x29,
        distance_mode: str = "long",
        timing_budget_ms: int = 100,
    ) -> bool:
        """Initialise the VL53L1X time-of-flight distance sensor."""
        try:
            import adafruit_vl53l1x

            self.distance = adafruit_vl53l1x.VL53L1X(self.i2c, address=address)
            # 1 = short (~1.3 m), 2 = long (~4 m)
            self.distance.distance_mode = 2 if distance_mode.lower() == "long" else 1
            self.distance.timing_budget = int(timing_budget_ms)
            self.distance.start_ranging()
            log.info(
                "VL53L1X ready  (addr=0x%02X, mode=%s, budget=%dms)",
                address,
                distance_mode,
                timing_budget_ms,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("VL53L1X init failed: %s", exc)
            self.distance = None
            return False

    def init_bme280(self, address: int = 0x77) -> bool:
        """Initialise the BME280 temperature/humidity/pressure sensor."""
        try:
            from adafruit_bme280 import basic as adafruit_bme280

            self.bme = adafruit_bme280.Adafruit_BME280_I2C(self.i2c, address=address)
            log.info("BME280 ready   (addr=0x%02X)", address)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("BME280 init failed: %s", exc)
            self.bme = None
            return False

    def init_ens160(self, address: int = 0x53) -> bool:
        """Initialise the ENS160 air-quality sensor."""
        try:
            import adafruit_ens160

            self.ens = adafruit_ens160.ENS160(self.i2c, address=address)
            # MODE_STANDARD = standard gas sensing
            self.ens.mode = adafruit_ens160.MODE_STANDARD
            log.info("ENS160 ready   (addr=0x%02X)", address)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("ENS160 init failed: %s", exc)
            self.ens = None
            return False

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def read_distance_mm(self, poll_timeout_s: float = 1.0) -> Optional[float]:
        """Return distance in millimetres using whichever sensor initialised.

        Prefers the RCWL-1655 ultrasonic sensor; falls back to VL53L1X ToF.
        Returns None if neither sensor is available or a read fails.
        """
        if self._rcwl_gpio is not None:
            return self._read_rcwl1655_gpio_mm()
        if self._hcsr04 is not None:
            return self._read_hcsr04_mm()
        return self._read_vl53l1x_mm(poll_timeout_s)

    def _read_rcwl1655_gpio_mm(self, retries: int = 5, retry_delay_s: float = 0.1) -> Optional[float]:
        """Read distance from the GPIO-connected RCWL-1655.

        gpiozero's DistanceSensor.distance returns metres (0.0 on missed echo).
        Retries up to `retries` times if 0 is returned.
        """
        import time

        for attempt in range(1, retries + 1):
            try:
                m = self._rcwl_gpio.distance
                if m is None or m == 0.0:
                    log.debug("RCWL-1655 GPIO attempt %d/%d: missed echo", attempt, retries)
                else:
                    return round(m * 1000.0, 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("RCWL-1655 GPIO read failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(retry_delay_s)

        log.warning("RCWL-1655 GPIO: no valid reading after %d attempts", retries)
        return None

    def _read_hcsr04_mm(self, retries: int = 5, retry_delay_s: float = 0.1) -> Optional[float]:
        """Read distance from the HC-SR04 Qwiic ultrasonic sensor.

        Retries up to `retries` times (with `retry_delay_s` pause between attempts)
        if the sensor returns 0, which indicates a missed echo.
        """
        import time

        for attempt in range(1, retries + 1):
            try:
                self._hcsr04.trigger_and_read()
                cm = self._hcsr04.distance
                if cm is None:
                    log.debug("HC-SR04 Qwiic attempt %d/%d: None", attempt, retries)
                elif cm == 0:
                    log.debug("HC-SR04 Qwiic attempt %d/%d: missed echo (0)", attempt, retries)
                else:
                    return round(float(cm) * 10.0, 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("HC-SR04 Qwiic read failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(retry_delay_s)

        log.warning("HC-SR04 Qwiic: no valid reading after %d attempts", retries)
        return None

    def _read_vl53l1x_mm(self, poll_timeout_s: float = 1.0) -> Optional[float]:
        """Poll the VL53L1X until a reading is ready or timeout expires."""
        if self.distance is None:
            return None

        import time

        deadline = time.monotonic() + poll_timeout_s
        try:
            while time.monotonic() < deadline:
                if self.distance.data_ready:
                    cm = self.distance.distance  # the driver returns centimetres
                    self.distance.clear_interrupt()
                    if cm is None:
                        return None
                    return round(cm * 10.0, 1)
                time.sleep(0.02)
            log.warning("VL53L1X read timed out after %.2fs", poll_timeout_s)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("VL53L1X read failed: %s", exc)
            return None

    def read_bme280(self) -> Dict[str, Optional[float]]:
        """Return temperature (C), humidity (%RH), pressure (hPa)."""
        if self.bme is None:
            return {"temperature_c": None, "humidity_pct": None, "pressure_hpa": None}
        try:
            return {
                "temperature_c": round(float(self.bme.temperature), 2),
                "humidity_pct": round(float(self.bme.relative_humidity), 2),
                "pressure_hpa": round(float(self.bme.pressure), 2),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("BME280 read failed: %s", exc)
            return {"temperature_c": None, "humidity_pct": None, "pressure_hpa": None}

    def read_ens160(
        self,
        temp_c: Optional[float] = None,
        hum_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return AQI, TVOC (ppb), eCO2 (ppm) and data-validity flag.

        Feeding ambient temperature/humidity from the BME280 into the ENS160
        improves its accuracy; we do so when those values are available.
        """
        if self.ens is None:
            return {
                "aqi": None,
                "tvoc_ppb": None,
                "eco2_ppm": None,
                "data_validity": None,
            }
        try:
            if temp_c is not None:
                self.ens.temperature_compensation = float(temp_c)
            if hum_pct is not None:
                self.ens.humidity_compensation = float(hum_pct)
            return {
                "aqi": int(self.ens.AQI) if self.ens.AQI is not None else None,
                "tvoc_ppb": int(self.ens.TVOC) if self.ens.TVOC is not None else None,
                "eco2_ppm": int(self.ens.eCO2) if self.ens.eCO2 is not None else None,
                "data_validity": getattr(self.ens, "data_validity", None),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("ENS160 read failed: %s", exc)
            return {
                "aqi": None,
                "tvoc_ppb": None,
                "eco2_ppm": None,
                "data_validity": None,
            }
