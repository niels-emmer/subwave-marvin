#!/usr/bin/env python3
"""A local stand-in for a SUB/WAVE station's admin API. Standard library only.

It reproduces the semantics that make a write path dangerous, so `apply` can be
proven without touching a live station:

  GET  /api/health          public
  GET  /api/now-playing     public
  GET  /api/state           public; station identity and current state
  GET  /api/personas        public; TRIMMED to 4 fields per persona
  GET  /api/schedule        public; shows TRIMMED to 7 of 26 fields
  POST /api/mcp               JSON-RPC: initialize, tools/list, tools/call
  GET  /api/dj/skills         admin; the skill catalog, with `seeded` for built-ins
  GET  /api/dj/skills/:slug/file   admin; a skill's fields and brief
  POST /api/dj/skills         admin; create a custom skill
  PUT  /api/dj/skills/:slug/file   admin; update a custom skill
  GET  /api/library/blocklist  admin; the third config document, returned whole
  POST /api/library/blocklist  admin; block an entry; 409 when already blocked
  POST /api/library/blocklist/rules      admin; mint a rule uuid
  PUT  /api/library/blocklist/rules/:id  admin; update a rule by uuid
  DELETE /api/shows/:id, /api/personas/:id, /api/library/blocklist/rules/:id  admin
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
import re
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "station.json"

# Mirrors settings.ID_RE in the station (and the persona-avatar route): no
# dashes, 3-32 chars. The mock enforces it so a bad id fails in tests instead
# of on a live station.
ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")

# What /api/schedule exposes per show. The other 19 fields are not readable
# over HTTP, which is exactly why read-modify-write would destroy them.
PUBLIC_SHOW_FIELDS = ("id", "name", "topic", "personaId", "guestPersonaIds", "moods", "mood")

# What admin /api/settings exposes: a UI summary, not the config document.
SETTINGS_SUMMARY_KEYS = (
    "autoPick", "budget", "defaults", "embedding", "env", "jingles", "libraryStats",
    "llm", "navidrome", "ollama", "onAir", "pickerBusy", "search", "serverTimezone",
    "skills", "streamOnAir", "tagger", "tts", "values",
)


MCP_TOOLS = [
    ("subwave_health", "station health", {}, False),
    ("subwave_now_playing", "what is on air", {}, False),
    ("subwave_station_state", "queue and history", {}, False),
    ("subwave_schedule", "the grid", {}, False),
    ("subwave_session", "current session", {}, False),
    ("subwave_search_library", "search the library", {"q": "string"}, False),
    ("subwave_similar_tracks", "similar tracks", {"id": "string"}, False),
    ("subwave_request_status", "request receipt", {"requestId": "string"}, False),
    ("subwave_list_skills", "list skills", {}, False),
    ("subwave_list_sfx", "list sfx", {}, False),
    ("subwave_list_jingles", "list jingles", {}, False),
    ("subwave_dj_announce", "have the DJ say something", {"message": "string"}, True),
    ("subwave_dj_segment", "run a DJ segment", {"type": "string"}, True),
    ("subwave_skip_track", "skip the current track", {"expectId": "string"}, True),
    ("subwave_request_song", "request a song", {"request": "string"}, True),
    ("subwave_queue_track", "queue a track", {"id": "string", "title": "string"}, True),
    ("subwave_queue_block", "queue a block", {"kind": "string"}, True),
    ("subwave_run_skill", "run a skill", {"name": "string"}, True),
    ("subwave_play_sfx", "play a sfx", {"name": "string"}, True),
    ("subwave_play_jingle", "play a jingle", {"filename": "string"}, True),
    ("subwave_refresh_playlist", "refresh a playlist", {}, True),
]
MCP_CONTROL = {name for name, _, _, control in MCP_TOOLS if control}


DEFAULT_SKILLS = {
    "curiosity": {"seeded": True, "label": "Curiosity", "cooldown": "90m",
                  "brief": "Wonder aloud about something small."},
    "demo-skill": {"seeded": False, "label": "Demo skill", "cooldown": "4h",
                   "brief": "A fictional segment used by the demo station."},
}


DEFAULT_BLOCKLIST = {
    "entries": [
        {"type": "track", "id": "subsonic-1", "name": "Mock Banned Track",
         "artist": "Mock Artist", "album": "Mock Album",
         "addedAt": "2026-10-01T00:00:00.000Z"},
    ],
    "rules": [
        {"id": "rule-0001", "label": "No christmas music", "field": "genre",
         "values": ["Christmas"], "season": None, "showIds": [],
         "addedAt": "2026-10-01T00:00:00.000Z"},
    ],
}


class Station:
    """In-memory station state plus the semantics of each route."""

    def __init__(self, settings, schedule, admin_user, admin_pass, max_shows=12,
                 blocklist=None):
        self.settings = copy.deepcopy(settings)
        self.schedule = copy.deepcopy(schedule)
        self.blocklist = copy.deepcopy(blocklist if blocklist is not None else DEFAULT_BLOCKLIST)
        self.skills = copy.deepcopy(DEFAULT_SKILLS)
        self.mcp_calls = []
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.max_shows = max_shows
        self.requests = []
        self._lock = threading.Lock()
        self._next_show = 1
        self._next_rule = 2

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

    def add_blocklist_entry(self, body):
        entry = body if isinstance(body, dict) else {}
        if not entry.get("type") or not entry.get("id"):
            return 400, {"error": "type and id are required"}
        for existing in self.blocklist.get("entries") or []:
            if existing.get("type") == entry["type"] and existing.get("id") == entry["id"]:
                return 409, {"error": "already blocked"}
        stored = dict(entry, addedAt="2026-10-31T00:00:00.000Z")
        self.blocklist.setdefault("entries", []).append(stored)
        return 200, stored

    def add_blocklist_rule(self, body):
        rule = dict(body if isinstance(body, dict) else {})
        with self._lock:
            value = "rule-%04d" % self._next_rule
            self._next_rule += 1
        rule["id"] = value
        rule["addedAt"] = "2026-10-31T00:00:00.000Z"
        self.blocklist.setdefault("rules", []).append(rule)
        return 200, rule

    def update_blocklist_rule(self, rule_id, body):
        for index, existing in enumerate(self.blocklist.get("rules") or []):
            if existing.get("id") == rule_id:
                updated = dict(existing)
                updated.update(body if isinstance(body, dict) else {})
                updated["id"] = rule_id
                self.blocklist["rules"][index] = updated
                return 200, updated
        return 404, {"error": "no such rule"}

    def delete_show(self, show_id):
        before = len(self.schedule.get("shows") or [])
        self.schedule["shows"] = [show for show in (self.schedule.get("shows") or [])
                                  if show.get("id") != show_id]
        if len(self.schedule["shows"]) == before:
            return 404, {"error": "no such show"}
        return 200, {"ok": True}

    def delete_persona(self, persona_id):
        before = len(self.settings.get("personas") or [])
        self.settings["personas"] = [persona for persona in (self.settings.get("personas") or [])
                                     if persona.get("id") != persona_id]
        if len(self.settings["personas"]) == before:
            return 404, {"error": "no such persona"}
        return 200, {"ok": True}

    def delete_rule(self, rule_id):
        before = len(self.blocklist.get("rules") or [])
        self.blocklist["rules"] = [rule for rule in (self.blocklist.get("rules") or [])
                                   if rule.get("id") != rule_id]
        if len(self.blocklist["rules"]) == before:
            return 404, {"error": "no such rule"}
        return 200, {"ok": True}

    def mcp_tools(self):
        return [{"name": name, "description": text,
                 "inputSchema": {"type": "object", "properties": {
                     key: {"type": "string"} for key in props},
                     "required": [key for key in props]}}
                for name, text, props, _ in MCP_TOOLS]

    def mcp_result(self, name, arguments):
        if name == "subwave_now_playing":
            return {"nowPlaying": {"title": "Mock Track", "artist": "Mock Artist"},
                    "activeShow": {"name": "Mock Show"}}
        if name == "subwave_station_state":
            return {"queue": [], "history": []}
        if name == "subwave_search_library":
            return {"tracks": [{"id": "tr-1", "title": "First Result", "artist": "A"},
                               {"id": "tr-2", "title": "Second Result", "artist": "B"}]}
        if name == "subwave_request_song":
            return {"requestId": "req-1", "status": "pending"}
        if name == "subwave_request_status":
            return {"requestId": arguments.get("requestId"), "status": "queued"}
        return {"ok": True, "tool": name}

    def public_skills(self):
        return {"skills": [{"kind": slug, "seeded": bool(skill.get("seeded")),
                            "label": skill.get("label") or slug}
                           for slug, skill in sorted(self.skills.items())]}

    def skill_file(self, slug):
        skill = self.skills.get(slug)
        if skill is None:
            return 404, {"error": "no such skill"}
        return 200, {"kind": slug, "custom": not skill.get("seeded"),
                     "exists": True, "label": skill.get("label"),
                     "cooldown": skill.get("cooldown"),
                     "brief": skill.get("brief") or ""}

    def create_skill(self, body):
        slug = (body or {}).get("name")
        if not slug:
            return 400, {"error": "name is required"}
        if slug in self.skills:
            return 409, {"error": "a skill named %r already exists" % slug}
        self.skills[slug] = {"seeded": False, "label": body.get("label") or slug,
                             "cooldown": body.get("cooldown") or "60m",
                             "brief": body.get("brief") or ""}
        return 200, {"skills": self.public_skills()["skills"]}

    def update_skill(self, slug, body):
        skill = self.skills.get(slug)
        if skill is None:
            return 404, {"error": "no such custom skill"}
        if skill.get("seeded"):
            return 400, {"error": "built-in"}
        for key in ("label", "cooldown", "brief"):
            if key in (body or {}):
                skill[key] = body[key]
        return 200, {"skills": self.public_skills()["skills"]}

    def public_blocklist(self):
        """The stored document plus the live fields the API adds."""
        return {
            "entries": [dict(entry, active=True) for entry in self.blocklist.get("entries") or []],
            "rules": [dict(rule, active=True, matchCount=0)
                      for rule in self.blocklist.get("rules") or []],
        }

    def public_state(self):
        """Station identity as /api/state exposes it."""
        return {
            "station": {"id": None, "name": self.settings.get("station"),
                        "multiStation": False},
            "timezone": self.settings.get("timezone"),
            "locale": self.settings.get("locale"),
            "theme": self.settings.get("theme"),
            "privacy": self.settings.get("privacy") or {},
            "current": None,
            "upcoming": [],
            "streamIdle": False,
            "pickerBusy": False,
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
        personas = body.get("personas")
        if isinstance(personas, list):
            for persona in personas:
                id_ = persona.get("id") if isinstance(persona, dict) else None
                if id_ is not None and not ID_RE.match(str(id_)):
                    return 400, {"error": "invalid persona id", "id": id_}
        for key, value in body.items():
            self.settings[key] = copy.deepcopy(value)
        return 200, {"ok": True, "keys": sorted(body)}

    def upsert_show(self, show):
        if not isinstance(show, dict):
            return 400, {"error": "show must be an object"}
        shows = self.schedule.setdefault("shows", [])
        show = copy.deepcopy(show)
        show_id = show.get("id")
        if show_id and not ID_RE.match(str(show_id)):
            return 400, {"error": "invalid show id", "id": show_id}
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

    def _mcp(self, body):
        payload = body if isinstance(body, dict) else {}
        method = payload.get("method")
        if method == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"protocolVersion": "2025-06-18",
                                               "capabilities": {"tools": {}},
                                               "serverInfo": {"name": "mock-subwave"}}})
        if method == "tools/list":
            return self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"tools": self.station.mcp_tools()}})
        if method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            authorised = self._authorised()
            if name in MCP_CONTROL and not authorised:
                self.station.mcp_calls.append({"tool": name, "arguments": arguments,
                                               "authorised": False})
                return self._send(401, {"error": "unauthorized"})
            self.station.mcp_calls.append({"tool": name, "arguments": arguments,
                                           "authorised": authorised})
            result = self.station.mcp_result(name, arguments)
            return self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"),
                                    "result": {"content": [
                                        {"type": "text", "text": json.dumps(result)}]}})
        return self._send(200, {"jsonrpc": "2.0", "id": payload.get("id"),
                                "error": {"code": -32601, "message": "unknown method %r"
                                          % method}})

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
        if path == "/api/mcp":
            return self._mcp(body)
        if path == "/api/dj/skills":
            if not self._admin("GET", path, None):
                return
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_skills())
        if path.startswith("/api/dj/skills/") and path.endswith("/file"):
            if not self._admin("GET", path, None):
                return
            self.station.record("GET", path, None, True)
            code, payload = self.station.skill_file(path.split("/")[4])
            return self._send(code, payload)
        if path == "/api/library/blocklist":
            if not self._admin("GET", path, None):
                return
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_blocklist())
        if path.startswith("/api/library/blocklist/rules/") and self.command == "PUT":
            pass  # handled in do_PUT
        if path == "/api/state":
            self.station.record("GET", path, None, True)
            return self._send(200, self.station.public_state())
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
        if path == "/api/mcp":
            return self._mcp(body)
        if path == "/api/dj/skills":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.create_skill(body)
            return self._send(code, payload)
        if path == "/api/library/blocklist":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.add_blocklist_entry(body)
            return self._send(code, payload)
        if path == "/api/library/blocklist/rules":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.add_blocklist_rule(body)
            return self._send(code, payload)
        if path == "/api/shows":
            if not self._admin("POST", path, body):
                return
            self.station.record("POST", path, body, True)
            code, payload = self.station.upsert_show((body or {}).get("show"))
            return self._send(code, payload)
        return self._send(404, {"error": "not found", "path": path})

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/library/blocklist/rules/"):
            if not self._admin("DELETE", path, None):
                return
            self.station.record("DELETE", path, None, True)
            code, payload = self.station.delete_rule(path.rsplit("/", 1)[-1])
            return self._send(code, payload)
        if path.startswith("/api/shows/"):
            if not self._admin("DELETE", path, None):
                return
            self.station.record("DELETE", path, None, True)
            code, payload = self.station.delete_show(path.rsplit("/", 1)[-1])
            return self._send(code, payload)
        if path.startswith("/api/personas/"):
            if not self._admin("DELETE", path, None):
                return
            self.station.record("DELETE", path, None, True)
            code, payload = self.station.delete_persona(path.rsplit("/", 1)[-1])
            return self._send(code, payload)
        return self._send(404, {"error": "not found", "path": path})

    def do_PUT(self):
        path = urlsplit(self.path).path
        body = self._body()
        if path.startswith("/api/dj/skills/") and path.endswith("/file"):
            if not self._admin("PUT", path, body):
                return
            self.station.record("PUT", path, body, True)
            code, payload = self.station.update_skill(path.split("/")[4], body)
            return self._send(code, payload)
        if path.startswith("/api/library/blocklist/rules/"):
            if not self._admin("PUT", path, body):
                return
            self.station.record("PUT", path, body, True)
            code, payload = self.station.update_blocklist_rule(
                path.rsplit("/", 1)[-1], body)
            return self._send(code, payload)
        if path == "/api/schedule":
            if not self._admin("PUT", path, body):
                return
            self.station.record("PUT", path, body, True)
            code, payload = self.station.replace_schedule(body or {})
            return self._send(code, payload)
        return self._send(404, {"error": "not found", "path": path})

def build_state_dir(root, settings, schedule, blocklist=None):
    """A state dir with the shape the real one has: config, assets, and the
    expensive library.db that a backup should skip by default."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(settings, indent=2))
    (root / "schedule.json").write_text(json.dumps(schedule, indent=2))
    (root / "blocklist.json").write_text(
        json.dumps(blocklist if blocklist is not None else DEFAULT_BLOCKLIST, indent=2))

    for slug, entry in DEFAULT_SKILLS.items():
        directory = root / "skills" / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            "---\nname: %s\nlabel: %s\ncooldown: %s\n---\n\n%s\n"
            % (slug, entry["label"], entry["cooldown"], entry["brief"]))
    (root / "skills" / "demo-skill" / "tool.mjs").write_text("export default {}\n")

    for name in ("sfx", "jingles", "beds"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("%s-one.mp3" % name)).write_bytes(b"\x00" * 1024)
        (root / ("%s.json" % name)).write_text(json.dumps({"items": ["%s-one.mp3" % name]}))

    (root / "themes").mkdir(parents=True, exist_ok=True)
    (root / "themes" / "classic-dark.json").write_text("{}")

    (root / "persona-avatars").mkdir(parents=True, exist_ok=True)
    (root / "persona-avatars" / "p_demo_nell.png").write_bytes(b"\x89PNG" + b"\x00" * 64)

    (root / "library.db").write_bytes(b"\x00" * 4096)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "app.log").write_text("a log line\n")
    (root / "secrets.env").write_text("NOT_A_REAL_SECRET=1\n")
    (root / "archive").mkdir(parents=True, exist_ok=True)
    (root / "archive" / "old.mp3").write_bytes(b"\x00" * 512)
    return root


class MockServer(ThreadingHTTPServer):
    def __init__(self, address, station):
        super().__init__(address, Handler)
        self.station = station


def start_mock(seed=None, admin=("mock-admin", "mock-pass"), port=0, max_shows=12,
               with_state_dir=True):
    """Start the mock in a background thread. Returns the server."""
    if seed is None:
        seed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document = copy.deepcopy(seed)
    station = Station(document["settings"], document["schedule"],
                      admin[0], admin[1], max_shows=max_shows,
                      blocklist=document.get("blocklist"))
    server = MockServer(("127.0.0.1", port), station)
    # short poll interval: shutdown() waits for the loop, and tests start
    # and stop many servers
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    server.thread = thread
    server.url = "http://127.0.0.1:%d" % server.server_address[1]

    server.state_dir = None
    if with_state_dir:
        server.state_dir = build_state_dir(
            Path(tempfile.mkdtemp(prefix="mock-state-")), station.settings,
            station.schedule, station.blocklist)
    server.admin = admin

    def stop():
        server.shutdown()
        server.server_close()
        if server.state_dir is not None:
            shutil.rmtree(server.state_dir, ignore_errors=True)

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
