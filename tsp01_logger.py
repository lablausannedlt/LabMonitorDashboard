"""
TSP01 Temperature & Humidity Logger → InfluxDB 2.x
====================================================
Reads from a Thorlabs TSP01 sensor via the TLTS instrument driver DLL
and writes measurements to InfluxDB 2.x for visualization in Grafana.

The TSP01 is a HID USB device — it does NOT appear as a standard VISA/USBTMC
instrument, so PyVISA cannot enumerate it. This script talks directly to
Thorlabs' TLTS_64.dll (installed with the TSP01 software).

Requirements:
  pip install influxdb-client pyyaml

Thorlabs software (provides TLTS_64.dll):
  https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=TSP01

Usage:
  python tsp01_logger.py              # uses config.yaml in same directory
  python tsp01_logger.py my_cfg.yaml  # uses a custom config file
  python tsp01_logger.py --list-devices
"""

import sys
import time
import signal
import logging
import argparse
import ctypes
from ctypes import (
    c_long, c_ulong, c_ushort, c_short, c_double, c_uint,
    c_char_p, byref, create_string_buffer,
)
from pathlib import Path
from datetime import datetime, timezone

import yaml
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("tsp01_logger.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("tsp01")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _shutdown(sig, frame):
    global _running
    log.info("Shutdown signal received – stopping after current iteration.")
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
# TLTS DLL wrapper
# ---------------------------------------------------------------------------

# Candidate install paths for the Thorlabs TSP01 instrument driver DLL.
# TLTSPB_64.dll (TSP01B) is tried FIRST — the shared header confirms this variant.
# TLTSP_64.dll (original TSP01) is kept as a fallback.
_DLL_CANDIDATES = [
    # 64-bit TLTSPB (TSP01B) — try first based on header evidence
    r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLTSPB_64.dll",
    r"C:\Program Files\IVI Foundation\Win64\Bin\TLTSPB_64.dll",
    r"C:\Program Files (x86)\Thorlabs\TSP01\TLTSPB_32.dll",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Bin\TLTSPB_32.dll",
    # 64-bit TLTSP (original TSP01) — fallback
    r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLTSP_64.dll",
    r"C:\Program Files\IVI Foundation\Win64\Bin\TLTSP_64.dll",
    r"C:\Program Files (x86)\Thorlabs\TSP01\TLTSP_32.dll",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Bin\TLTSP_32.dll",
]

# Each driver variant uses different names for the resource-discovery functions.
# TLTSPB: findRsrc / getRsrcName  (from the official header)
# TLTSP:  getDeviceCount / getDeviceResourceString
_DISCOVERY_FUNCS: dict[str, tuple[str, str]] = {
    "TLTSPB": ("findRsrc",        "getRsrcName"),
    "TLTSP":  ("getDeviceCount",  "getDeviceResourceString"),
}

# VISA / IVI type aliases mapped to ctypes
ViStatus  = c_long
ViSession = c_ulong
ViBoolean = c_ushort
ViReal64  = c_double
ViInt16   = c_short
ViUInt16  = c_ushort   # channel arg is unsigned in the driver header
ViUInt32  = c_uint
TLTS_BUFFER_SIZE = 256

# Temperature channel constants (from TLTSP_Defines.h)
TLTSP_TEMPER_CHANNEL_1 = 11   # internal thermistor
TLTSP_TEMPER_CHANNEL_2 = 12   # external probe connector
TLTSP_TEMPER_CHANNEL_3 = 13   # second external probe (if present)


def _load_tlts_dll() -> tuple[ctypes.WinDLL, str]:
    """
    Find and load the Thorlabs TSP01 instrument driver DLL.

    Returns:
        (dll, prefix) where prefix is the function name prefix,
        e.g. 'TLTSP' for TLTSP_64.dll  →  functions are TLTSP_init, etc.
    """
    for path in _DLL_CANDIDATES:
        if Path(path).exists():
            log.info("Loading driver DLL: %s", path)
            dll = ctypes.WinDLL(path)
            # Derive function prefix from stem: "TLTSP_64" → "TLTSP"
            prefix = Path(path).stem.split("_")[0]
            log.info("Using function prefix: %s_*", prefix)
            return dll, prefix
    raise FileNotFoundError(
        "Thorlabs TSP01 driver DLL not found.\n"
        "Install the TSP01 software from:\n"
        "  https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=TSP01\n"
        "Searched:\n" + "\n".join(f"  {p}" for p in _DLL_CANDIDATES)
    )


_VISA_ERRORS = {
    0xBFFF0011: "VI_ERROR_RSRC_NFOUND – device not found at that resource string "
                "(check the string with --list-devices, and close Thorlabs TSP01 software if open)",
    0xBFFF0072: "VI_ERROR_RSRC_BUSY – device is in use by another application "
                "(close Thorlabs TSP01 software)",
    0xBFFF00A0: "VI_ERROR_NCIC – not currently the controller in charge",
    0xBFFF0015: "VI_ERROR_TMO – operation timed out",
    0xBFFF003E: "VI_ERROR_NSUP_OPER – operation not supported",
}


def _check(status: int, fn_name: str, dll=None, vi=None):
    """Raise if a TLTSP function returned a non-zero (error) status."""
    if status == 0:
        return
    code = status & 0xFFFFFFFF
    hint = _VISA_ERRORS.get(code, "")
    # Try to get a human-readable message from the driver itself
    driver_msg = ""
    if dll is not None:
        # Try both known error message function names
        for em_fn in ("TLTSPB_errorMessage", "TLTSP_errorMessage"):
            try:
                buf = create_string_buffer(512)
                getattr(dll, em_fn)(vi or ViSession(0), c_long(status), buf)
                driver_msg = buf.value.decode(errors="replace").strip()
                break
            except Exception:
                continue
    detail = driver_msg or hint or "no additional detail"
    raise RuntimeError(
        f"{fn_name} failed  [0x{code:08X}]  {detail}"
    )


class TSP01:
    """
    Wrapper around the Thorlabs TLTS instrument driver DLL for the TSP01.

    Channel mapping:
        channel 1 → internal thermistor
        channel 2 → external probe (optional)
    """

    def __init__(self, resource_name: str | None = None):
        """
        Args:
            resource_name: VISA-style USB resource string, e.g.
                           "USB0::0x1313::0x8075::P5007554::0::INSTR"
                           Pass None to auto-detect the first connected TSP01.
        """
        self._dll, self._pfx = _load_tlts_dll()
        self._vi  = ViSession(0)

        if resource_name is None:
            resource_name = self._find_first_resource()
            log.info("Auto-detected resource: %s", resource_name)

        rsrc = resource_name.encode() if isinstance(resource_name, str) else resource_name

        fn = getattr(self._dll, f"{self._pfx}_init")
        status = fn(
            c_char_p(rsrc),
            ViBoolean(0),       # IDQuery  = False (avoids extra comms during open)
            ViBoolean(0),       # resetDevice = False
            byref(self._vi),
        )
        _check(status, f"{self._pfx}_init", dll=self._dll)
        log.info("TSP01 initialised (handle=%d)", self._vi.value)

    # ------------------------------------------------------------------
    # Resource discovery
    # ------------------------------------------------------------------

    def _find_first_resource(self) -> str:
        """Return the resource string of the first TSP01/TSP01B found."""
        count_fn, name_fn = _DISCOVERY_FUNCS[self._pfx]
        count = ViUInt32(0)
        status = getattr(self._dll, f"{self._pfx}_{count_fn}")(
            ViSession(0), byref(count)   # VI_NULL session, per header
        )
        _check(status, f"{self._pfx}_{count_fn}")
        if count.value == 0:
            raise RuntimeError(
                f"No devices found by {self._pfx}_{count_fn}. "
                "Check the USB cable and that the Thorlabs driver is installed."
            )
        buf = create_string_buffer(TLTS_BUFFER_SIZE)
        status = getattr(self._dll, f"{self._pfx}_{name_fn}")(
            ViSession(0), ViUInt32(0), buf
        )
        _check(status, f"{self._pfx}_{name_fn}")
        return buf.value.decode()

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def read_temperature(self, channel: int = TLTSP_TEMPER_CHANNEL_1) -> float:
        """
        Read temperature in °C.

        Args:
            channel: TLTSP_TEMPER_CHANNEL_1 (11) = internal thermistor
                     TLTSP_TEMPER_CHANNEL_2 (12) = external probe
        """
        temp = ViReal64(0.0)
        status = getattr(self._dll, f"{self._pfx}_measTemperature")(
            self._vi,
            ViUInt16(channel),   # driver header declares this as ViUInt16
            byref(temp),
        )
        _check(status, f"{self._pfx}_measTemperature(ch{channel})")
        return temp.value

    def read_humidity(self) -> float:
        """Read relative humidity in %RH."""
        hum = ViReal64(0.0)
        status = getattr(self._dll, f"{self._pfx}_measHumidity")(
            self._vi, byref(hum)
        )
        _check(status, f"{self._pfx}_measHumidity")
        return hum.value

    def read_all(self) -> dict:
        """Return dict with all available measurements."""
        data: dict = {
            "temperature_internal_c": self.read_temperature(TLTSP_TEMPER_CHANNEL_1),
            "humidity_pct":           self.read_humidity(),
        }
        # External probe: attempt read, skip if not connected or error
        try:
            ext = self.read_temperature(TLTSP_TEMPER_CHANNEL_2)
            # Driver returns ~9.9e37 when probe is absent
            if abs(ext) < 1e10:
                data["temperature_external_c"] = ext
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------
    # Context manager / cleanup
    # ------------------------------------------------------------------

    def close(self):
        if self._vi.value:
            getattr(self._dll, f"{self._pfx}_close")(self._vi)
            log.info("TSP01 connection closed.")
            self._vi = ViSession(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def list_devices():
    """Helper: print all TSP01/TSP01B devices found by the Thorlabs driver."""
    dll, pfx = _load_tlts_dll()
    count_fn, name_fn = _DISCOVERY_FUNCS[pfx]
    count = ViUInt32(0)
    status = getattr(dll, f"{pfx}_{count_fn}")(ViSession(0), byref(count))
    _check(status, f"{pfx}_{count_fn}")
    print(f"Found {count.value} device(s) via {pfx}_{count_fn}:")
    for i in range(count.value):
        rsrc = create_string_buffer(TLTS_BUFFER_SIZE)
        getattr(dll, f"{pfx}_{name_fn}")(ViSession(0), ViUInt32(i), rsrc)
        print(f"  [{i}] {rsrc.value.decode()}")


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------

class InfluxWriter:
    """Writes data points to InfluxDB 2.x."""

    def __init__(self, cfg: dict):
        self._bucket      = cfg["bucket"]
        self._org         = cfg["org"]
        self._measurement = cfg.get("measurement", "tsp01")
        self._tags        = cfg.get("tags", {})

        self._client = InfluxDBClient(
            url=cfg["url"],
            token=cfg["token"],
            org=self._org,
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        log.info(
            "InfluxDB connected: %s  bucket=%s  org=%s",
            cfg["url"], self._bucket, self._org,
        )

    def write(self, fields: dict, timestamp: datetime | None = None):
        ts = timestamp or datetime.now(timezone.utc)
        point = Point(self._measurement).time(ts, WritePrecision.S)
        for k, v in self._tags.items():
            point = point.tag(k, v)
        for k, v in fields.items():
            point = point.field(k, v)
        self._write_api.write(bucket=self._bucket, org=self._org, record=point)
        log.debug("Written to InfluxDB: %s", fields)

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

    sensor_cfg = cfg["sensor"]
    influx_cfg = cfg["influxdb"]
    acq_cfg    = cfg.get("acquisition", {})

    interval_s  = float(acq_cfg.get("interval_seconds", 10))
    retry_delay = float(acq_cfg.get("retry_delay_seconds", 30))
    max_retries = int(acq_cfg.get("max_consecutive_errors", 5))

    # resource_name can be omitted in config to trigger auto-detection
    resource_name = sensor_cfg.get("visa_resource") or None

    log.info("Polling interval: %.1f s", interval_s)

    with TSP01(resource_name) as sensor, InfluxWriter(influx_cfg) as writer:
        consecutive_errors = 0

        while _running:
            loop_start = time.monotonic()

            try:
                data = sensor.read_all()
                now  = datetime.now(timezone.utc)
                writer.write(data, timestamp=now)

                ext_str = (
                    f"T_ext={data['temperature_external_c']:.3f} °C"
                    if "temperature_external_c" in data
                    else "(no ext probe)"
                )
                log.info(
                    "T_int=%.3f °C  RH=%.2f %%  %s",
                    data["temperature_internal_c"],
                    data["humidity_pct"],
                    ext_str,
                )
                consecutive_errors = 0

            except Exception as exc:
                consecutive_errors += 1
                log.warning(
                    "Read error (%d/%d): %s", consecutive_errors, max_retries, exc
                )
                if consecutive_errors >= max_retries:
                    log.error("Too many consecutive errors – exiting.")
                    break
                _interruptible_sleep(retry_delay)
                continue

            elapsed    = time.monotonic() - loop_start
            sleep_time = max(0.0, interval_s - elapsed)
            _interruptible_sleep(sleep_time)

    log.info("Logger stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TSP01 → InfluxDB 2.x logger")
    parser.add_argument(
        "config",
        nargs="?",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print all TSP01 devices found by the TLTS driver and exit",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    run(args.config)
