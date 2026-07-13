# Pi Sensor Logger

Periodically reads distance and full environmental data from a Qwiic sensor
chain on a Raspberry Pi Zero 2 W and uploads the readings as JSON to an HTTPS
endpoint.

## Hardware

- Raspberry Pi Zero 2 W running Raspberry Pi OS (Bookworm or Bullseye)
- SparkFun Qwiic SHIM for Raspberry Pi (exposes the Pi's I2C bus on a Qwiic connector)
- SparkFun Qwiic Distance Sensor — VL53L1X (I2C `0x29`)
- SparkFun Qwiic Environmental Combo Breakout — ENS160 + BME280
  - BME280 at I2C `0x77`
  - ENS160 at I2C `0x53`

All three boards share the single I2C bus brought out by the Qwiic SHIM.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Main loop: load config, read sensors, POST JSON |
| `sensors.py` | Adafruit-driver wrappers for VL53L1X, BME280, ENS160 |
| `uploader.py` | HTTP JSON POST with bearer / API key / basic auth and retry/backoff |
| `config.yaml` | Configuration template — edit before running |
| `requirements.txt` | Python dependencies |
| `pi-sensor-logger.service` | systemd unit (auto-starts on boot, restarts on failure) |
| `install.sh` | One-shot installer for the Pi |

## Quick install (on the Pi)

```bash
# 1. Copy this whole directory to the Pi, e.g. ~/pi-sensor-logger
scp -r pi-sensor-logger pi@raspberrypi.local:~/

# 2. SSH in and run the installer
ssh pi@raspberrypi.local
cd ~/pi-sensor-logger
chmod +x install.sh
sudo ./install.sh

# 3. Edit the config and fill in your endpoint + token
sudo nano /etc/pi-sensor-logger/config.yaml

# 4. Start the service
sudo systemctl start pi-sensor-logger
sudo journalctl -u pi-sensor-logger -f
```

The installer:

1. Installs `python3-venv`, `i2c-tools`, `libgpiod2`, `wireless-tools`
2. Enables the I2C interface (`raspi-config nonint do_i2c 0`)
3. Copies files to `/opt/pi-sensor-logger/`
4. Creates a virtualenv and installs Python deps
5. Installs the systemd unit and enables it on boot

## Configuration

All runtime settings live in `/etc/pi-sensor-logger/config.yaml`:

```yaml
upload:
  url: "https://example.com/api/readings"
  device_id: "pi-zero-01"
  timeout_seconds: 10
  retries: 3
  auth:
    type: "bearer"   # or api_key | basic | none
    token: "REPLACE_WITH_YOUR_TOKEN"

measurement:
  interval_seconds: 60

sensors:
  vl53l1x: { enabled: true, address: 0x29, distance_mode: "long", timing_budget_ms: 100 }
  bme280:  { enabled: true, address: 0x77 }
  ens160:  { enabled: true, address: 0x53 }
```

### Wi-Fi

Wi-Fi is managed by Raspberry Pi OS (NetworkManager on Bookworm, wpa_supplicant
on Bullseye). Set it up once with `sudo raspi-config`, `nmtui`, or by editing
`/etc/wpa_supplicant/wpa_supplicant.conf`. The app only **reads** the current
SSID via `iwgetid` so it can include it in payloads and skip uploads when
offline. You can set `wifi.expected_ssid` to get a warning if the Pi ever
joins a different network.

## JSON payload format

One POST per measurement, `Content-Type: application/json`:

```json
{
  "device_id": "pi-zero-01",
  "timestamp": "2026-05-21T14:32:07+00:00",
  "distance_mm": 1234.5,
  "temperature_f": 73.06,
  "humidity_pct": 41.3,
  "pressure_hpa": 1013.2,
  "aqi": 2,
  "tvoc_ppb": 87,
  "eco2_ppm": 540,
  "ens160_data_validity": 0,
  "wifi": { "connected": true, "ssid": "HomeWiFi", "ip": "192.168.1.42" }
}
```

`ens160_data_validity` follows the ENS160 datasheet: `0` = normal,
`1` = warm-up, `2` = initial start-up, `3` = invalid output. Expect `1` or
`2` for the first few minutes after a cold boot.

`null` appears in any field whose sensor failed to read.

## Verifying sensors before running the service

```bash
# List I2C devices — you should see 0x29, 0x53, 0x77
sudo i2cdetect -y 1

# Take a single reading and POST it
sudo -u pi /opt/pi-sensor-logger/venv/bin/python \
     /opt/pi-sensor-logger/main.py \
     --config /etc/pi-sensor-logger/config.yaml --once
```

## Troubleshooting

- **`Failed to open I2C bus`** — I2C isn't enabled. Run `sudo raspi-config`
  → Interface Options → I2C → Enable, then reboot.
- **All sensors return `null`** — make sure your user is in the `i2c` group
  (`sudo usermod -aG i2c pi` then log out/in). The installer attempts this
  automatically.
- **`iwgetid` returns nothing** — only applies to wired or no connection;
  uploads will be skipped until Wi-Fi associates.
- **Uploads keep returning 401** — double-check the `auth.type` and that the
  token/key matches what your endpoint expects.

## License

Use it however you like.
