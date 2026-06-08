#!/usr/bin/env python3
"""
flight_tracker.py  —  Radar-style overhead flight display
==========================================================
Fetches live aircraft positions from OpenSky Network and renders them
as a fullscreen ceiling-projection-friendly radar display.

=== QUICK START ===

  pip install -r requirements.txt
  python flight_tracker.py

  Click a blip to select it — press SPACE to deselect — ESC to quit.

=== RASPBERRY PI NOTES ===

  pygame is usually pre-installed on Raspberry Pi OS. If not:
      sudo apt install python3-pygame

  For headless projector boot, you may also need:
      sudo apt install xserver-xorg xinit

=== AUTO-START ON BOOT (Raspberry Pi, systemd) ===

  Create /etc/systemd/system/flighttracker.service :

    [Unit]
    Description=Flight Tracker Display
    After=network-online.target graphical.target
    Wants=network-online.target

    [Service]
    User=pi
    Environment=DISPLAY=:0
    WorkingDirectory=/home/pi/flight_tracker
    ExecStart=/usr/bin/python3 /home/pi/flight_tracker/flight_tracker.py
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=graphical.target

  Then enable it:
    sudo systemctl daemon-reload
    sudo systemctl enable flighttracker
    sudo systemctl start flighttracker
"""

import math
import os
import sys
import threading
import time
from datetime import datetime

import pygame
import requests
from dotenv import load_dotenv

load_dotenv()


# ==============================================================================
# CONFIG
# ==============================================================================

LAT                   = float(os.getenv("LAT",            "51.5074"))
LON                   = float(os.getenv("LON",            "-0.1278"))
RADIUS                = float(os.getenv("RADIUS",         "0.5"))
OPENSKY_CLIENT_ID     = os.getenv("OPENSKY_CLIENT_ID",    "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET","")
POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL",    "15"))
TARGET_FPS            = int(os.getenv("TARGET_FPS",       "30"))


# ==============================================================================
# COLOUR PALETTE  (designed for dark-room / ceiling projection)
# ==============================================================================

BG           = (  6,  10,  20)   # deep navy background
RING         = (  0,  55,  80)   # radar range rings
RING_LABEL   = ( 30,  90, 110)   # ring distance labels
CROSSHAIR    = (  0,  40,  60)   # N/S/E/W crosshair lines
WHITE        = (255, 255, 255)
DIM          = (120, 140, 165)   # secondary / label text
ACCENT       = (  0, 210, 170)   # teal — YOU marker, selected aircraft
PANEL_BG     = (  8,  16,  32)   # detail panel background
PANEL_BORDER = (  0, 150, 120)   # detail panel border
ERROR_COL    = (255,  80,  60)   # error status text
SWEEP_COL    = (  0, 200, 100)   # radar sweep leading edge
GREY         = ( 75,  80,  90)   # on-ground aircraft


def altitude_colour(alt_m):
    """Low = warm red/orange → cruise = cool cyan/blue."""
    if alt_m is None: return GREY
    if alt_m < 1500:  return (255,  80,  60)
    if alt_m < 4000:  return (255, 175,  45)
    if alt_m < 8000:  return (150, 255,  70)
    if alt_m < 11000: return ( 60, 210, 255)
    return                   (120, 140, 255)


# ==============================================================================
# SHARED STATE  (fetch thread writes; render loop reads under _lock)
# ==============================================================================

_lock        = threading.Lock()
_aircraft    = []    # list of plane dicts, replaced atomically each poll
_history     = {}    # icao -> [(lat, lon), …]  max 15 entries
_first_seen  = {}    # icao -> epoch float, for entrance pulse
_last_fetch  = 0.0
_fetch_error = ""


# ==============================================================================
# OPENSKY API  (OAuth2 client-credentials flow)
# ==============================================================================

OPENSKY_URL       = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")

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
    _token_expiry = time.time() + data.get("expires_in", 300) - 30
    return _token


def get_auth_headers():
    if not OPENSKY_CLIENT_ID:
        return {}
    if time.time() >= _token_expiry:
        fetch_token()
    return {"Authorization": f"Bearer {_token}"}


def fetch_loop():
    global _last_fetch, _fetch_error

    while True:
        try:
            params = {
                "lamin": LAT - RADIUS,
                "lamax": LAT + RADIUS,
                "lomin": LON - RADIUS,
                "lomax": LON + RADIUS,
            }
            response = requests.get(
                OPENSKY_URL, params=params, headers=get_auth_headers(), timeout=10
            )
            response.raise_for_status()

            states = response.json().get("states") or []
            planes = parse_states(states)

            with _lock:
                now           = time.time()
                current_icaos = {p["icao"] for p in planes}

                # Prune history for planes that left the area
                for icao in list(_history):
                    if icao not in current_icaos:
                        del _history[icao]

                for plane in planes:
                    icao = plane["icao"]
                    if icao not in _first_seen:
                        _first_seen[icao] = now
                    if icao not in _history:
                        _history[icao] = []
                    hist = _history[icao]
                    pos  = (plane["lat"], plane["lon"])
                    if not hist or hist[-1] != pos:
                        hist.append(pos)
                        if len(hist) > 15:
                            hist.pop(0)

                _aircraft[:] = planes
                _last_fetch   = now
                _fetch_error  = ""

        except requests.exceptions.Timeout:
            with _lock: _fetch_error = "API timeout — retrying"
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response else "?"
            with _lock: _fetch_error = f"HTTP {code} error"
        except requests.exceptions.ConnectionError:
            with _lock: _fetch_error = "No network connection"
        except Exception as exc:
            with _lock: _fetch_error = str(exc)[:50]

        time.sleep(POLL_INTERVAL)


def parse_states(states):
    planes = []
    for s in states:
        lon, lat = s[5], s[6]
        if lon is None or lat is None:
            continue
        callsign = (s[1] or "").strip() or s[0] or "????"
        planes.append({
            "icao":      s[0] or "????",
            "callsign":  callsign,
            "lat":       lat,
            "lon":       lon,
            "alt_m":     s[7],
            "on_ground": bool(s[8]),
            "speed_ms":  s[9],
            "track":     s[10],
        })
    return planes


# ==============================================================================
# COORDINATE HELPERS
# ==============================================================================

def geo_to_screen(plane_lat, plane_lon, cx, cy, px_per_deg):
    dlat = plane_lat - LAT
    dlon = (plane_lon - LON) * math.cos(math.radians(LAT))
    return cx + int(dlon * px_per_deg), cy - int(dlat * px_per_deg)


def ms_to_knots(speed_ms):
    return round(speed_ms * 1.944) if speed_ms is not None else None


def compass_point(degrees):
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[int((degrees + 11.25) / 22.5) % 16]


# ==============================================================================
# DRAWING
# ==============================================================================

def draw_sweep(surface, cx, cy, radius_px, angle_deg):
    """Cosmetic rotating radar sweep — fading green wedge."""
    for offset in range(28):
        a = math.radians(angle_deg - offset)
        ex = cx + int(math.sin(a) * radius_px)
        ey = cy - int(math.cos(a) * radius_px)
        intensity = max(0, 68 - offset * 2)
        pygame.draw.line(surface, (0, intensity, intensity // 3), (cx, cy), (ex, ey), 1)
    a  = math.radians(angle_deg)
    ex = cx + int(math.sin(a) * radius_px)
    ey = cy - int(math.cos(a) * radius_px)
    pygame.draw.line(surface, SWEEP_COL, (cx, cy), (ex, ey), 2)


def draw_radar_rings(surface, cx, cy, px_per_deg, font):
    step = 0.25 if RADIUS <= 0.6 else 0.5
    r    = step
    while r <= RADIUS + 0.01:
        r_px = int(r * px_per_deg)
        pygame.draw.circle(surface, RING, (cx, cy), r_px, 1)
        km  = int(r * 111)
        lbl = font.render(f"{km}km", True, RING_LABEL)
        surface.blit(lbl, (cx + r_px + 3, cy - lbl.get_height() // 2))
        r = round(r + step, 2)

    reach = int(RADIUS * px_per_deg)
    pygame.draw.line(surface, CROSSHAIR, (cx, cy - reach), (cx, cy + reach), 1)
    pygame.draw.line(surface, CROSSHAIR, (cx - reach, cy), (cx + reach, cy), 1)

    margin = reach + 16
    for text, dx, dy in [("N", 0, -margin), ("S", 0, margin),
                         ("E", margin, 0),  ("W", -margin, 0)]:
        s = font.render(text, True, DIM)
        surface.blit(s, (cx + dx - s.get_width() // 2, cy + dy - s.get_height() // 2))


def draw_self_marker(surface, cx, cy, pulse_r, font):
    """YOU marker with an animated expanding pulse ring."""
    if pulse_r > 0:
        pygame.draw.circle(surface, ACCENT, (cx, cy), pulse_r, 1)
    pygame.draw.circle(surface, ACCENT, (cx, cy), 6, 2)
    pygame.draw.circle(surface, ACCENT, (cx, cy), 2)
    lbl = font.render("YOU", True, ACCENT)
    surface.blit(lbl, (cx + 10, cy - lbl.get_height() // 2))


def draw_aircraft_icon(surface, px, py, track_deg, colour, selected=False):
    """Arrow-shaped icon that rotates with heading. Circle fallback if no track."""
    size = 9
    if selected:
        pygame.draw.circle(surface, ACCENT, (px, py), size + 7, 1)

    if track_deg is None:
        pygame.draw.circle(surface, colour, (px, py), 4)
        return

    rad  = math.radians(track_deg)
    # Nose tip
    tx   = px + int(math.sin(rad) * size)
    ty   = py - int(math.cos(rad) * size)
    # Back-left wing
    lx   = px + int(math.sin(rad + math.radians(148)) * size * 0.65)
    ly   = py - int(math.cos(rad + math.radians(148)) * size * 0.65)
    # Back-right wing
    rx   = px + int(math.sin(rad - math.radians(148)) * size * 0.65)
    ry   = py - int(math.cos(rad - math.radians(148)) * size * 0.65)
    # Tail notch
    bx   = px + int(math.sin(rad + math.radians(180)) * size * 0.35)
    by   = py - int(math.cos(rad + math.radians(180)) * size * 0.35)

    pygame.draw.polygon(surface, colour, [(tx, ty), (lx, ly), (bx, by), (rx, ry)])


def draw_trail(surface, positions, cx, cy, px_per_deg, colour):
    """Fading position history trail — oldest positions are darkest."""
    if len(positions) < 2:
        return
    n    = len(positions)
    r, g, b = colour
    for i in range(1, n):
        f  = (i / n) * 0.55
        c  = (int(r * f), int(g * f), int(b * f))
        p1 = geo_to_screen(positions[i - 1][0], positions[i - 1][1], cx, cy, px_per_deg)
        p2 = geo_to_screen(positions[i][0],     positions[i][1],     cx, cy, px_per_deg)
        pygame.draw.line(surface, c, p1, p2, 1)


def draw_plane(surface, plane, history, cx, cy, px_per_deg, font,
               selected=False, first_seen=0.0):
    px, py  = geo_to_screen(plane["lat"], plane["lon"], cx, cy, px_per_deg)
    dist_px = math.hypot(px - cx, py - cy)
    if dist_px > RADIUS * px_per_deg * 1.08:
        return

    colour = GREY if plane["on_ground"] else altitude_colour(plane["alt_m"])

    if not plane["on_ground"] and history:
        draw_trail(surface, history, cx, cy, px_per_deg, colour)

    # Entrance pulse: expanding ring that fades over 3 seconds
    age = time.time() - first_seen
    if age < 3.0:
        pulse_r   = int((age / 3.0) * 30)
        fade      = 1.0 - age / 3.0
        pulse_col = tuple(int(c * fade) for c in colour)
        pygame.draw.circle(surface, pulse_col, (px, py), pulse_r, 1)

    draw_aircraft_icon(surface, px, py, plane["track"], colour, selected)

    label_col = ACCENT if selected else colour
    lbl       = font.render(plane["callsign"], True, label_col)
    surface.blit(lbl, (px + 13, py - lbl.get_height() // 2))


def draw_detail_panel(surface, plane, W, H, font_lg, font_md, font_sm):
    """Right-side info panel shown when an aircraft is selected."""
    PW, PH = 268, 300
    ox     = W - PW - 18
    oy     = H // 2 - PH // 2

    panel = pygame.Surface((PW, PH))
    panel.fill(PANEL_BG)
    pygame.draw.rect(panel, PANEL_BORDER, (0, 0, PW, PH), 1)

    # Top accent strip coloured by altitude
    strip_col = GREY if plane["on_ground"] else altitude_colour(plane["alt_m"])
    pygame.draw.rect(panel, strip_col, (0, 0, PW, 4))

    y = 16
    # Callsign — large
    cs = font_lg.render(plane["callsign"], True, WHITE)
    panel.blit(cs, (PW // 2 - cs.get_width() // 2, y))
    y += cs.get_height() + 4

    # ICAO hex — small, dimmed
    icao = font_sm.render(plane["icao"].upper(), True, DIM)
    panel.blit(icao, (PW // 2 - icao.get_width() // 2, y))
    y += icao.get_height() + 12

    pygame.draw.line(panel, PANEL_BORDER, (14, y), (PW - 14, y), 1)
    y += 11

    def row(label, value, val_col=WHITE):
        nonlocal y
        l = font_sm.render(label, True, DIM)
        v = font_md.render(value, True, val_col)
        panel.blit(l, (14, y))
        panel.blit(v, (PW - v.get_width() - 14, y))
        y += max(l.get_height(), v.get_height()) + 7

    status_text = "ON GROUND" if plane["on_ground"] else "AIRBORNE"
    row("STATUS", status_text, GREY if plane["on_ground"] else ACCENT)

    alt_m = plane["alt_m"]
    if alt_m is not None:
        row("ALTITUDE", f"{int(alt_m)}m  {int(alt_m * 3.281)}ft",
            altitude_colour(alt_m))
    else:
        row("ALTITUDE", "---", DIM)

    kt = ms_to_knots(plane["speed_ms"])
    row("SPEED", f"{kt} kt" if kt is not None else "---")

    if plane["track"] is not None:
        row("HEADING", f"{int(plane['track'])}°  {compass_point(plane['track'])}")
    else:
        row("HEADING", "---")

    # Altitude bar
    if alt_m is not None and not plane["on_ground"]:
        y += 4
        pygame.draw.line(panel, PANEL_BORDER, (14, y), (PW - 14, y), 1)
        y += 9
        bar_w = PW - 28
        fill  = min(1.0, alt_m / 13000)
        pygame.draw.rect(panel, RING, (14, y, bar_w, 6))
        pygame.draw.rect(panel, altitude_colour(alt_m), (14, y, int(bar_w * fill), 6))
        lo = font_sm.render("GND", True, DIM)
        hi = font_sm.render("FL430", True, DIM)
        panel.blit(lo, (14,                    y + 9))
        panel.blit(hi, (PW - hi.get_width() - 14, y + 9))

    surface.blit(panel, (ox, oy))


def draw_header(surface, aircraft, last_fetch_t, error_msg, font_title, font_sm, W):
    airborne = sum(1 for a in aircraft if not a["on_ground"])
    grounded = len(aircraft) - airborne

    title = font_title.render("OVERHEAD", True, ACCENT)
    surface.blit(title, (14, 10))

    parts = [f"{airborne} airborne"]
    if grounded:
        parts.append(f"{grounded} on ground")
    count = font_sm.render("  ·  ".join(parts), True, DIM)
    surface.blit(count, (14, 10 + title.get_height() + 2))

    if last_fetch_t:
        age   = int(time.time() - last_fetch_t)
        ts    = datetime.fromtimestamp(last_fetch_t).strftime("%H:%M:%S")
        right = f"updated {ts}  +{age}s"
    else:
        right = "awaiting first fetch…"
    if error_msg:
        right = f"[!] {error_msg}"

    right_col  = ERROR_COL if error_msg else DIM
    right_surf = font_sm.render(right, True, right_col)
    surface.blit(right_surf, (W - right_surf.get_width() - 14, 18))

    pygame.draw.line(surface, RING, (0, 54), (W, 54), 1)


def draw_legend(surface, sx, sy, font):
    entries = [
        (altitude_colour(12000), "> 11km  cruise"),
        (altitude_colour(9500),  "8 – 11 km"),
        (altitude_colour(6000),  "4 – 8 km"),
        (altitude_colour(2500),  "1.5 – 4 km"),
        (altitude_colour(500),   "< 1.5 km  low"),
        (GREY,                   "ground / no data"),
    ]
    hdr = font.render("ALTITUDE", True, DIM)
    surface.blit(hdr, (sx, sy))
    y = sy + hdr.get_height() + 5
    for colour, text in entries:
        pygame.draw.rect(surface, colour, (sx, y + 3, 8, 8))
        t = font.render(text, True, DIM)
        surface.blit(t, (sx + 13, y))
        y += t.get_height() + 3


def find_plane_at(mx, my, aircraft, cx, cy, px_per_deg, threshold=14):
    """Return ICAO of the closest plane to a screen click, or None."""
    best, best_dist = None, threshold
    for plane in aircraft:
        px, py = geo_to_screen(plane["lat"], plane["lon"], cx, cy, px_per_deg)
        d = math.hypot(mx - px, my - py)
        if d < best_dist:
            best, best_dist = plane["icao"], d
    return best


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    thread = threading.Thread(target=fetch_loop, daemon=True)
    thread.start()

    pygame.init()
    info   = pygame.display.Info()
    W, H   = info.current_w, info.current_h
    screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN)
    pygame.display.set_caption("Flight Tracker")
    pygame.mouse.set_visible(False)
    clock  = pygame.time.Clock()

    def load_font(name, size, bold=False):
        try:
            return pygame.font.SysFont(name, size, bold=bold)
        except Exception:
            return pygame.font.Font(None, size + 4)

    font_title = load_font("dejavusansmono", 24, bold=True)
    font_lg    = load_font("dejavusansmono", 20, bold=True)
    font_md    = load_font("dejavusansmono", 15)
    font_label = load_font("dejavusansmono", 13)
    font_sm    = load_font("dejavusansmono", 11)

    cx         = W // 2
    cy         = H // 2 + 24
    px_per_deg = int(min(W, H) * 0.44 / RADIUS)
    radius_px  = int(RADIUS * px_per_deg)

    sweep_angle   = 0.0
    pulse_t       = 0.0   # YOU-marker pulse phase (0–3s cycle)
    selected_icao = None

    while True:
        # ── Events ────────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit()
                if event.key == pygame.K_SPACE:
                    selected_icao = None
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                with _lock:
                    ac_snap = list(_aircraft)
                hit = find_plane_at(event.pos[0], event.pos[1], ac_snap,
                                    cx, cy, px_per_deg)
                selected_icao = hit if hit != selected_icao else None

        # ── Snapshot shared state ─────────────────────────────────────────────
        with _lock:
            aircraft        = list(_aircraft)
            history_snap    = {k: list(v) for k, v in _history.items()}
            first_seen_snap = dict(_first_seen)
            last_fetch_t    = _last_fetch
            error_msg       = _fetch_error

        # ── Animate ───────────────────────────────────────────────────────────
        sweep_angle = (sweep_angle + 360 / (TARGET_FPS * 4)) % 360
        pulse_t     = (pulse_t + 1 / TARGET_FPS) % 3.0

        # ── Draw ──────────────────────────────────────────────────────────────
        screen.fill(BG)

        draw_radar_rings(screen, cx, cy, px_per_deg, font_sm)
        draw_sweep(screen, cx, cy, radius_px, sweep_angle)

        pulse_r = int((pulse_t / 3.0) * 22) if pulse_t < 2.5 else 0
        draw_self_marker(screen, cx, cy, pulse_r, font_sm)

        selected_plane = next(
            (p for p in aircraft if p["icao"] == selected_icao), None
        )

        # Draw unselected planes first so selected renders on top
        for plane in aircraft:
            if plane["icao"] == selected_icao:
                continue
            draw_plane(screen, plane,
                       history_snap.get(plane["icao"], []),
                       cx, cy, px_per_deg, font_label,
                       selected=False,
                       first_seen=first_seen_snap.get(plane["icao"], time.time()))

        if selected_plane:
            draw_plane(screen, selected_plane,
                       history_snap.get(selected_plane["icao"], []),
                       cx, cy, px_per_deg, font_label,
                       selected=True,
                       first_seen=first_seen_snap.get(selected_plane["icao"], time.time()))
            draw_detail_panel(screen, selected_plane, W, H, font_lg, font_md, font_sm)

        draw_header(screen, aircraft, last_fetch_t, error_msg, font_title, font_sm, W)
        draw_legend(screen, W - 178, H - 128, font_sm)

        coord = (f"{abs(LAT):.4f}{'N' if LAT >= 0 else 'S'}  "
                 f"{abs(LON):.4f}{'E' if LON >= 0 else 'W'}  "
                 f"r={RADIUS}°")
        coord_surf = font_sm.render(coord, True, RING_LABEL)
        screen.blit(coord_surf, (14, H - coord_surf.get_height() - 10))

        # Click hint when something is selected
        if selected_icao:
            hint = font_sm.render("SPACE  deselect  ·  click another blip", True, RING_LABEL)
            screen.blit(hint, (W // 2 - hint.get_width() // 2, H - hint.get_height() - 10))

        pygame.display.flip()
        clock.tick(TARGET_FPS)


if __name__ == "__main__":
    main()
