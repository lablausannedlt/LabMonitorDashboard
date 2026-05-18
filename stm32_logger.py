"""
STM32 IKS01A3 → InfluxDB 2.x logger
=====================================
Reads DataLogTerminal serial output from a NUCLEO-F401RE + IKS01A3 shield,
parses each sensor line with regex, and writes batched points to InfluxDB.

The serial port is reopened automatically if the USB cable is disconnected.
Points are collected for one batch window (batch_seconds) before writing.

Requirements:
  pip install -r requirements.txt   # includes pyserial

Usage:
  python stm32_logger.py              # uses config.yaml in same directory
  python stm32_logger.py my_cfg.yaml  # uses a custom config file
"""

import re
import sys
import time
import signal
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

import serial
import yaml
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("stm32_logger.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("stm32")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _shutdown(sig, frame):
    global _running
    log.info("Shutdown signal received – stopping after current batch.")
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _interruptible_sleep(seconds: float, chunk: float = 1.0):
    """Sleep for *seconds* but wake up every *chunk* seconds to check _running."""
    deadline = time.monotonic() + seconds
    while _running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(chunk, remaining))


# ---------------------------------------------------------------------------
# Serial line parser
# ---------------------------------------------------------------------------
_IGNORED_RE = re.compile(r"^(WHOAMI|ODR|FS)\[")

# Single-value environment lines: (regex, influx_field_name, cast_fn)
_ENV_PATTERNS: list[tuple[re.Pattern, str, type]] = [
    (re.compile(r"^Hum\[(\d+)\]:\s*([\d.]+)\s*%$"),              "humidity_pct",     float),
    (re.compile(r"^Temp\[(\d+)\]:\s*([+-]?[\d.]+)\s*degC$"),     "temperature_degC", float),
    (re.compile(r"^Press\[(\d+)\]:\s*([\d.]+)\s*hPa$"),          "pressure_hPa",     float),
]

# Three-axis IMU lines: (regex, field_x, field_y, field_z)
# \1 back-reference ensures the sensor index is the same for all three axes.
_IMU_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (
        re.compile(
            r"^ACC_X\[(\d+)\]:\s*([+-]?\d+),\s*ACC_Y\[\1\]:\s*([+-]?\d+),\s*ACC_Z\[\1\]:\s*([+-]?\d+)$"
        ),
        "acc_x", "acc_y", "acc_z",
    ),
    (
        re.compile(
            r"^GYR_X\[(\d+)\]:\s*([+-]?\d+),\s*GYR_Y\[\1\]:\s*([+-]?\d+),\s*GYR_Z\[\1\]:\s*([+-]?\d+)$"
        ),
        "gyr_x", "gyr_y", "gyr_z",
    ),
    (
        re.compile(
            r"^MAG_X\[(\d+)\]:\s*([+-]?\d+),\s*MAG_Y\[\1\]:\s*([+-]?\d+),\s*MAG_Z\[\1\]:\s*([+-]?\d+)$"
        ),
        "mag_x", "mag_y", "mag_z",
    ),
]


def _point_key(p: Point) -> tuple:
    """Unique key for a point: (measurement, frozen tags, frozen field names).
    Used to keep only the latest sample per channel in each write window."""
    return (p._name, frozenset(p._tags.items()), frozenset(p._fields.keys()))


def parse_line(line: str) -> list[Point]:
    """
    Parse one DataLogTerminal output line into zero or more InfluxDB Points.

    Returns [] for WHOAMI/ODR metadata lines, empty/whitespace input, or
    any line matching no known pattern.  Never raises.
    """
    line = line.strip()
    if not line or _IGNORED_RE.match(line):
        return []

    now = datetime.now(timezone.utc)

    for pattern, field_name, cast in _ENV_PATTERNS:
        m = pattern.match(line)
        if m:
            return [
                Point("environment")
                .tag("sensor_index", m.group(1))
                .field(field_name, cast(m.group(2)))
                .time(now, WritePrecision.S)
            ]

    for pattern, fx, fy, fz in _IMU_PATTERNS:
        m = pattern.match(line)
        if m:
            return [
                Point("imu")
                .tag("sensor_index", m.group(1))
                .field(fx, int(m.group(2)))
                .field(fy, int(m.group(3)))
                .field(fz, int(m.group(4)))
                .time(now, WritePrecision.S)
            ]

    return []


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------
class InfluxWriter:
    """Writes pre-built Point objects to InfluxDB 2.x."""

    def __init__(self, cfg: dict):
        self._bucket = cfg["bucket"]
        self._org    = cfg["org"]
        self._client = InfluxDBClient(
            url=cfg["url"], token=cfg["token"], org=self._org
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        log.info(
            "InfluxDB connected: %s  bucket=%s  org=%s",
            cfg["url"], self._bucket, self._org,
        )

    def write(self, points: list[Point]) -> None:
        if points:
            self._write_api.write(bucket=self._bucket, org=self._org, record=points)

    def close(self):
        self._write_api.close()
        self._client.close()
        log.info("InfluxDB connection closed.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Main acquisition loop
# ---------------------------------------------------------------------------
def run(config_path: str = "config.yaml"):
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        log.error("Config file not found: %s", cfg_file.resolve())
        sys.exit(1)

    with open(cfg_file, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    stm32_cfg  = cfg["stm32"]
    influx_cfg = cfg["influxdb"]

    port        = stm32_cfg["port"]
    baud        = int(stm32_cfg.get("baud", 115200))
    retry_delay = float(stm32_cfg.get("retry_delay_seconds", 5))
    batch_sec   = float(stm32_cfg.get("batch_seconds", 10.0))
    max_retries = int(stm32_cfg.get("max_consecutive_errors", 5))
    extra_tags  = stm32_cfg.get("tags", {})

    log.info(
        "STM32 logger starting  port=%s  baud=%d  batch=%.1fs",
        port, baud, batch_sec,
    )

    with InfluxWriter(influx_cfg) as writer:
        ser: serial.Serial | None = None
        consecutive_errors = 0
        unmatched_warned: set[str] = set()

        while _running:
            # (Re)open serial port if disconnected
            if ser is None or not ser.is_open:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                try:
                    ser = serial.Serial(port, baud, timeout=1.0)
                    log.info("Serial port opened: %s @ %d baud", port, baud)
                    unmatched_warned.clear()
                except serial.SerialException as exc:
                    log.warning(
                        "Cannot open %s: %s — retrying in %.0fs",
                        port, exc, retry_delay,
                    )
                    _interruptible_sleep(retry_delay)
                    continue

            # Collect one window of data; keep only the latest sample per channel
            latest: dict[tuple, Point] = {}
            batch_deadline = time.monotonic() + batch_sec
            serial_error = False

            while _running and time.monotonic() < batch_deadline:
                try:
                    raw = ser.readline()   # blocks up to serial timeout (1 s)
                except serial.SerialException as exc:
                    log.warning("Serial read error: %s — reconnecting", exc)
                    ser = None
                    serial_error = True
                    break

                if raw:
                    line = raw.decode("utf-8", errors="replace").strip()
                    points = parse_line(line)
                    if points:
                        for p in points:
                            for k, v in extra_tags.items():
                                p.tag(k, v)
                            latest[_point_key(p)] = p
                    elif line and not _IGNORED_RE.match(line):
                        if line not in unmatched_warned:
                            log.warning("Unmatched serial line: %r", line)
                            unmatched_warned.add(line)

            if serial_error or not latest:
                continue

            # Write one sample per channel to InfluxDB
            to_write = list(latest.values())
            try:
                writer.write(to_write)
                log.info("Wrote %d point(s) to InfluxDB", len(to_write))
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                log.warning(
                    "InfluxDB write error (%d/%d): %s",
                    consecutive_errors, max_retries, exc,
                )
                if consecutive_errors >= max_retries:
                    log.error("Too many consecutive errors – exiting.")
                    break
                _interruptible_sleep(retry_delay)

        if ser and ser.is_open:
            ser.close()

    log.info("STM32 logger stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="STM32 IKS01A3 → InfluxDB 2.x logger")
    parser.add_argument(
        "config",
        nargs="?",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    args = parser.parse_args()
    run(args.config)
