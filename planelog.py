#!/usr/bin/env python3
"""Planes Overhead - logger.

Reads readsb's aircraft.json every few seconds and stores the positions of
aircraft near home in SQLite, ready for the TRMNL renderer to draw.

Run as a service (see install.sh), or by hand:
    python3 planelog.py            start logging
    python3 planelog.py --stats    summary of the last 24 hours
"""
import argparse
import json
import math
import os
import signal
import sqlite3
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
EARTH_RADIUS_MI = 3958.8
FRESH_SECONDS = 15    # ignore positions older than this
NEW_PASS_GAP = 300    # seconds unseen before a return counts as a new pass
STALE_FEED = 60       # warn if aircraft.json is older than this

DEFAULTS = {
    "home": {"lat": None, "lon": None},
    "radius_miles": 5,
    "log_margin_miles": 1,
    "poll_seconds": 5,
    "retain_hours": 48,
    "aircraft_json": "/run/readsb/aircraft.json",
    "receiver_json": "/run/readsb/receiver.json",
    "database": "planes.db",
}


def log(msg):
    print(msg, flush=True)


def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(value)
                cfg[key] = merged
            else:
                cfg[key] = value
    if not os.path.isabs(cfg["database"]):
        cfg["database"] = os.path.join(HERE, cfg["database"])
    return cfg


def home_location(cfg):
    """Home lat/lon from config, falling back to the location set in readsb."""
    lat, lon = cfg["home"].get("lat"), cfg["home"].get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    try:
        with open(cfg["receiver_json"]) as f:
            rx = json.load(f)
        return float(rx["lat"]), float(rx["lon"])
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(
            "No home location. Either run 'sudo readsb-set-location LAT LON' "
            "or set home.lat and home.lon in config.yaml."
        )


def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def altitude_ft(aircraft):
    alt = aircraft.get("alt_baro")
    if alt == "ground":
        return 0
    if isinstance(alt, (int, float)):
        return int(alt)
    alt = aircraft.get("alt_geom")
    return int(alt) if isinstance(alt, (int, float)) else None


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS positions (
               ts      INTEGER NOT NULL,  -- unix time of the position
               hex     TEXT    NOT NULL,  -- aircraft ID
               flight  TEXT,              -- callsign
               lat     REAL    NOT NULL,
               lon     REAL    NOT NULL,
               alt     INTEGER,           -- feet, 0 = on ground
               gs      REAL,              -- ground speed, knots
               track   REAL,              -- heading over ground, degrees
               type    TEXT,              -- e.g. A320 (if readsb supplies it)
               reg     TEXT,              -- registration (if supplied)
               dist_mi REAL    NOT NULL   -- distance from home
           )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions (ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_positions_hex_ts ON positions (hex, ts)")
    db.commit()
    return db


def read_feed(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def start_alerts(cfg, home):
    """The alert engine, or None if alerts aren't set up."""
    settings = cfg.get("alerts") or {}
    if not settings.get("enabled", True) or not str(settings.get("ntfy_topic") or "").strip():
        log("Alerts are off (no alerts.ntfy_topic in config.yaml).")
        return None
    try:
        from planealert import AlertEngine
    except ImportError as exc:
        log(f"Alerts are off (planealert.py not available: {exc}).")
        return None
    engine = AlertEngine(settings, home)
    quiet = engine.cfg["quiet_hours"] or "none"
    log(f"Alerts are on (quiet hours: {quiet}).")
    return engine


def run(cfg):
    home_lat, home_lon = home_location(cfg)
    radius = float(cfg["radius_miles"])
    limit = radius + float(cfg["log_margin_miles"])
    poll = max(1, int(cfg["poll_seconds"]))
    retain = int(cfg["retain_hours"]) * 3600
    db = open_db(cfg["database"])

    log(f"Logging aircraft within {limit:g} miles of home to {cfg['database']}")
    alerts = start_alerts(cfg, (home_lat, home_lon))

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_pos = {}    # hex -> (lat, lon) last stored
    last_seen = {}   # hex -> unix time last stored
    feed_ok = True
    next_prune = 0

    while running:
        started = time.time()
        feed = read_feed(cfg["aircraft_json"])
        now = feed.get("now", started) if feed else None

        if feed is None or started - now > STALE_FEED:
            if feed_ok:
                log(f"WARNING: no fresh data in {cfg['aircraft_json']} - is readsb running?")
                feed_ok = False
        else:
            if not feed_ok:
                log("Feed is back.")
                feed_ok = True
            rows = []
            for ac in feed.get("aircraft", []):
                lat, lon = ac.get("lat"), ac.get("lon")
                if lat is None or lon is None:
                    continue
                seen_pos = ac.get("seen_pos", 0)
                if seen_pos > FRESH_SECONDS:
                    continue
                dist = distance_miles(home_lat, home_lon, lat, lon)
                if dist > limit:
                    continue
                hex_id = (ac.get("hex") or "").strip()
                if not hex_id or last_pos.get(hex_id) == (lat, lon):
                    continue

                ts = int(now - seen_pos)
                flight = (ac.get("flight") or "").strip() or None
                alt = altitude_ft(ac)
                ac_type = ac.get("t")

                if ts - last_seen.get(hex_id, 0) > NEW_PASS_GAP:
                    label = flight or hex_id
                    extra = f" {ac_type}" if ac_type else ""
                    height = f"{alt} ft" if alt is not None else "alt unknown"
                    log(f"In range: {label}{extra}, {dist:.1f} mi, {height}")

                last_pos[hex_id] = (lat, lon)
                last_seen[hex_id] = ts
                if alerts:
                    try:
                        alerts.observe(ac, ts, dist)
                    except Exception as exc:  # an alert fault must never stop the logging
                        log(f"WARNING: alert check failed for {hex_id}: {exc!r}")
                rows.append(
                    (ts, hex_id, flight, lat, lon, alt, ac.get("gs"),
                     ac.get("track"), ac_type, ac.get("r"), round(dist, 2))
                )
            if rows:
                db.executemany(
                    "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
                )
                db.commit()

        if started >= next_prune:
            db.execute("DELETE FROM positions WHERE ts < ?", (int(started) - retain,))
            db.commit()
            for hex_id in [h for h, t in last_seen.items() if started - t > NEW_PASS_GAP]:
                last_seen.pop(hex_id, None)
                last_pos.pop(hex_id, None)
            next_prune = started + 3600

        # sleep in short steps so a stop request is acted on promptly
        deadline = started + poll
        while running and time.time() < deadline:
            time.sleep(0.2)

    db.close()
    log("Stopped.")


def stats(cfg):
    if not os.path.exists(cfg["database"]):
        sys.exit("No database yet - the logger hasn't stored anything.")
    radius = float(cfg["radius_miles"])
    since = int(time.time()) - 24 * 3600
    db = sqlite3.connect(cfg["database"])
    positions, aircraft = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT hex) FROM positions "
        "WHERE ts >= ? AND dist_mi <= ?", (since, radius)
    ).fetchone()
    print(f"Last 24 hours, within {radius:g} miles:")
    print(f"  {aircraft} aircraft, {positions} positions")
    lowest = db.execute(
        "SELECT flight, hex, type, alt, ts FROM positions "
        "WHERE ts >= ? AND dist_mi <= ? AND alt > 0 ORDER BY alt LIMIT 1",
        (since, radius),
    ).fetchone()
    latest = db.execute(
        "SELECT flight, hex, type, alt, ts FROM positions "
        "WHERE ts >= ? AND dist_mi <= ? ORDER BY ts DESC LIMIT 1",
        (since, radius),
    ).fetchone()
    for title, row in (("Lowest", lowest), ("Latest", latest)):
        if row:
            flight, hex_id, ac_type, alt, ts = row
            when = time.strftime("%a %H:%M", time.localtime(ts))
            height = f"{alt} ft" if alt is not None else "alt unknown"
            print(f"  {title}: {flight or hex_id} {ac_type or ''} {height} at {when}")
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Planes Overhead logger")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--stats", action="store_true",
                        help="print a summary of the last 24 hours and exit")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.stats:
        stats(config)
    else:
        run(config)
