# NagpurBus — Real-Time City Transit & Multi-Criteria RAPTOR Planner

Real-time public transit intelligence and journey planning platform for **Nagpur** (NMC Aapli Bus + Nagpur Maha Metro + Walking), engineered with strict data truthfulness, GTFS/GTFS-RT standards compliance, and 60 FPS vector maps.

---

## Core Architecture

- **GTFS Static Ingestion & Road-Snapping Pipeline (`ingest/`)**:
  - Builds a clean GTFS static feed (`0 errors, 0 warnings` on MobilityData GTFS Validator v8.0.1) covering **30 routes** — 12 official NMC feeder routes parsed directly from Maha Metro schedules, 16 NMC Aapli Bus city trunk corridors from Maha Metro Table 3-12 operational parameters, plus Nagpur Metro Aqua & Orange Lines.
  - Dual-carriageway OSM road snapping so bus stops align cleanly with actual drivable curbs and directional lanes.
- **Live Telemetry & GTFS-RT Engine (`backend/`, `eta_engine/`, `simulator/`)**:
  - FastAPI backend with 5-second WebSocket vehicle broadcast (`live_hub.py`), protobuf GTFS-RT adapter ready for live municipal feeds, and a stochastic Nagpur fleet simulator (explicitly isolated and labelled `SIMULATED` — never mixed with real GPS telemetry).
  - Every arrival time carries an explicit provenance label (`LIVE`, `SCHEDULED`, `ESTIMATED`, or `SIMULATED`).
- **Multi-Criteria RAPTOR Journey Planner (`planner/raptor.py`)**:
  - Optimizes multi-modal **Bus + Metro + Walk** transfers across earliest arrival, minimum transfers, and total walking distance.
- **Commuter Map UI & Nagpur Area Guide (`backend/app/map_ui.py`, `backend/app/nagpur_bot.py`)**:
  - Interactive 60 FPS MapLibre GL JS interface using OpenFreeMap Liberty vector tiles (`© OpenStreetMap contributors`, ODbL) — zero paid map API keys.
  - Built-in Nagpur Area Guide Bot for route lookups, fare/interchange guidance, and locality search across Nagpur neighborhoods.
- **Mobile Shell (`mobile/`)**:
  - Cross-platform Flutter client scaffold (`pubspec.yaml`, `lib/`) for Android/iOS transit tracking.

---

## Quick Start

```powershell
# 1. Create virtual environment & install package
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Copy environment configuration
Copy-Item .env.example .env

# 3. Start the FastAPI server + Commuter Map UI
uvicorn backend.app.main:app --reload --port 8000
```

Open **`http://127.0.0.1:8000`** in your browser.

---

## Running Tests & Linting

```powershell
pytest -v
ruff check .
```

---

## Data Sources & Attribution

See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for complete provenance, honest schedule limitations, and OpenStreetMap / Maha Metro / NMC attribution details.
