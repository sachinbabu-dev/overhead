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
import socket
import threading
import time

import requests
import urllib3.util.connection as _urllib3_cn
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

# Railway's default outbound traffic uses IPv6, which OpenSky blocks.
# Force all outbound connections to use IPv4.
_urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

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
SAT_POLL_INTERVAL     = int(os.getenv("SAT_POLL_INTERVAL",  "60"))
SAT_MIN_ELEVATION     = float(os.getenv("SAT_MIN_ELEVATION", "0"))

print(f"[config] LAT={LAT} LON={LON} RADIUS={RADIUS} POLL_INTERVAL={POLL_INTERVAL}")
print(f"[config] OPENSKY_CLIENT_ID={'SET' if OPENSKY_CLIENT_ID else 'NOT SET'}")
print(f"[config] OPENSKY_CLIENT_SECRET={'SET' if OPENSKY_CLIENT_SECRET else 'NOT SET'}")

OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")

# adsb.lol — free community ADS-B API, no auth, works from cloud IPs
# Radius env var is in degrees; adsb.lol expects nautical miles (1° ≈ 60 nm)
ADSBEXCHANGE_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"

# ==============================================================================
# TOKEN MANAGEMENT  (kept for /api/flightinfo and /api/track which use OpenSky)
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

_sat_lock      = threading.Lock()
_sat_cache     = {"satellites": [], "fetched_at": 0.0, "error": "", "tle_count": 0}
_tle_sats: list = []
_tle_refreshed = 0.0


def fetch_loop():
    dist_nm = max(1, int(RADIUS * 60))  # degrees → nautical miles
    url = ADSBEXCHANGE_URL.format(lat=LAT, lon=LON, dist=dist_nm)
    while True:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            aircraft = resp.json().get("ac") or []

            planes = []
            for a in aircraft:
                if a.get("lat") is None or a.get("lon") is None:
                    continue
                alt_baro  = a.get("alt_baro")
                on_ground = alt_baro == "ground"
                alt_m     = None if (on_ground or alt_baro is None) else alt_baro * 0.3048
                gs        = a.get("gs")
                callsign  = (a.get("flight") or "").strip() or a.get("hex") or "????"
                planes.append({
                    "icao":      (a.get("hex") or "????").upper(),
                    "callsign":  callsign,
                    "lat":       a["lat"],
                    "lon":       a["lon"],
                    "alt_m":     alt_m,
                    "on_ground": on_ground,
                    "speed_ms":  gs / 1.944 if gs is not None else None,
                    "track":     a.get("track"),
                    "category":  a.get("category"),
                })

            with _lock:
                _cache["planes"]     = planes
                _cache["fetched_at"] = time.time()
                _cache["error"]      = ""

        except requests.exceptions.Timeout:
            with _lock: _cache["error"] = "API timeout — retrying"
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response else "?"
            with _lock: _cache["error"] = f"HTTP {code}"
        except requests.exceptions.ConnectionError:
            with _lock: _cache["error"] = "No network connection"
        except Exception as exc:
            with _lock: _cache["error"] = str(exc)[:100]

        time.sleep(POLL_INTERVAL)


threading.Thread(target=fetch_loop, daemon=True).start()


# ==============================================================================
# SATELLITE TRACKING  (Celestrak TLE + skyfield)
# ==============================================================================

def _load_tles(ts):
    global _tle_sats, _tle_refreshed
    try:
        from skyfield.api import EarthSatellite
        resp = requests.get(
            "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle",
            timeout=20,
        )
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
        sats = []
        for i in range(0, len(lines) - 2, 3):
            try:
                sats.append(EarthSatellite(lines[i + 1], lines[i + 2], lines[i], ts))
            except Exception:
                pass
        _tle_sats = sats
        _tle_refreshed = time.time()
        print(f"[satellites] loaded {len(sats)} TLEs from Celestrak")
    except Exception as exc:
        print(f"[satellites] TLE fetch error: {exc}")


def sat_fetch_loop():
    global _tle_sats, _tle_refreshed
    try:
        from skyfield.api import load, wgs84
    except ImportError:
        print("[satellites] skyfield not installed — satellite tracking disabled")
        return

    ts       = load.timescale()
    observer = wgs84.latlon(LAT, LON)

    _load_tles(ts)

    while True:
        try:
            if time.time() - _tle_refreshed > 3600:
                _load_tles(ts)

            if not _tle_sats:
                time.sleep(60)
                continue

            t        = ts.now()
            overhead = []

            for sat in _tle_sats:
                try:
                    diff = sat - observer
                    topo = diff.at(t)
                    alt, az, dist = topo.altaz()

                    if alt.degrees < SAT_MIN_ELEVATION:
                        continue

                    sub = sat.at(t).subpoint()
                    overhead.append({
                        "name":          sat.name.strip(),
                        "norad_id":      int(sat.model.satnum),
                        "lat":           round(sub.latitude.degrees,  4),
                        "lon":           round(sub.longitude.degrees, 4),
                        "alt_km":        round(sub.elevation.km,      0),
                        "elevation_deg": round(alt.degrees,            1),
                        "azimuth_deg":   round(az.degrees,             1),
                        "range_km":      round(dist.km,                0),
                    })
                except Exception:
                    pass

            with _sat_lock:
                _sat_cache["satellites"] = overhead
                _sat_cache["fetched_at"] = time.time()
                _sat_cache["error"]      = ""
                _sat_cache["tle_count"]  = len(_tle_sats)

        except Exception as exc:
            with _sat_lock:
                _sat_cache["error"] = str(exc)[:100]

        time.sleep(SAT_POLL_INTERVAL)


threading.Thread(target=sat_fetch_loop, daemon=True).start()

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


@app.get("/api/satellites")
def api_satellites():
    with _sat_lock:
        return {
            "satellites": _sat_cache["satellites"],
            "fetched_at": _sat_cache["fetched_at"],
            "error":      _sat_cache["error"],
            "tle_count":  _sat_cache["tle_count"],
        }


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
