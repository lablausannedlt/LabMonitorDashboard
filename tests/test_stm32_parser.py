"""
Unit tests for stm32_logger.parse_line().

Tests cover all recognised line types, metadata lines that must be ignored,
and edge cases that must not raise.

Run from the repo root:
  pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from stm32_logger import parse_line


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def lp(pts: list, idx: int = 0) -> str:
    """Return the InfluxDB line-protocol string for point at index idx."""
    return pts[idx].to_line_protocol()


# ---------------------------------------------------------------------------
# Environment sensor lines
# ---------------------------------------------------------------------------
def test_humidity():
    pts = parse_line("Hum[0]: 43.14 %")
    assert len(pts) == 1
    s = lp(pts)
    assert s.startswith("environment,sensor_index=0")
    assert "humidity_pct=" in s
    assert "43.14" in s


def test_humidity_sensor_index_1():
    pts = parse_line("Hum[1]: 55.00 %")
    assert len(pts) == 1
    assert "sensor_index=1" in lp(pts)


def test_temperature_positive():
    pts = parse_line("Temp[0]: +23.42 degC")
    assert len(pts) == 1
    s = lp(pts)
    assert s.startswith("environment,sensor_index=0")
    assert "temperature_degC=" in s
    assert "23.42" in s


def test_temperature_negative():
    pts = parse_line("Temp[2]: -5.10 degC")
    assert len(pts) == 1
    s = lp(pts)
    assert "sensor_index=2" in s
    assert "temperature_degC=" in s
    assert "-5.1" in s


def test_temperature_no_sign():
    pts = parse_line("Temp[1]: 23.54 degC")
    assert len(pts) == 1
    assert "temperature_degC=" in lp(pts)


def test_pressure():
    pts = parse_line("Press[1]: 963.29 hPa")
    assert len(pts) == 1
    s = lp(pts)
    assert "environment,sensor_index=1" in s
    assert "pressure_hPa=" in s
    assert "963.29" in s


# ---------------------------------------------------------------------------
# IMU lines
# ---------------------------------------------------------------------------
def test_accel():
    pts = parse_line("ACC_X[0]: 65, ACC_Y[0]: 122, ACC_Z[0]: 1008")
    assert len(pts) == 1
    s = lp(pts)
    assert s.startswith("imu,sensor_index=0")
    assert "acc_x=65i" in s
    assert "acc_y=122i" in s
    assert "acc_z=1008i" in s


def test_accel_negative_values():
    pts = parse_line("ACC_X[0]: -10, ACC_Y[0]: 5, ACC_Z[0]: -3")
    assert len(pts) == 1
    s = lp(pts)
    assert "acc_x=-10i" in s
    assert "acc_y=5i" in s
    assert "acc_z=-3i" in s


def test_gyro():
    pts = parse_line("GYR_X[0]: 10, GYR_Y[0]: -5, GYR_Z[0]: 3")
    assert len(pts) == 1
    s = lp(pts)
    assert s.startswith("imu,sensor_index=0")
    assert "gyr_x=10i" in s
    assert "gyr_y=-5i" in s
    assert "gyr_z=3i" in s


def test_mag():
    pts = parse_line("MAG_X[0]: 120, MAG_Y[0]: -44, MAG_Z[0]: 310")
    assert len(pts) == 1
    s = lp(pts)
    assert s.startswith("imu,sensor_index=0")
    assert "mag_x=120i" in s
    assert "mag_y=-44i" in s
    assert "mag_z=310i" in s


def test_imu_mismatched_sensor_indices_not_matched():
    # Back-reference \1 in regex must reject mismatched indices
    pts = parse_line("ACC_X[0]: 65, ACC_Y[1]: 122, ACC_Z[0]: 1008")
    assert pts == []


# ---------------------------------------------------------------------------
# Ignored metadata lines
# ---------------------------------------------------------------------------
def test_whoami_ignored():
    assert parse_line("WHOAMI[0]: 0x6c") == []


def test_odr_ignored():
    assert parse_line("ODR[0]: 25.000 Hz") == []


# ---------------------------------------------------------------------------
# Edge cases — must not raise
# ---------------------------------------------------------------------------
def test_empty_string():
    assert parse_line("") == []


def test_whitespace_only():
    assert parse_line("   ") == []


def test_newline_only():
    assert parse_line("\n") == []


def test_garbage_ascii():
    assert parse_line("this is not sensor data!@#$%") == []


def test_garbage_with_brackets():
    assert parse_line("Foo[0]: bar") == []


def test_partial_acc_line():
    assert parse_line("ACC_X[0]: 65") == []


def test_leading_whitespace_stripped():
    pts = parse_line("  Hum[0]: 43.14 %")
    assert len(pts) == 1


def test_trailing_whitespace_stripped():
    pts = parse_line("Hum[0]: 43.14 %  ")
    assert len(pts) == 1
