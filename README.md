# LabMonitor

Continuous lab environment monitoring — Thorlabs TSP01B sensor + OpenWeatherMap → InfluxDB → Grafana.

## What it does

- Reads **temperature & humidity** from a Thorlabs TSP01B USB sensor every 10 s
- Fetches **outdoor weather** for Lausanne from OpenWeatherMap every 10 min
- Stores everything in **InfluxDB 2.x** (Docker container `LabMonitor`, port 8086)
- Visualises live data in **Grafana** (Windows service, port 3000)

## Quick start

```powershell
# Install Python dependencies (once)
pip install -r requirements.txt

# Test manually
python tsp01_logger.py     # Ctrl+C to stop
python owm_logger.py       # Ctrl+C to stop

# Start background tasks (no window)
schtasks /run /tn "TSP01 logger"
schtasks /run /tn "OWM logger"

# Check they are running
Get-Process pythonw
```

## Files

| File | Description |
|------|-------------|
| `tsp01_logger.py` | TSP01B sensor → InfluxDB daemon |
| `owm_logger.py` | OpenWeatherMap → InfluxDB daemon |
| `config.yaml` | Shared config (tokens, intervals, city ID) |
| `requirements.txt` | Python dependencies |
| `list_dll_exports.py` | Dev utility — enumerate Thorlabs DLL exports |

## Logs

Runtime logs are written alongside the scripts:
- `tsp01_logger.log`
- `owm_logger.log`

## URLs

| Service | URL |
|---------|-----|
| InfluxDB UI | http://localhost:8086 |
| Grafana (local) | http://localhost:3000 |
| Grafana (LAN) | http://192.168.0.251:3000 |

## Requirements

- Windows 10/11 (64-bit)
- Thorlabs TSP01 software (installs `TLTSPB_64.dll`)
- Python 3.10+ (Miniconda)
- Docker Desktop (InfluxDB container)
- Grafana Windows service
- OpenWeatherMap free API key

See `LabMonitor_Documentation.docx` for full setup and removal instructions.
