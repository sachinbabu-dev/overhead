#!/usr/bin/env python3
"""
server.py — FastAPI backend for the web-based flight tracker.

Runs the OpenSky fetch loop server-side (keeping credentials out of the
browser), caches the latest state, and exposes it as JSON at /api/flights.
Also serves index.html at /.

  pip install -r requirements.txt
  python server.py
  → open http://localhost:8000
"""

import os
import threading
import time

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

# ==============================================================================
# CONFIG
# ==============================================================================

LAT                   = float(os.getenv("LAT",            "51.42981802598574"))
LON                   = float(os.getenv("LON",            "-0.9751568602915136"))
RADIUS                = float(os.getenv("RADIUS",         "0.5"))
OPENSKY_CLIENT_ID     = os.getenv("OPENSKY_CLIENT_ID",    "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET","")
POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL",    "15"))

print(f"[config] LAT={LAT} LON={LON} RADIUS={RADIUS} POLL_INTERVAL={POLL_INTERVAL}")
print(f"[config] OPENSKY_CLIENT_ID={'SET' if OPENSKY_CLIENT_ID else 'NOT SET'}")
print(f"[config] OPENSKY_CLIENT_SECRET={'SET' if OPENSKY_CLIENT_SECRET else 'NOT SET'}")

OPENSKY_URL       = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")

# ==============================================================================
# TOKEN MANAGEMENT
# ==============================================================================

_token        = ""
_token_expiry = 0.0


def fetch_token():
    global _token, _token_expiry
    resp = requests.post(
        OPENSKY_TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data          = resp.json()
    _token        = data["access_token"]
    _token_expiry = time.time() + data.get("expires_in", 1800) - 30


def get_headers():
    if not OPENSKY_CLIENT_ID:
        return {}
    if time.time() >= _token_expiry:
        fetch_token()
    return {"Authorization": f"Bearer {_token}"}


# ==============================================================================
# SHARED CACHE  (fetch thread writes; API handler reads)
# ==============================================================================

_lock  = threading.Lock()
_cache = {"planes": [], "fetched_at": 0.0, "error": ""}


def fetch_loop():
    while True:
        try:
            params = {
                "lamin": LAT - RADIUS, "lamax": LAT + RADIUS,
                "lomin": LON - RADIUS, "lomax": LON + RADIUS,
                "extended": 1,   # required to receive s[17] category field
            }
            resp = requests.get(
                OPENSKY_URL, params=params, headers=get_headers(), timeout=10
            )
            resp.raise_for_status()
            states = resp.json().get("states") or []

            planes = []
            for s in states:
                if s[5] is None or s[6] is None:
                    continue
                callsign = (s[1] or "").strip() or s[0] or "????"
                planes.append({
                    "icao":      s[0] or "????",
                    "callsign":  callsign,
                    "lat":       s[6],
                    "lon":       s[5],
                    "alt_m":     s[7],
                    "on_ground": bool(s[8]),
                    "speed_ms":  s[9],
                    "track":     s[10],
                    # ADS-B emitter category (s[17]): 8=rotorcraft, 9=glider,
                    # 14=UAV, 10=lighter-than-air — used to select radar icon
                    "category":  s[17] if len(s) > 17 else None,
                })

            with _lock:
                _cache["planes"]     = planes
                _cache["fetched_at"] = time.time()
                _cache["error"]      = ""

        except requests.exceptions.Timeout:
            with _lock: _cache["error"] = "API timeout — retrying"
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response else "?"
            if code == 429:
                retry_after = int(
                    exc.response.headers.get("X-Rate-Limit-Retry-After-Seconds", POLL_INTERVAL * 4)
                )
                with _lock: _cache["error"] = f"Rate limited — waiting {retry_after}s"
                time.sleep(retry_after)
                continue
            with _lock: _cache["error"] = f"HTTP {code}"
        except requests.exceptions.ConnectionError:
            with _lock: _cache["error"] = "No network connection"
        except Exception as exc:
            with _lock: _cache["error"] = str(exc)[:100]

        time.sleep(POLL_INTERVAL)


threading.Thread(target=fetch_loop, daemon=True).start()

# ==============================================================================
# API
# ==============================================================================

app = FastAPI()


@app.get("/api/flights")
def api_flights():
    with _lock:
        return {
            "planes":     _cache["planes"],
            "fetched_at": _cache["fetched_at"],
            "error":      _cache["error"],
            "config": {
                "lat":    LAT,
                "lon":    LON,
                "radius": RADIUS,
            },
        }


@app.get("/api/flightinfo/{icao}")
def api_flightinfo(icao: str):
    """
    Returns aircraft metadata (type, registration, operator) and the most
    recent flight's estimated departure/arrival airports.
    Two OpenSky calls are made in parallel threads to keep latency low.
    """
    import concurrent.futures
    now  = int(time.time())
    meta = {}
    trip = {}

    def fetch_meta():
        try:
            r = requests.get(
                f"https://opensky-network.org/api/metadata/aircraft/icao/{icao.lower()}",
                headers=get_headers(), timeout=8,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def fetch_trip():
        try:
            r = requests.get(
                "https://opensky-network.org/api/flights/aircraft",
                params={"icao24": icao.lower(), "begin": now - 86400, "end": now},
                headers=get_headers(), timeout=8,
            )
            if r.status_code == 200:
                flights = r.json() or []
                if flights:
                    return max(flights, key=lambda f: f.get("lastSeen") or f.get("firstSeen") or 0)
        except Exception:
            pass
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fm = ex.submit(fetch_meta)
        ft = ex.submit(fetch_trip)
        meta = fm.result()
        trip = ft.result()

    cat = (meta.get("categoryDescription") or "").lower()
    return {
        "icao":         icao,
        "registration": meta.get("registration"),
        "typecode":     meta.get("typecode"),
        "model":        meta.get("model") or meta.get("manufacturername"),
        "operator":     meta.get("operator") or meta.get("operatorcallsign"),
        "is_helicopter": "rotorcraft" in cat or "helicopter" in cat,
        "origin":       trip.get("estDepartureAirport"),
        "destination":  trip.get("estArrivalAirport"),
    }


@app.get("/api/track/{icao}")
def api_track(icao: str):
    """
    Fetch the full flight trajectory from OpenSky for a single aircraft.
    Returns an ordered list of {lat, lon, alt_m, track} waypoints from
    takeoff to the most recent position.
    path entry format from OpenSky: [time, lat, lon, baro_alt, true_track, on_ground]
    """
    try:
        resp = requests.get(
            "https://opensky-network.org/api/tracks",
            params={"icao24": icao.lower(), "time": 0},
            headers=get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        path = [
            {"lat": p[1], "lon": p[2], "alt_m": p[3]}
            for p in (data.get("path") or [])
            if p[1] is not None and p[2] is not None
        ]
        return {"icao": icao, "callsign": data.get("callsign", ""), "path": path}
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response else "?"
        return JSONResponse({"error": f"HTTP {code}", "path": []})
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:100], "path": []})


@app.get("/")
def serve_index():
    return FileResponse("index.html")


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"  Flight tracker → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
