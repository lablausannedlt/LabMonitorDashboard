# LabMonitor

Continuous lab environment monitoring — Thorlabs TSP01B + STM32 IKS01A3 + OpenWeatherMap → InfluxDB → Grafana.

## What it does

- Reads **temperature & humidity** from a Thorlabs TSP01B USB sensor every 10 s (`tsp01_logger.py`)
- Reads **temperature, humidity, pressure, accelerometer, gyroscope, magnetometer** from a NUCLEO-F401RE + IKS01A3 shield over USB serial (`stm32_logger.py`)
- Fetches **outdoor weather** for Lausanne from OpenWeatherMap every 10 min (`owm_logger.py`)
- Stores everything in **InfluxDB 2.x** (Docker container `LabMonitor`, port 8086)
- Visualises live data in **Grafana** (Windows service, port 3000)

## Quick start

```powershell
# Install Python dependencies (once)
pip install -r requirements.txt

# Test manually (Ctrl+C to stop each)
python tsp01_logger.py
python stm32_logger.py
python owm_logger.py

# Start background tasks (no window)
schtasks /run /tn "TSP01 logger"
schtasks /run /tn "STM32 logger"
schtasks /run /tn "OWM logger"

# Check they are running
Get-Process pythonw
```

## Files

| File | Description |
|------|-------------|
| `tsp01_logger.py` | Thorlabs TSP01B sensor → InfluxDB daemon |
| `stm32_logger.py` | STM32 IKS01A3 (serial) → InfluxDB daemon |
| `owm_logger.py` | OpenWeatherMap → InfluxDB daemon |
| `config.yaml` | Shared config for all loggers (tokens, ports, intervals) |
| `config.example.yaml` | Config template — copy to `config.yaml` and fill in values |
| `requirements.txt` | Python dependencies |
| `tests/` | Parser unit tests (`pytest tests/`) |
| `list_dll_exports.py` | Dev utility — enumerate Thorlabs DLL exports |

## Configuration

All three loggers share a single `config.yaml`:

```yaml
sensor:        # TSP01B — VISA resource string
influxdb:      # shared InfluxDB connection (URL, token, org, bucket)
acquisition:   # TSP01B polling interval and error thresholds
openweathermap: # OWM API key, city, polling interval
stm32:         # serial port, baud rate, batch interval
```

Copy the template and fill in real values (see inline comments):

```powershell
Copy-Item config.example.yaml config.yaml
```

`config.yaml` is gitignored — never commit it.

## InfluxDB schema

| Measurement | Fields | Tags |
|-------------|--------|------|
| `tsp01` | `temperature_internal_c`, `humidity_pct`, `temperature_external_c`, `temperature_external_c2` | `location`, `sensor_id` |
| `environment` | `temperature_degC`, `humidity_pct`, `pressure_hPa` | `sensor_index`, `location`, `sensor_id` |
| `imu` | `acc_x/y/z`, `gyr_x/y/z`, `mag_x/y/z` | `sensor_index`, `location`, `sensor_id` |
| `openweathermap` | `main_temp`, `main_humidity`, `main_pressure`, `wind_speed`, … | `name`, `sys_country` |

## Logs

Runtime logs are written alongside the scripts:

| File | Logger |
|------|--------|
| `tsp01_logger.log` | TSP01B |
| `stm32_logger.log` | STM32 IKS01A3 |
| `owm_logger.log` | OpenWeatherMap |

## URLs

| Service | URL |
|---------|-----|
| InfluxDB UI | http://localhost:8086 |
| Grafana (local) | http://localhost:3000 |
| Grafana (LAN) | http://192.168.0.251:3000 |

## Requirements

- Windows 10/11 (64-bit)
- Thorlabs TSP01 software (installs `TLTSPB_64.dll`)
- NUCLEO-F401RE flashed with X-CUBE-MEMS1 `DataLogTerminal` firmware
- Python 3.10+ (Miniconda)
- Docker Desktop (InfluxDB container)
- Grafana Windows service
- OpenWeatherMap free API key

See `LabMonitor_Documentation.docx` for full setup and removal instructions.
