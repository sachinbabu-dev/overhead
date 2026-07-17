# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

Always launch via `./run.sh` — it creates `.venv/` on first use, installs `requirements.txt` when its hash changes, and execs the target script with the venv's interpreter. Running `python server.py` against the system Python will silently disable satellite tracking (skyfield lives in the venv, so the import guard at `server.py:186` returns early).

```bash
# Web server (primary mode — browser UI at http://localhost:8000)
./run.sh

# Fullscreen Pygame radar (standalone, ceiling-projector mode)
./run.sh flight_tracker.py
```

The server reads `PORT` from the environment (default `8000`). On Railway the `Procfile` runs `uvicorn server:app` directly (Railway builds its own environment; `run.sh` is local-only).

## Environment variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LAT` / `LON` | Reading, UK | Observer position |
| `RADIUS` | `0.5` (degrees) | Scan radius — server converts to nautical miles for adsb.lol |
| `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` | — | OAuth2 credentials for OpenSky metadata & track endpoints |
| `AVIATIONSTACK_KEY` | — | Flight detail API (departure/arrival enrichment) |
| `POLL_INTERVAL` | `15` s | How often the flight fetch loop runs |
| `SAT_POLL_INTERVAL` | `60` s | Satellite position refresh rate |
| `SAT_MIN_ELEVATION` | `0` deg | Filter satellites below this elevation angle |
| `TARGET_FPS` | `30` | Pygame render loop target (flight_tracker.py only) |

## Architecture

### Two entry points

**`server.py`** is the production path. It runs two daemon threads:
- **Flight loop** — polls `adsb.lol` every `POLL_INTERVAL` seconds and writes to `_cache` under `_lock`.
- **Satellite loop** — fetches TLEs from Celestrak, computes overhead positions with `skyfield`, and writes to `_sat_cache` under `_sat_lock`. TLEs are refreshed once per hour.

FastAPI exposes these endpoints:
- `GET /` — serves `index.html`
- `GET /api/flights` — current aircraft list + config
- `GET /api/flightinfo/{icao}` — OpenSky metadata + most recent flight leg (two parallel requests via `ThreadPoolExecutor`)
- `GET /api/track/{icao}` — full trajectory waypoints from OpenSky
- `GET /api/satellites` — current overhead satellites
- `GET /api/flightdetail/{callsign}` — Aviationstack enrichment with parallel airport detail lookups

**`flight_tracker.py`** is the standalone Pygame version. It fetches from OpenSky directly (not adsb.lol), maintains a position `_history` dict (up to 15 trail points per aircraft), and renders a fullscreen radar with animated sweep, altitude-coloured icons, and a click-to-inspect detail panel. No web server involved.

### Frontend (`index.html`)

Single-file browser client. Leaflet.js provides the map tile layer underneath a `<canvas>` overlay that draws the radar. The canvas polls `/api/flights` and `/api/satellites` on separate intervals and renders aircraft icons, satellite markers, and an optional flight detail side panel. All styling uses CSS custom properties defined at `:root`.

### Key design notes

- **IPv4 forcing** (`server.py:28`): Railway's outbound traffic is IPv6 by default, which OpenSky Network blocks. `urllib3.util.connection.allowed_gai_family` is monkey-patched to `AF_INET` at startup.
- **Data source split**: live position data comes from `adsb.lol` (no auth, works from cloud IPs); aircraft metadata and track history come from OpenSky (requires credentials).
- **No database** — all state is in-process memory; a server restart clears history.
