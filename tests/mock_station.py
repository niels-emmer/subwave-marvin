#!/usr/bin/env python3
"""A local stand-in for a SUB/WAVE station's admin API. Standard library only.

It reproduces the semantics that make a write path dangerous, so `apply` can be
proven without touching a live station:

  GET  /api/health          public
  GET  /api/now-playing     public
  GET  /api/personas        public; TRIMMED to 4 fields per persona
  GET  /api/schedule        public; shows TRIMMED to 7 of 26 fields
  GET  /api/settings        admin; the 19-key UI summary, NOT settings.json
  POST /api/settings        admin; SHALLOW merge - a top-level key replaces wholesale
  POST /api/shows           admin; upsert by show.id, assigns an id when absent,
                            409 once the show cap is reached
  PUT  /api/schedule        admin; REPLACES the whole grid
  GET  /api/backup/export   admin; a zip WITHOUT schedule.json - the real bug

Admin routes require HTTP Basic auth and answer 401 without it.

Every request is recorded in `server.station.requests` as
{"method", "path", "body", "authorised"} so tests can assert on what was sent.

From a test:

    server = start_mock()
    ... requests against server.url ...
    server.stop()

From a shell:

    python3 tests/mock_station.py --port 8099
"""

import argparse
import base64
import copy
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "station.json"

# What /api/schedule exposes per show. The other 19 fields are not readable
# over HTTP, which is exactly why read-modify-write would destroy them.
PUBLIC_SHOW_FIELDS = ("id", "name", "topic", "personaId", "guestPersonaIds", "moods", "mood")

# What admin /api/settings exposes: a UI summary, not the config document.
SETTINGS_SUMMARY_KEYS = (
    "autoPick", "budget", "defaults", "embedding", "env", "jingles", "libraryStats",
    "llm", "navidrome", "ollama", "onAir", "pickerBusy", "search", "serverTimezone",
    "skills", "streamOnAir", "tagger", "tts", "values",
)


class Station:
    """In-memory station state plus the semantics of each route."""

    def __init__(self, settings, schedule, admin_user, admin_pass, max_shows=12):
        self.settings = copy.deepcopy(settings)
        self.schedule = copy.deepcopy(schedule)
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.max_shows = max_shows
        self.requests = []
        self._lock = threading.Lock()
        self._next_show = 1

    # -- bookkeeping -------------------------------------------------------
    def record(self, method, path, body, authorised):
        with self._lock:
            self.requests.append({
                "method": method,
                "path": path,
                "body": body,
                "authorised": authorised,
            })

    def new_show_id(self):
        with self._lock:
            value = "s_mock%02d" % self._next_show
            self._next_show += 1
            return value

    # -- reads -------------------------------------------------------------
    def public_schedule(self):
        shows = []
        for show in self.schedule.get("shows") or []:
            # Exactly the 7 fields the real /api/schedule exposes. "mood" is a
            # derived single value, not a field the show document carries.
            shows.append({
                "id": show.get("id"),
                "name": show.get("name"),
                "topic": show.get("topic"),
                "personaId": show.get("personaId"),
                "guestPersonaIds": show.get("guestPersonaIds") or [],
                "moods": show.get("moods") or [],
                "mood": (show.get("moods") or [None])[0],
            })
        return {
            "shows": shows,
            "schedule": self.schedule.get("schedule") or {},
            "override": self.schedule.get("override"),
            "personas": [{"id": p.get("id"), "name": p.get("name")}
                         for p in self.settings.get("personas") or []],
            "timezone": self.settings.get("timezone"),
            "locale": self.settings.get("locale"),
        }

    def public_personas(self):
        """The 4 fields /api/personas exposes, even to an admin."""
        return {"personas": [
            {"id": p.get("id"), "name": p.get("name"),
             "tagline": p.get("tagline"), "avatar": p.get("avatar")}
            for p in self.settings.get("personas") or []
        ]}

    def now_playing(self):
        persona = None
        active = None
        shows = self.schedule.get("shows") or []
        if shows:
            active = {"name": shows[0].get("name"), "persona": {"name": None}}
        return {
            "nowPlaying": {"title": "Mock Track", "artist": "Mock Artist"},
            "activeShow": active,
            "dj": {"station": self.settings.get("station")},
            "listeners": {"current": 0, "peak": 1},
            "streamOnline": True,
            "timezone": self.settings.get("timezone"),
            "locale": self.settings.get("locale"),
        }

    def settings_summary(self):
        """The UI summary. Deliberately NOT settings.json."""
        summary = {}
        for key in SETTINGS_SUMMARY_KEYS:
            if key == "serverTimezone":
                summary[key] = self.settings.get("timezone")
            elif key == "skills":
                summary[key] = list((self.settings.get("skills") or {}).get("enabled") or {})
            elif key == "onAir":
                summary[key] = True
            elif key == "pickerBusy":
                summary[key] = False
            else:
                summary[key] = {}
        return summary

    def backup_zip(self):
        """A zip that omits schedule.json, like the real export."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("settings.json",
                             json.dumps(self.settings, indent=2))
            archive.writestr("library.db", b"not a real library")
            archive.writestr("skills/demo-skill/SKILL.md", "# demo skill\n")
            archive.writestr("themes/classic-dark.json", "{}")
            archive.writestr("persona-avatars/README.txt", "avatars\n")
            # schedule.json is intentionally absent: that is the bug.
        return buffer.getvalue()

    # -- writes ------------------------------------------------------------
    def merge_settings(self, body):
        """Shallow merge: each top-level key replaces the existing value."""
        for key, value in body.items():
            self.settings[key] = copy.deepcopy(value)
        return 200, {"ok": True, "keys": sorted(body)}

    def upsert_show(self, show):
        if not isinstance(show, dict):
            return 400, {"error": "show must be an object"}
        shows = self.schedule.setdefault("shows", [])
        show = copy.deepcopy(show)
        show_id = show.get("id")
        if show_id:
            for index, existing in enumerate(shows):
                if existing.get("id") == show_id:
                    shows[index] = show
                    return 200, {"shows": shows, "show": show, "created": False}
            shows.append(show)
            return 200, {"shows": shows, "show": show, "created": True}
        if len(shows) >= self.max_shows:
            return 409, {"error": "show limit reached", "limit": self.max_shows}
        show["id"] = self.new_show_id()
        shows.append(show)
        return 200, {"shows": shows, "show": show, "created": True}

    def replace_schedule(self, body):
        grid = body.get("schedule") if isinstance(body, dict) and "schedule" in body else body
        if not isinstance(grid, dict):
            return 400, {"error": "schedule must be an object"}
        if sorted(grid) != [str(day) for day in range(7)]:
            return 400, {"error": "schedule must have days 0..6", "got": sorted(grid)}
        for day, cells in grid.items():
            if not isinstance(cells, list) or len(cells) != 24:
                return 400, {"error": "day %s must have 24 cells" % day}
        self.schedule["schedule"] = copy.deepcopy(grid)
        if isinstance(body, dict) and "override" in body:
            self.schedule["override"] = copy.deepcopy(body["override"])
        return 200, {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockSubwave/1.0"
    protocol_version = "HTTP/1.1"

    # keep test output readable
    def log_message(self, *args):
        pass

    @property
    def station(self):
        return self.server.station

    # -- helpers -----------------------------------------------------------
    def _authorised(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[len("Basic "):]).decode("utf-8")
        except Exception:
            return False
        expected = "%s:%s" % (self.station.admin_user, self.station.admin_pass)
        return raw == expected

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _send(self, code, payload=None, raw=None, content_type="application/json"):
        if raw is None:
            raw = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _admin(self, method, path, body):
        """Record and enforce auth. Returns True when the request may proceed."""
        if self._authorised():
            return True
        self.station.record(method, path, body, False)
        self._send(401, {"error": "unauthorized"})
        return False

    # -- verbs -------------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.station.record("GET", path, None, True)
            return self._send(200, {"status": "on-air"})
        if path == "/api/now-playing":
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.now_playing())
        if path == "/api/personas":
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_personas())
        if path == "/api/schedule":
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_schedule())
        if path == "/api/settings":
            if not self._admin("GET", path, None):
                return
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.settings_summary())
        if path == "/api/backup/export":
            if not self._admin("GET", path, None):
                return
            self.station.record("GET", path, None, True)
            return self._send(200, raw=self.station.backup_zip(),
                              content_type="application/zip")
        return self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self._body()
        if path == "/api/settings":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.merge_settings(body or {})
            return self._send(code, payload)
        if path == "/api/shows":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.upsert_show((body or {}).get("show"))
            return self._send(code, payload)
        return self._send(404, {"error": "not found", "path": path})

    def do_PUT(self):
        path = urlsplit(self.path).path
        body = self._body()
        if path == "/api/personas":
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_personas())
        if path == "/api/schedule":
            if not self._admin("PUT", path, body):
                return
            self.station.record("PUT", path, body, True)
            code, payload = self.station.replace_schedule(body or {})
            return self._send(code, payload)
        return self._send(404, {"error": "not found", "path": path})


class MockServer(ThreadingHTTPServer):
    def __init__(self, address, station):
        super().__init__(address, Handler)
        self.station = station


def start_mock(seed=None, admin=("mock-admin", "mock-pass"), port=0, max_shows=12):
    """Start the mock in a background thread. Returns the server."""
    if seed is None:
        seed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document = copy.deepcopy(seed)
    station = Station(document["settings"], document["schedule"],
                      admin[0], admin[1], max_shows=max_shows)
    server = MockServer(("127.0.0.1", port), station)
    # short poll interval: shutdown() waits for the loop, and tests start
    # and stop many servers
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    server.thread = thread
    server.url = "http://127.0.0.1:%d" % server.server_address[1]
    server.admin = admin

    def stop():
        server.shutdown()
        server.server_close()

    server.stop = stop
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the mock SUB/WAVE station.")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--seed", default=str(FIXTURE))
    parser.add_argument("--admin-user", default="mock-admin")
    parser.add_argument("--admin-pass", default="mock-pass")
    parser.add_argument("--max-shows", type=int, default=12)
    args = parser.parse_args(argv)

    seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
    server = start_mock(seed, (args.admin_user, args.admin_pass),
                        port=args.port, max_shows=args.max_shows)
    print("mock station: %s" % server.url, flush=True)
    print("admin auth:   %s / %s" % (args.admin_user, args.admin_pass))
    print("seed:         %s" % args.seed)
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
