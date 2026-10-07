#!/usr/bin/env python3
"""Planes Overhead - alerts.

Watches the aircraft the logger sees and sends a phone notification (via the
ntfy app) for: aircraft circling nearby, traffic to or from Southend, and
interesting types (helicopters, military, warbirds).

    python3 planealert.py --test    send a sample notification
"""
import argparse
import collections
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from planelog import EARTH_RADIUS_MI, HERE, altitude_ft, distance_miles, load_config

USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) PlanesOverhead/1.0"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}\d[A-Z0-9]{0,4}$")  # e.g. EZY43FL, BAW889
SEND_PAUSE = 300        # after a failed notification, wait before trying again
LOOKUP_PAUSE = 600      # after a failed route lookup, wait before trying again
ROUTE_KEEP = 12 * 3600  # how long a looked-up route is remembered
FORGET_AFTER = 1800     # drop an aircraft's history after this long unseen
MAX_STEP = 30           # seconds between positions beyond which turning isn't measured
CONFIRMATIONS = 6       # flight-path matches in a row before a Southend alert
STEADY_DEGREES = 15     # ...during which its heading must stay within this

HELICOPTER_TYPES = {
    "A109", "A119", "A139", "A149", "A169", "A189", "AS32", "AS50", "AS55", "AS65",
    "B06", "B407", "B412", "B429", "B505", "EC20", "EC25", "EC30", "EC35", "EC45",
    "EC55", "EC75", "EH10", "EXPL", "G2CA", "GAZL", "H47", "H500", "H60", "H64",
    "LYNX", "MD52", "MD60", "PUMA", "R22", "R44", "R66", "S76", "S92",
}

DEFAULTS = {
    "enabled": True,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "quiet_hours": "22:00-07:00",   # "" for none
    "repeat_minutes": 60,           # silence per aircraft, per kind of alert
    "map_url": "",                  # opened when an alert is tapped; "" = this Pi's live map
    "circling": {
        "enabled": True,
        "degrees": 360,             # how much turning counts as circling
        "window_minutes": 15,
        "max_altitude_ft": 10000,
    },
    "airport": {
        "enabled": True,
        "name": "Southend",
        "iata": "SEN",
        "icao": "EGMC",
        "lat": 51.5714,
        "lon": 0.6956,
        "route_lookup": True,       # ask adsbdb.com for airline routes
        "lookup_below_ft": 12000,
        "arrival_below_ft": 5000,   # flight-path rule: low, descending, heading for it
        "departure_below_ft": 8000, # flight-path rule: climbing, heading away from it
        "within_degrees": 30,
    },
    "interesting": {
        "enabled": True,
        "helicopters": True,
        "military": True,
        "database_flagged": True,   # aircraft the database marks as noteworthy
        "types": ["SPIT", "HURI", "LANC", "B17", "P51", "DC3", "C47", "CAT", "DH82",
                  "DH89", "DHC1", "T6", "ST75", "YK52", "HUNT", "VAMP", "JPRO", "GNAT"],
    },
}


def log(msg):
    print(msg, flush=True)


def merged(defaults, user):
    out = {}
    for key, value in defaults.items():
        given = (user or {}).get(key)
        if isinstance(value, dict):
            out[key] = merged(value, given if isinstance(given, dict) else {})
        else:
            out[key] = value if given is None else given
    return out


def bearing(lat1, lon1, lat2, lon2):
    """Compass bearing in degrees from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def turn(from_deg, to_deg):
    """Signed change in heading, -180..180 (positive = clockwise)."""
    return (to_deg - from_deg + 180) % 360 - 180


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) % 360 // 45)]


def parse_quiet(text):
    """'22:00-07:00' -> (start, end) in minutes past midnight, or None."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not match:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def send_ntfy(settings, title, body, tags="airplane", click=""):
    """Returns (ok, detail)."""
    topic = str(settings.get("ntfy_topic") or "").strip()
    if not topic:
        return False, "no ntfy_topic set in config.yaml"
    url = str(settings.get("ntfy_server") or "https://ntfy.sh").rstrip("/") + "/" + topic
    headers = {
        "Title": title.encode("ascii", "replace").decode(),
        "Tags": tags,
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def lookup_route(callsign):
    """Airline route for a callsign.

    Returns {"origin": {...}, "destination": {...}}, or None if the database
    doesn't know the callsign. Raises OSError if the lookup itself failed.
    """
    url = ROUTE_URL.format(callsign=urllib.parse.quote(callsign))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise OSError(f"HTTP {exc.code}")
    except ValueError as exc:
        raise OSError(f"unreadable reply: {exc}")
    route = (data.get("response") or {}).get("flightroute") if isinstance(data, dict) else None
    if not isinstance(route, dict):
        return None

    def airport(info):
        info = info or {}
        return {"iata": info.get("iata_code") or "", "icao": info.get("icao_code") or "",
                "city": info.get("municipality") or info.get("name") or ""}

    return {"origin": airport(route.get("origin")),
            "destination": airport(route.get("destination"))}


class AlertEngine:
    def __init__(self, settings, home, send=send_ntfy, lookup=lookup_route, clock=time.time):
        self.cfg = merged(DEFAULTS, settings)
        self.home = home
        self.send = send
        self.lookup = lookup
        self.clock = clock
        self.quiet = parse_quiet(self.cfg["quiet_hours"])
        self.state = {}       # hex -> what we know about an aircraft in range
        self.sent = {}        # (hex, kind) -> when we last alerted
        self.routes = {}      # callsign -> (when looked up, route or None)
        self.send_after = 0
        self.lookup_after = 0
        self.next_tidy = 0
        self.map_url = str(self.cfg["map_url"] or "").strip() or (
            f"http://{socket.gethostname()}.local/tar1090/")

    # ------------------------------------------------------------ rules

    def _interesting(self, ac):
        rules = self.cfg["interesting"]
        if not rules["enabled"]:
            return None
        ac_type = (ac.get("t") or "").upper()
        flags = ac.get("dbFlags") or 0
        if rules["military"] and flags & 1:
            return "Military"
        if rules["helicopters"] and (ac.get("category") == "A7" or ac_type in HELICOPTER_TYPES):
            return "Helicopter"
        if ac_type and ac_type in {str(t).upper() for t in rules["types"] or []}:
            return "Interesting type"
        if rules["database_flagged"] and flags & 2:
            return "Noteworthy aircraft"
        return None

    def _circling(self, st, ac, now, alt):
        rules = self.cfg["circling"]
        track = ac.get("track")
        speed = ac.get("gs")
        usable = (rules["enabled"] and track is not None and alt and
                  alt <= rules["max_altitude_ft"] and (speed is None or speed >= 30))
        if not usable:
            st["turn_from"] = None
            return False
        if st.get("turn_from") is not None and now - st["turn_time"] <= MAX_STEP:
            st["turns"].append((now, turn(st["turn_from"], track)))
        st["turn_from"], st["turn_time"] = track, now
        while st["turns"] and now - st["turns"][0][0] > rules["window_minutes"] * 60:
            st["turns"].popleft()
        if abs(sum(step for _when, step in st["turns"])) >= rules["degrees"]:
            st["turns"].clear()
            return True
        return False

    def _climb_rate(self, st, ac, now, alt):
        """Feet per minute, from the aircraft if it says, else from recent heights."""
        for key in ("baro_rate", "geom_rate"):
            if isinstance(ac.get(key), (int, float)):
                return ac[key]
        history = st["heights"]
        if alt is not None:
            history.append((now, alt))
        while history and now - history[0][0] > 90:
            history.popleft()
        if len(history) >= 2 and history[-1][0] - history[0][0] >= 20:
            return (history[-1][1] - history[0][1]) / ((history[-1][0] - history[0][0]) / 60)
        return None

    def _route(self, callsign, now):
        """Looked-up route, None if unknown, or 'unavailable' if we couldn't ask."""
        cached = self.routes.get(callsign)
        if cached and now - cached[0] < ROUTE_KEEP:
            return cached[1]
        if now < self.lookup_after:
            return "unavailable"
        try:
            route = self.lookup(callsign)
        except OSError as exc:
            log(f"Route lookup unavailable ({exc}); using flight path only for now")
            self.lookup_after = now + LOOKUP_PAUSE
            return "unavailable"
        self.routes[callsign] = (now, route)
        return route

    def _airport(self, st, ac, now, alt, callsign):
        """Text describing a link to the airport, or None."""
        rules = self.cfg["airport"]
        if not rules["enabled"] or alt is None:
            return None
        name = rules["name"]

        if (rules["route_lookup"] and callsign and alt <= rules["lookup_below_ft"]
                and AIRLINE_CALLSIGN.match(callsign) and "route" not in st):
            route = self._route(callsign, now)
            if route != "unavailable":
                st["route"] = route
        route = st.get("route")
        if route:
            ends = (route["origin"], route["destination"])
            codes = {rules["iata"], rules["icao"]} - {""}
            if not any(end["iata"] in codes or end["icao"] in codes for end in ends):
                return None                      # a known route that isn't ours
            arriving = route["destination"]["iata"] in codes or route["destination"]["icao"] in codes
            cities = " to ".join(end["city"] or end["iata"] or "?" for end in ends)
            return (f"{name} arrival" if arriving else f"{name} departure", cities + ".")

        # no known route: judge by the flight path
        track = ac.get("track")
        rate = self._climb_rate(st, ac, now, alt)
        if track is None or rate is None or alt <= 0 or ac.get("lat") is None:
            st["path_hits"] = 0
            return None
        to_airport = bearing(ac["lat"], ac["lon"], rules["lat"], rules["lon"])
        limit = rules["within_degrees"]
        kind = None
        if alt <= rules["arrival_below_ft"] and rate <= -200 and abs(turn(track, to_airport)) <= limit:
            kind = "arrival"
        elif (alt <= rules["departure_below_ft"] and rate >= 300
              and abs(turn(track, (to_airport + 180) % 360)) <= limit):
            kind = "departure"
        # it has to hold a steady course at the airport, not just swing past that heading
        steady = (st["path_hits"] > 0 and kind == st.get("path_kind")
                  and abs(turn(st["path_track"], track)) <= STEADY_DEGREES)
        if not steady:
            st["path_hits"] = 0
            st["path_track"] = track
        st["path_kind"] = kind
        if kind is None:
            return None
        st["path_hits"] += 1
        if st["path_hits"] < CONFIRMATIONS:
            return None
        return (f"Probably {name} {kind}", "Judged from its flight path.")

    # ------------------------------------------------------------ main entry

    def observe(self, ac, now, dist):
        """Call for each fresh position of an aircraft in range."""
        if not self.cfg["enabled"]:
            return
        hex_id = (ac.get("hex") or "").strip()
        if not hex_id or ac.get("lat") is None:
            return
        st = self.state.get(hex_id)
        if st is None or now - st["seen"] > FORGET_AFTER:
            st = {"turns": collections.deque(), "heights": collections.deque(),
                  "turn_from": None, "turn_time": 0, "path_hits": 0}
            self.state[hex_id] = st
        st["seen"] = now
        alt = altitude_ft(ac)
        callsign = (ac.get("flight") or "").strip().upper()

        found = []   # (kind, title text, extra line)
        label = self._interesting(ac)
        if label:
            found.append(("interesting", label, ""))
        if self._circling(st, ac, now, alt):
            found.append(("circling", "Circling", ""))
        link = self._airport(st, ac, now, alt, callsign)
        if link:
            found.append(("airport", link[0], link[1]))

        wait = self.cfg["repeat_minutes"] * 60
        due = [f for f in found if now - self.sent.get((hex_id, f[0]), 0) >= wait]
        if due:
            for kind, _title, _extra in due:
                self.sent[(hex_id, kind)] = now
            self._notify(ac, hex_id, callsign, alt, dist, due, now)
        if now >= self.next_tidy:
            self._tidy(now, wait)

    def _notify(self, ac, hex_id, callsign, alt, dist, due, now):
        name = callsign or (ac.get("r") or hex_id).upper()
        ac_type = ac.get("t")
        labels = [t for _k, t, _e in due]
        for i in range(1, len(labels)):       # "Helicopter, circling", not "..., Circling"
            if labels[i] == "Circling" or labels[i].startswith("Probably"):
                labels[i] = labels[i][0].lower() + labels[i][1:]
        title = ", ".join(labels) + f": {name}" + (f" ({ac_type})" if ac_type else "")

        if alt is None:
            height = "height unknown"
        elif alt <= 100:
            height = "near ground"
        else:
            height = f"{alt:,} ft"
        where = compass(bearing(self.home[0], self.home[1], ac["lat"], ac["lon"]))
        parts = [p for p in (ac.get("r") if ac.get("r") and ac.get("r") != name else "",
                             height, f"{dist:.1f} miles {where} of home") if p]
        body = ", ".join(parts) + "."
        for _k, _t, extra in due:
            if extra:
                body += " " + extra
        tags = "helicopter" if any(t == "Helicopter" for _k, t, _e in due) else "airplane"

        if self._is_quiet(now):
            log(f"Alert held back (quiet hours): {title}")
            return
        if now < self.send_after:
            log(f"Alert not sent (notifications paused after a failure): {title}")
            return
        ok, detail = self.send(self.cfg, title, body, tags, f"{self.map_url}?icao={hex_id}")
        if ok:
            log(f"Alert sent: {title} - {body}")
        else:
            log(f"WARNING: alert not sent ({detail}): {title}")
            self.send_after = now + SEND_PAUSE

    def _is_quiet(self, now):
        if not self.quiet:
            return False
        start, end = self.quiet
        local = time.localtime(now)
        minute = local.tm_hour * 60 + local.tm_min
        if start == end:
            return False
        return start <= minute < end if start < end else (minute >= start or minute < end)

    def _tidy(self, now, wait):
        for hex_id in [h for h, st in self.state.items() if now - st["seen"] > FORGET_AFTER]:
            del self.state[hex_id]
        for key in [k for k, when in self.sent.items() if now - when > wait]:
            del self.sent[key]
        for callsign in [c for c, (when, _r) in self.routes.items() if now - when > ROUTE_KEEP]:
            del self.routes[callsign]
        self.next_tidy = now + 600


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Planes Overhead alerts")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    parser.add_argument("--test", action="store_true", help="send a sample notification")
    args = parser.parse_args()
    settings = merged(DEFAULTS, load_config(args.config).get("alerts"))
    if not args.test:
        parser.print_help()
        sys.exit(0)
    good, info = send_ntfy(settings, "Test: planes overhead",
                           "If you can read this on your phone, alerts are working.")
    print("Sent - check your phone." if good else f"Not sent: {info}")
    sys.exit(0 if good else 1)
