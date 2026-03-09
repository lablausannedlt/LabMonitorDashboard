"""
OpenWeatherMap → InfluxDB 2.x Logger
=====================================
Fetches current weather from the free OpenWeatherMap /data/2.5/weather
endpoint and writes it to InfluxDB using the same field/tag names that
the standard OpenWeatherMap Grafana dashboard expects.

This replaces Telegraf's inputs.openweathermap plugin, which requires
paid API endpoints (/data/2.5/group and /data/2.5/forecast).

Requirements:
  pip install influxdb-client requests pyyaml

Usage:
  python owm_logger.py              # uses config.yaml in same directory
  python owm_logger.py my_cfg.yaml  # uses a custom config file
"""

import sys
import time
import signal
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

import requests
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
        logging.FileHandler("owm_logger.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("owm")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _shutdown(sig, frame):
    global _running
    log.info("Shutdown signal received – stopping.")
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
# OpenWeatherMap fetch
# ---------------------------------------------------------------------------
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


def fetch_weather(api_key: str, city_id: int, units: str = "metric") -> dict:
    """
    Fetch current weather for a city and return a flat dict of
    fields + tags matching the Telegraf openweathermap measurement schema.
    """
    resp = requests.get(
        OWM_URL,
        params={"id": city_id, "appid": api_key, "units": units, "lang": "en"},
        timeout=10,
    )
    resp.raise_for_status()
    d = resp.json()

    tags = {
        "name":        d.get("name", ""),
        "sys_country": d.get("sys", {}).get("country", ""),
    }

    fields = {
        # Temperature / humidity / pressure
        "main_temp":      float(d["main"]["temp"]),
        "main_feels_like": float(d["main"]["feels_like"]),
        "main_temp_min":  float(d["main"]["temp_min"]),
        "main_temp_max":  float(d["main"]["temp_max"]),
        "main_pressure":  float(d["main"]["pressure"]),
        "main_humidity":  float(d["main"]["humidity"]),
        # Wind
        "wind_speed":     float(d["wind"]["speed"]),
        "wind_deg":       float(d["wind"].get("deg", 0)),
        # Clouds
        "clouds_all":     float(d["clouds"]["all"]),
        # Visibility (metres)
        "visibility":     float(d.get("visibility", 0)),
        # Sunrise / sunset (Unix seconds — dashboard multiplies by 1000 for ms)
        "sys_sunrise":    float(d["sys"]["sunrise"]),
        "sys_sunset":     float(d["sys"]["sunset"]),
        # Weather description (string field)
        "weather_0_description": d["weather"][0]["description"],
        "weather_0_main":        d["weather"][0]["main"],
    }

    return {"tags": tags, "fields": fields}


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------
class InfluxWriter:
    def __init__(self, cfg: dict):
        self._bucket      = cfg["bucket"]
        self._org         = cfg["org"]
        # Always use "openweathermap" — ignore the tsp01 measurement name
        # that may be set in the shared influxdb config section
        self._measurement = "openweathermap"

        self._client = InfluxDBClient(
            url=cfg["url"], token=cfg["token"], org=self._org
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        log.info("InfluxDB connected: %s  bucket=%s", cfg["url"], self._bucket)

    def write(self, tags: dict, fields: dict, timestamp: datetime | None = None):
        ts = timestamp or datetime.now(timezone.utc)
        point = Point(self._measurement).time(ts, WritePrecision.S)
        for k, v in tags.items():
            point = point.tag(k, v)
        for k, v in fields.items():
            point = point.field(k, v)
        self._write_api.write(bucket=self._bucket, org=self._org, record=point)

    def close(self):
        self._write_api.close()
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(config_path: str = "config.yaml"):
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        log.error("Config file not found: %s", cfg_file.resolve())
        sys.exit(1)

    with open(cfg_file, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    owm_cfg    = cfg["openweathermap"]
    influx_cfg = cfg["influxdb"]
    acq_cfg    = cfg.get("acquisition", {})

    api_key     = owm_cfg["api_key"]
    city_id     = int(owm_cfg["city_id"])
    units       = owm_cfg.get("units", "metric")
    interval_s  = float(owm_cfg.get("interval_seconds", 60))   # 10 min default
    retry_delay = float(acq_cfg.get("retry_delay_seconds", 60))
    max_retries = int(acq_cfg.get("max_consecutive_errors", 5))

    log.info("Polling interval: %.0f s  city_id=%d", interval_s, city_id)

    with InfluxWriter(influx_cfg) as writer:
        consecutive_errors = 0

        while _running:
            loop_start = time.monotonic()

            try:
                data = fetch_weather(api_key, city_id, units)
                now  = datetime.now(timezone.utc)
                writer.write(data["tags"], data["fields"], timestamp=now)

                log.info(
                    "%s, %s  T=%.1f°C  RH=%.0f%%  P=%.0fhPa  Wind=%.1fm/s  %s",
                    data["tags"]["name"],
                    data["tags"]["sys_country"],
                    data["fields"]["main_temp"],
                    data["fields"]["main_humidity"],
                    data["fields"]["main_pressure"],
                    data["fields"]["wind_speed"],
                    data["fields"]["weather_0_description"],
                )
                consecutive_errors = 0

            except Exception as exc:
                consecutive_errors += 1
                log.warning("Error (%d/%d): %s", consecutive_errors, max_retries, exc)
                if consecutive_errors >= max_retries:
                    log.error("Too many consecutive errors – exiting.")
                    break
                _interruptible_sleep(retry_delay)
                continue

            elapsed    = time.monotonic() - loop_start
            sleep_time = max(0.0, interval_s - elapsed)
            _interruptible_sleep(sleep_time)

    log.info("OWM logger stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenWeatherMap → InfluxDB logger")
    parser.add_argument("config", nargs="?", default="config.yaml")
    args = parser.parse_args()
    run(args.config)
