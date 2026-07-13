-- schema.sql -- MySQL/MariaDB schema for pi-sensor-server.
-- Safe to run multiple times; uses CREATE TABLE IF NOT EXISTS.
-- The app runs this on every startup (via init_db() in app.py).

CREATE TABLE IF NOT EXISTS readings (
    id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id             VARCHAR(64)    NOT NULL,
    ts                    DATETIME(3)    NOT NULL,    -- UTC, millisecond precision
    distance_mm           DOUBLE         NULL,
    tide_height_ft        DOUBLE         NULL,
    temperature_f         DOUBLE         NULL,
    humidity_pct          DOUBLE         NULL,
    pressure_hpa          DOUBLE         NULL,
    aqi                   DOUBLE         NULL,
    tvoc_ppb              DOUBLE         NULL,
    eco2_ppm              DOUBLE         NULL,
    ens160_data_validity  DOUBLE         NULL,
    wifi_ssid             VARCHAR(64)    NULL,
    wifi_ip               VARCHAR(45)    NULL,        -- fits IPv6
    wifi_connected        TINYINT        NULL,        -- 0/1
    remote_ip             VARCHAR(45)    NULL,
    raw_json              JSON           NULL,        -- full payload, for forensics
    received_at           DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY idx_readings_device_ts (device_id, ts),
    KEY idx_readings_ts        (ts)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- Migration: add tide_height_ft to an existing table.
-- Skip this statement if the column already exists.
ALTER TABLE readings
    ADD COLUMN tide_height_ft DOUBLE NULL
    AFTER distance_mm;
