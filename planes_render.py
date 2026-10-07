#!/usr/bin/env python3
"""Planes Overhead - renderer.

Draws the last 24 hours of aircraft passes near home as a 1872x1404 PNG for a
TRMNL X and pushes it to a Webhook Image plugin.

    python3 planes_render.py              render and push
    python3 planes_render.py --no-push    render only (writes planes.png)
"""
import argparse
import json
import math
import os
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from PIL import Image, ImageChops, ImageDraw, ImageFont

from planelog import EARTH_RADIUS_MI, HERE, NEW_PASS_GAP, home_location, load_config

WIDTH, HEIGHT = 1872, 1404     # TRMNL X
MAP_SIZE = 1404                # square map panel on the left
SS = 2                         # draw at 2x and shrink, for smooth lines
WINDOW_HOURS = 24
MAX_UPLOAD_BYTES = 1_000_000   # TRMNL Webhook Image limit
NEAR_GROUND_FT = 100           # at or below this, show "near ground" not a figure
MAP_RETRY_SECONDS = 3600       # wait this long after a failed map download
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) PlanesOverhead/1.0"
# OpenStreetMap data servers, tried in turn until one answers
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# these servers turn away requests that don't say plainly what they are
MAP_USER_AGENTS = ["PlanesOverhead/1.0 (home Raspberry Pi display; one request)", USER_AGENT]

WHITE, BLACK = 255, 0
COAST_GREY, RING_GREY, PLACE_GREY, MUTED = 110, 170, 50, 90

# (upper altitude limit in feet, shade, line width in final pixels, legend label)
BANDS = [
    (3000, 0, 6, "below 3,000 ft"),
    (10000, 70, 4, "3,000 - 10,000 ft"),
    (25000, 150, 2.5, "10,000 - 25,000 ft"),
    (10 ** 9, 190, 2, "above 25,000 ft"),
]
LOW_BANDS = (1, 0)   # drawn over the coastline, lowest last
HIGH_BANDS = (3, 2)  # drawn under it, as background
UNKNOWN_BAND = len(BANDS) - 1  # no altitude reported: draw as the faintest

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
             "/usr/share/fonts/TTF"]
_fonts = {}


def log(msg):
    print(msg, flush=True)


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        chosen = None
        for folder in FONT_DIRS:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                chosen = ImageFont.truetype(path, size * SS)
                break
        if chosen is None:
            chosen = ImageFont.load_default(size * SS)
        _fonts[key] = chosen
    return _fonts[key]


def band_for(alt):
    if alt is None:
        return UNKNOWN_BAND
    for i, (limit, _shade, _width, _label) in enumerate(BANDS):
        if alt < limit:
            return i
    return UNKNOWN_BAND


# ---------------------------------------------------------------- map data

def fetch_map(lat, lon, half_miles):
    """Coastline and place names around home, from OpenStreetMap."""
    dlat = math.degrees(half_miles / EARTH_RADIUS_MI)
    dlon = dlat / math.cos(math.radians(lat))
    box = f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
    query = (
        "[out:json][timeout:60];("
        f'way["natural"="coastline"]({box});'
        f'node["place"~"^(town|village)$"]({box});'
        ");out geom;"
    )
    body = urllib.parse.urlencode({"data": query}).encode()
    elements, problems = None, []
    for agent in MAP_USER_AGENTS:
        for url in OVERPASS_URLS:
            host = urllib.parse.urlparse(url).netloc
            request = urllib.request.Request(url, data=body, headers={
                "User-Agent": agent,
                "Accept": "application/json, */*",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    elements = json.load(response)["elements"]
                log(f"Map data from {host}")
                break
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                problems.append(f"{host}: {exc}")
        if elements is not None:
            break
    if elements is None:
        raise OSError("; ".join(problems[-len(OVERPASS_URLS):]))

    coast, places = [], []
    for el in elements:
        tags = el.get("tags") or {}
        if el.get("type") == "way":
            line = [[p["lat"], p["lon"]] for p in el.get("geometry") or [] if p]
            if len(line) < 2:
                continue
            if tags.get("natural") == "coastline":
                coast.append(line)
        elif el.get("type") == "node" and tags.get("name"):
            places.append({"name": tags["name"], "kind": tags.get("place"),
                           "lat": el["lat"], "lon": el["lon"]})
    return {"coast": coast, "places": places}


def load_map(cache_path, lat, lon, half_miles, retry_now=False):
    """Cached map data; downloads it the first time (or if home has moved)."""
    key = [round(lat, 4), round(lon, 4), round(half_miles, 2)]
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    if cache.get("key") == key and "coast" in cache:
        return cache
    waiting = time.time() - cache.get("failed_at", 0) < MAP_RETRY_SECONDS
    if cache.get("key") == key and waiting and not retry_now:
        return None
    try:
        data = fetch_map(lat, lon, half_miles)
        data["key"] = key
        log(f"Map data downloaded: {len(data['coast'])} coastline sections, "
            f"{len(data['places'])} places")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        log(f"WARNING: map download failed ({exc}); drawing without coastline for now")
        data = {"key": key, "failed_at": time.time()}
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data if "coast" in data else None


# ---------------------------------------------------------------- flight data

def load_passes(db_path, since, radius):
    """Group stored positions into passes that came within `radius` miles."""
    if not os.path.exists(db_path):
        return []
    db = sqlite3.connect(db_path, timeout=30)
    rows = db.execute(
        "SELECT hex, ts, flight, lat, lon, alt, type, dist_mi FROM positions "
        "WHERE ts >= ? ORDER BY hex, ts", (since,)
    ).fetchall()
    db.close()

    passes, current = [], None
    for hex_id, ts, flight, lat, lon, alt, ac_type, dist in rows:
        if current is None or current["hex"] != hex_id or ts - current["end"] > NEW_PASS_GAP:
            current = {"hex": hex_id, "flight": None, "type": None, "points": [],
                       "start": ts, "end": ts}
            passes.append(current)
        current["end"] = ts
        current["flight"] = current["flight"] or flight
        current["type"] = current["type"] or ac_type
        current["points"].append((ts, lat, lon, alt, dist))

    kept = []
    for p in passes:
        inside = [pt for pt in p["points"] if pt[4] <= radius]
        if not inside:
            continue
        closest = min(inside, key=lambda pt: pt[4])
        airborne = [pt[3] for pt in inside if pt[3]]  # skips unknown and on-ground
        p["closest_mi"] = closest[4]
        p["time"] = closest[0]
        p["min_alt"] = min(airborne) if airborne else None
        p["label"] = p["flight"] or p["hex"].upper()
        kept.append(p)
    return kept


# ---------------------------------------------------------------- drawing

class Canvas:
    """Thin wrapper so all drawing is written in final-pixel units."""

    def __init__(self, image):
        self.image = image
        self.draw = ImageDraw.Draw(image)

    def line(self, points, shade, width):
        pts = [(x * SS, y * SS) for x, y in points]
        if len(pts) >= 2:
            self.draw.line(pts, fill=shade, width=max(1, round(width * SS)), joint="curve")

    def circle(self, cx, cy, r, outline=None, fill=None, width=1):
        self.draw.ellipse(
            [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS],
            outline=outline, fill=fill, width=max(1, round(width * SS)))

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, width=1):
        self.draw.rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], fill=fill,
                            outline=outline, width=max(1, round(width * SS)))

    def polygon(self, points, fill):
        self.draw.polygon([(x * SS, y * SS) for x, y in points], fill=fill)

    def text_size(self, text, fnt):
        left, top, right, bottom = self.draw.textbbox((0, 0), text, font=fnt)
        return (right - left) / SS, (bottom - top) / SS

    def text(self, x, y, text, fnt, shade=BLACK, anchor="la", halo=False):
        self.draw.text((x * SS, y * SS), text, font=fnt, fill=shade, anchor=anchor,
                       stroke_width=4 * SS if halo else 0, stroke_fill=WHITE)

    def fit(self, text, max_width, size, bold=True, min_size=20):
        """Largest font (down to min_size) at which text fits max_width."""
        while size > min_size and self.text_size(text, font(size, bold))[0] > max_width:
            size -= 2
        return font(size, bold)


def draw_map(base, home, radius, map_data, passes):
    canvas = Canvas(base)
    centre = MAP_SIZE / 2
    ring_px = MAP_SIZE * 0.455           # pixels from home to the outer ring
    scale = ring_px / radius             # pixels per mile
    home_lat, home_lon = home
    cos_lat = math.cos(math.radians(home_lat))
    mile_per_deg = math.radians(1) * EARTH_RADIUS_MI

    def xy(lat, lon):
        return (centre + (lon - home_lon) * mile_per_deg * cos_lat * scale,
                centre - (lat - home_lat) * mile_per_deg * scale)

    mask = Image.new("L", base.size, 0)
    Canvas(mask).circle(centre, centre, ring_px, fill=255)
    blank = Image.new("L", base.size, WHITE)

    def draw_tracks(bands):
        """Draw these height bands and merge them in, clipped to the outer ring."""
        layer = Image.new("L", base.size, WHITE)
        tracks = Canvas(layer)
        for band in bands:
            _limit, shade, width, _label = BANDS[band]
            for p in passes:
                run, prev = [], None
                for _ts, lat, lon, alt, _dist in p["points"]:
                    here = xy(lat, lon)
                    if band_for(alt) == band:
                        if not run and prev is not None:
                            run.append(prev)
                        run.append(here)
                    else:
                        if len(run) >= 2:
                            tracks.line(run, shade, width)
                        run = []
                    prev = here
                if len(run) >= 2:
                    tracks.line(run, shade, width)
        base.paste(ImageChops.darker(base, Image.composite(layer, blank, mask)))

    # back to front: high traffic, coastline, low traffic, range rings
    coast = [[xy(a, b) for a, b in line] for line in (map_data or {}).get("coast", [])]
    draw_tracks(HIGH_BANDS)
    for line in coast:
        canvas.line(line, WHITE, 9)      # white edging keeps it readable over traffic
    for line in coast:
        canvas.line(line, COAST_GREY, 3)
    draw_tracks(LOW_BANDS)

    rings = sorted({r for r in (1, 3, 5) if r < radius} | {radius})
    for r in rings:
        outer = r == radius
        canvas.circle(centre, centre, r * scale, outline=BLACK if outer else MUTED,
                      width=3 if outer else 1.5)

    # labels on top of everything
    placed = [(centre - 40, centre - 40, centre + 40, centre + 40)]

    def try_label(x, y, text, fnt, shade, anchor="mm"):
        w, h = canvas.text_size(text, fnt)
        if anchor == "mm":
            box = (x - w / 2 - 4, y - h / 2 - 4, x + w / 2 + 4, y + h / 2 + 4)
        else:
            box = (x - 4, y - h / 2 - 4, x + w + 4, y + h / 2 + 4)
        if box[0] < 8 or box[1] < 8 or box[2] > MAP_SIZE - 8 or box[3] > MAP_SIZE - 8:
            return False
        for other in placed:
            if box[0] < other[2] and box[2] > other[0] and box[1] < other[3] and box[3] > other[1]:
                return False
        canvas.text(x, y, text, fnt, shade, anchor=anchor, halo=True)
        placed.append(box)
        return True

    for r in rings:  # ring distances, up the north-east diagonal
        d = r * scale * math.sqrt(0.5)
        try_label(centre + d + 6, centre - d - 6, f"{r:g} mi", font(22, True), MUTED, "lm")

    if map_data:
        towns_first = sorted(map_data.get("places", []), key=lambda pl: pl["kind"] != "town")
        for place in towns_first:
            x, y = xy(place["lat"], place["lon"])
            is_town = place["kind"] == "town"
            try_label(x, y, place["name"], font(26 if is_town else 22, True), PLACE_GREY)

    # home marker and north arrow
    canvas.circle(centre, centre, 15, fill=WHITE)
    canvas.circle(centre, centre, 11, fill=BLACK)
    canvas.circle(centre, centre, 4, fill=WHITE)
    nx, ny = 60, 110
    canvas.polygon([(nx, ny - 56), (nx - 15, ny - 14), (nx, ny - 24), (nx + 15, ny - 14)], BLACK)
    canvas.text(nx, ny + 4, "N", font(30, True), BLACK, anchor="mm")


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def feet(alt):
    if alt is None:
        return "height unknown"
    # heights come from air pressure, uncorrected for the day's weather, so an
    # aircraft close to the ground can report a small or even negative figure
    if alt <= NEAR_GROUND_FT:
        return "near ground"
    return f"{alt:,} ft"


def draw_panel(base, radius, passes, now):
    canvas = Canvas(base)
    x0, x1 = MAP_SIZE + 36, WIDTH - 40
    width = x1 - x0
    canvas.line([(MAP_SIZE, 0), (MAP_SIZE, HEIGHT)], BLACK, 3)

    canvas.text(x0, 40, "PLANES", font(52, True))
    canvas.text(x0, 98, "OVERHEAD", font(52, True))
    canvas.text(x0, 168, f"within {radius:g} miles, last {WINDOW_HOURS} hours",
                font(23), MUTED)
    canvas.line([(x0, 212), (x1, 212)], BLACK, 3)

    # headline count
    count = len(passes)
    canvas.text(x0, 226, f"{count:,}", canvas.fit(f"{count:,}", width, 150), BLACK)
    canvas.text(x0, 392, "pass" if count == 1 else "passes", font(30, True), MUTED)

    def block(y, title, p, detail):
        canvas.text(x0, y, title, font(22, True), MUTED)
        if p is None:
            canvas.text(x0, y + 34, "none", font(34, True), MUTED)
            return
        name = p["label"] + (f"  {p['type']}" if p["type"] else "")
        canvas.text(x0, y + 32, name, canvas.fit(name, width, 44))
        canvas.text(x0, y + 90, detail, font(26), BLACK)

    with_alt = [p for p in passes if p["min_alt"] is not None]
    lowest = min(with_alt, key=lambda p: p["min_alt"]) if with_alt else None
    latest = max(passes, key=lambda p: p["time"]) if passes else None
    canvas.line([(x0, 452), (x1, 452)], RING_GREY, 2)
    block(470, "LOWEST", lowest,
          f"{feet(lowest['min_alt'])} at {clock(lowest['time'])}" if lowest else "")
    canvas.line([(x0, 612), (x1, 612)], RING_GREY, 2)
    block(630, "MOST RECENT", latest,
          f"{feet(latest['min_alt'])} at {clock(latest['time'])}" if latest else "")
    canvas.line([(x0, 772), (x1, 772)], RING_GREY, 2)

    # passes per hour, oldest on the left, current hour on the right
    canvas.text(x0, 790, "PASSES PER HOUR", font(22, True), MUTED)
    this_hour = int(now // 3600)
    counts = [0] * WINDOW_HOURS
    for p in passes:
        slot = WINDOW_HOURS - 1 - (this_hour - int(p["time"] // 3600))
        if 0 <= slot < WINDOW_HOURS:
            counts[slot] += 1
    top, bottom = 850, 1040
    peak = max(counts) or 1
    step = width / WINDOW_HOURS
    canvas.text(x1, 790, f"peak {max(counts)}", font(22), MUTED, anchor="ra")
    for i, n in enumerate(counts):
        bx = x0 + i * step
        if n:
            canvas.rect(bx + 2, bottom - max(3, (bottom - top) * n / peak), bx + step - 2,
                        bottom, fill=BLACK)
        hour = time.localtime((this_hour - (WINDOW_HOURS - 1 - i)) * 3600).tm_hour
        if hour % 6 == 0:
            canvas.line([(bx + step / 2, bottom + 2), (bx + step / 2, bottom + 10)], BLACK, 2)
            canvas.text(bx + step / 2, bottom + 14, f"{hour:02d}", font(20), MUTED, anchor="ma")
    canvas.line([(x0, bottom), (x1, bottom)], BLACK, 2)

    # altitude key
    canvas.text(x0, 1100, "HEIGHT", font(22, True), MUTED)
    for i, (_limit, shade, line_width, label) in enumerate(BANDS):
        y = 1150 + i * 40
        canvas.line([(x0, y), (x0 + 80, y)], shade, line_width)
        canvas.text(x0 + 100, y, label, font(23), BLACK, anchor="lm")

    canvas.text(x0, HEIGHT - 44, time.strftime("Updated %a %H:%M", time.localtime(now)),
                font(22), MUTED)


def render(cfg, now=None, retry_map=False):
    now = now or time.time()
    home = home_location(cfg)
    radius = float(cfg["radius_miles"])
    passes = load_passes(cfg["database"], int(now) - WINDOW_HOURS * 3600, radius)
    map_data = load_map(os.path.join(HERE, "map_cache.json"), home[0], home[1], radius * 1.25,
                        retry_now=retry_map)

    base = Image.new("L", (WIDTH * SS, HEIGHT * SS), WHITE)
    draw_map(base, home, radius, map_data, passes)
    Canvas(base).rect(MAP_SIZE, 0, WIDTH, HEIGHT, fill=WHITE)  # clear anything that strayed
    draw_panel(base, radius, passes, now)

    image = base.resize((WIDTH, HEIGHT), Image.LANCZOS)
    # snap to the display's 16 grey levels (also keeps the file small)
    return image.point([round(v / 17) * 17 for v in range(256)]), len(passes)


def save_png_4bit(image, path):
    """Write a 4-bit, 16-level greyscale PNG - the format a TRMNL X requires.

    Pillow can only write 8-bit greyscale, so the file is assembled here.
    """
    width, height = image.size
    if width % 2:
        raise ValueError("image width must be even")
    levels = image.point([min(15, (v + 8) // 17) for v in range(256)]).tobytes()
    half = (width // 2, height)
    left = Image.frombytes("L", half, levels[0::2]).point([v * 16 for v in range(256)])
    right = Image.frombytes("L", half, levels[1::2])
    packed = ImageChops.add(left, right).tobytes()  # two pixels per byte
    row = width // 2
    raw = b"".join(b"\x00" + packed[i:i + row] for i in range(0, len(packed), row))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 4, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def push(path, url):
    with open(path, "rb") as f:
        body = f.read()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "image/png", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Planes Overhead renderer")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--output", default=os.path.join(HERE, "planes.png"))
    parser.add_argument("--no-push", action="store_true", help="render only")
    parser.add_argument("--refresh-map", action="store_true",
                        help="retry the map download now instead of waiting")
    args = parser.parse_args()
    config = load_config(args.config)

    picture, total = render(config, retry_map=args.refresh_map)
    save_png_4bit(picture, args.output)
    size_kb = os.path.getsize(args.output) / 1024
    log(f"Rendered {total} passes to {args.output} ({size_kb:.0f} KB)")

    webhook = ((config.get("trmnl") or {}).get("webhook_url") or "").strip()
    if args.no_push:
        sys.exit(0)
    if not webhook:
        log("No trmnl.webhook_url in config.yaml - image saved but not pushed.")
        sys.exit(0)
    if os.path.getsize(args.output) > MAX_UPLOAD_BYTES:
        log("WARNING: image is over TRMNL's 1 MB limit - not pushed.")
        sys.exit(1)
    status, detail = push(args.output, webhook)
    if status is not None and 200 <= status < 300:
        log(f"Pushed to TRMNL (HTTP {status}).")
    else:
        log(f"WARNING: push to TRMNL failed (HTTP {status}) {detail}")
        sys.exit(1)
