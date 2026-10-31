#!/usr/bin/env python3
"""Read-only operations against a SUB/WAVE station.

Tiers:
  tier 1  HTTP only. Portable. Reads are trimmed by the server.
  tier 2  HTTP plus host access (ssh + docker exec). Full fidelity.

Every command prints the tier it is running at. Nothing here writes to a station.

Configuration comes from the environment, with a repo-root .env as a fallback:
  SUBWAVE_BASE_URL, SUBWAVE_ADMIN_USER, SUBWAVE_ADMIN_PASS,
  SUBWAVE_STATION_PASSWORD, SUBWAVE_HOST, SUBWAVE_STATE_DIR, SUBWAVE_CONTAINER
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = REPO_ROOT / "instance"
DEFAULT_CONTAINER = "sub-wave-controller"
DEFAULT_TIMEOUT = 20
SENTINEL = "__REDACTED__"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 1
EXIT_CONFIG = 2

# Each entry is a path into settings.json. A trailing "*" redacts every child.
REDACT_PATHS = (
    ("privacy", "password"),
    ("llm", "apiKey"),
    ("llm", "keys", "*"),
    ("llm", "fallback", "apiKey"),
    ("tts", "cloud", "apiKey"),
    ("tts", "cloud", "compatApiKey"),
    ("search", "apiKey"),
    ("embedding", "apiKey"),
    ("scrobble", "lastfm", "apiKey"),
    ("scrobble", "lastfm", "apiSecret"),
    ("scrobble", "lastfm", "sessionKey"),
    ("scrobble", "listenbrainz", "userToken"),
)

CHARACTER_FIELDS = (
    ("station", "station"),
    ("description", "stationDescription"),
    ("locale", "locale"),
    ("timezone", "timezone"),
    ("theme", "theme"),
    ("houseRules", "djHouseRules"),
)


class ConfigError(Exception):
    """Missing or unusable configuration."""


class ToolError(Exception):
    """An operation failed (network, ssh, bad response)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_dotenv(path):
    """Parse KEY=VALUE lines. Comments and blanks ignored; no dependency."""
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class Config:
    """Environment plus .env fallback. Real environment variables always win."""

    KEYS = (
        "SUBWAVE_BASE_URL",
        "SUBWAVE_ADMIN_USER",
        "SUBWAVE_ADMIN_PASS",
        "SUBWAVE_STATION_PASSWORD",
        "SUBWAVE_HOST",
        "SUBWAVE_STATE_DIR",
        "SUBWAVE_CONTAINER",
    )

    def __init__(self, dotenv=None, environ=None):
        dotenv = dotenv if dotenv is not None else load_dotenv(REPO_ROOT / ".env")
        environ = environ if environ is not None else os.environ
        self.values = {}
        for key in self.KEYS:
            value = environ.get(key)
            if value is None or value == "":
                value = dotenv.get(key, "")
            self.values[key] = (value or "").strip()
        self.container = self.values["SUBWAVE_CONTAINER"] or DEFAULT_CONTAINER
        self.base_url = self.values["SUBWAVE_BASE_URL"].rstrip("/")
        self.host = self.values["SUBWAVE_HOST"]
        self.state_dir = self.values["SUBWAVE_STATE_DIR"].rstrip("/")
        self.admin_user = self.values["SUBWAVE_ADMIN_USER"]
        self.admin_pass = self.values["SUBWAVE_ADMIN_PASS"]
        self.station_password = self.values["SUBWAVE_STATION_PASSWORD"]

    def require_http(self):
        if not self.base_url:
            raise ConfigError(
                "SUBWAVE_BASE_URL is not set. Copy .env.example to .env and fill it in, "
                "or export it in the environment."
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(
                "SUBWAVE_BASE_URL must start with http:// or https:// (got %r)" % self.base_url
            )

    def has_tier2_settings(self):
        return bool(self.host and self.state_dir)

    def missing_tier2(self):
        missing = []
        if not self.host:
            missing.append("SUBWAVE_HOST")
        if not self.state_dir:
            missing.append("SUBWAVE_STATE_DIR")
        return missing


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def http_json(config, path, admin=False, station=False, method="GET", body=None,
              timeout=DEFAULT_TIMEOUT):
    """Call a JSON endpoint. Returns (status_code, payload)."""
    config.require_http()
    url = config.base_url + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if admin:
        if not (config.admin_user and config.admin_pass):
            raise ConfigError("this endpoint needs SUBWAVE_ADMIN_USER and SUBWAVE_ADMIN_PASS")
        token = base64.b64encode(
            ("%s:%s" % (config.admin_user, config.admin_pass)).encode()
        ).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    if station:
        if not config.station_password:
            raise ConfigError("this endpoint needs SUBWAVE_STATION_PASSWORD")
        request.add_header("x-station-auth", config.station_password)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, {"error": detail or exc.reason}
    except urllib.error.URLError as exc:
        raise ToolError("cannot reach %s (%s)" % (url, exc.reason))
    except json.JSONDecodeError as exc:
        raise ToolError("%s did not return JSON (%s)" % (url, exc))


def ssh_run(config, remote_command, timeout=DEFAULT_TIMEOUT):
    """Run a command on the host. Returns CompletedProcess."""
    if not config.host:
        raise ConfigError("SUBWAVE_HOST is not set")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", config.host, remote_command]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ToolError("ssh is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ToolError("ssh to %s timed out after %ss" % (config.host, timeout))


def read_state_file(config, filename):
    """Read a file from the controller container's state dir (tier 2)."""
    path = "%s/%s" % (config.state_dir, filename)
    # Single-quoted so the REMOTE shell expands nothing; the local shell never
    # sees it because the whole string is one argv element.
    result = ssh_run(config, "docker exec %s cat '%s'" % (config.container, path))
    if result.returncode != 0:
        raise ToolError(
            "cannot read %s via docker exec %s: %s"
            % (path, config.container, (result.stderr or "").strip() or "unknown error")
        )
    return result.stdout


# --------------------------------------------------------------------------
# tier detection
# --------------------------------------------------------------------------

def ssh_probe(config):
    """(ok, detail) for tier 2. Probes; never assumes."""
    path = "%s/settings.json" % config.state_dir
    result = ssh_run(config, "docker exec %s test -r '%s'" % (config.container, path))
    if result.returncode == 0:
        return True, "ssh %s, docker exec %s" % (config.host, config.container)
    detail = (result.stderr or "").strip().splitlines()
    return False, "host probe failed: " + (detail[-1] if detail else "exit %d" % result.returncode)


def detect_tier(config, probe=None):
    """Return (tier, detail). `probe` is injectable so tests need no ssh."""
    if not config.has_tier2_settings():
        return 1, "missing " + ", ".join(config.missing_tier2())
    probe = probe or (lambda: ssh_probe(config))
    ok, detail = probe()
    return (2 if ok else 1), detail


TIER_LABEL = {
    1: "1 - HTTP only (no host access)",
    2: "2 - host access (ssh + docker exec)",
}


# --------------------------------------------------------------------------
# slugs and the lockfile
# --------------------------------------------------------------------------

def slugify(name):
    """kebab-case: lowercase, non-alphanumerics collapsed to single hyphens."""
    text = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return text or "unnamed"


def assign_slugs(items, previous):
    """Map slug -> id for items, stable against `previous`.

    A slug already in the lockfile never changes, even if the name did: the id
    is the identity. New items get a kebab slug, collisions take -2, -3, ...
    """
    by_id = {id_: slug for slug, id_ in previous.items()}
    used = dict(previous)
    result = {}
    for item in items:
        id_ = item.get("id")
        if not id_:
            continue
        if id_ in by_id:
            result[by_id[id_]] = id_
            continue
        base = slugify(item.get("name") or id_)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = "%s-%d" % (base, suffix)
            suffix += 1
        used[candidate] = id_
        result[candidate] = id_
    return result


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

def _redact_path(doc, path):
    node = doc
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return 0
        node = node[key]
    last = path[-1]
    if not isinstance(node, dict):
        return 0
    if last == "*":
        count = 0
        for key, value in list(node.items()):
            if isinstance(value, str) and value:
                node[key] = SENTINEL
                count += 1
        return count
    if last not in node:
        return 0
    value = node[last]
    if isinstance(value, str) and value:
        node[last] = SENTINEL
        return 1
    return 0


def redact(settings):
    """Return (redacted copy, count of values replaced)."""
    doc = copy.deepcopy(settings)
    count = 0
    for path in REDACT_PATHS:
        count += _redact_path(doc, path)
    return doc, count


# --------------------------------------------------------------------------
# the model: live documents <-> the instance/ character layer
# --------------------------------------------------------------------------

def strip_settings(settings):
    """What belongs in instance/settings.json: no personas, no shows."""
    return {k: v for k, v in settings.items()
            if k not in ("personas", "activePersonaId", "shows")}


def character_from(settings):
    return {name: copy.deepcopy(settings.get(source)) for name, source in CHARACTER_FIELDS}


def build_live_model(settings, schedule):
    """Canonical model from the two live documents, secrets redacted."""
    redacted, count = redact(settings)
    return {
        "settings": strip_settings(redacted),
        "character": character_from(redacted),
        "personas": {p["id"]: p for p in (redacted.get("personas") or []) if p.get("id")},
        "shows": {s["id"]: s for s in (schedule.get("shows") or []) if s.get("id")},
        "grid": schedule.get("schedule") or {},
        "override": schedule.get("override"),
        "activePersonaId": redacted.get("activePersonaId"),
        "redacted": count,
    }


def load_instance_model(root=None):
    """Read instance/ back into the canonical model."""
    root = Path(root or INSTANCE_DIR)
    lock = json.loads((root / "ids.lock.json").read_text(encoding="utf-8"))
    personas = {}
    for path in sorted((root / "character" / "personas").glob("*.json")):
        persona = json.loads(path.read_text(encoding="utf-8"))
        personas[persona["id"]] = persona
    shows = {}
    show_files = {}
    for path in sorted((root / "character" / "shows").glob("*.json")):
        show = json.loads(path.read_text(encoding="utf-8"))
        show_files[path.stem] = show
        # a show authored in the repo has no server id yet
        shows[show.get("id") or ("new:%s" % path.stem)] = show
    schedule_doc = json.loads((root / "character" / "schedule.json").read_text(encoding="utf-8"))
    show_slugs = lock.get("shows") or {}
    grid = {
        day: [show_slugs.get(cell, cell) for cell in cells]
        for day, cells in (schedule_doc.get("schedule") or {}).items()
    }
    override = schedule_doc.get("override")
    if override:
        override = show_slugs.get(override, override)
    settings_path = root / "settings.json"
    return {
        "settings": json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.is_file() else None,
        "character": json.loads(
            (root / "character" / "character.json").read_text(encoding="utf-8")),
        "personas": personas,
        "shows": shows,
        "show_files": show_files,
        "grid": grid,
        "override": override,
        "activePersonaId": lock.get("activePersonaId"),
        "redacted": 0,
    }


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_instance(model, previous_lock=None, capture=None):
    """Write the character layer. Returns a summary dict."""
    previous_lock = previous_lock or {}
    persona_slugs = assign_slugs(list(model["personas"].values()),
                                 previous_lock.get("personas") or {})
    show_slugs = assign_slugs(list(model["shows"].values()),
                              previous_lock.get("shows") or {})

    write_json(INSTANCE_DIR / "settings.json", model["settings"])
    write_json(INSTANCE_DIR / "character" / "character.json", model["character"])

    for slug, id_ in persona_slugs.items():
        write_json(INSTANCE_DIR / "character" / "personas" / ("%s.json" % slug),
                   model["personas"][id_])
    for slug, id_ in show_slugs.items():
        write_json(INSTANCE_DIR / "character" / "shows" / ("%s.json" % slug),
                   model["shows"][id_])

    id_to_slug = {id_: slug for slug, id_ in show_slugs.items()}
    grid = {
        day: [id_to_slug.get(cell, cell) for cell in cells]
        for day, cells in model["grid"].items()
    }
    override = model["override"]
    write_json(INSTANCE_DIR / "character" / "schedule.json",
               {"schedule": grid, "override": id_to_slug.get(override, override) if override else None})

    lock = {
        "version": 1,
        "personas": persona_slugs,
        "shows": show_slugs,
        "ids": {id_: slug for slug, id_ in list(persona_slugs.items()) + list(show_slugs.items())},
        "activePersonaId": model.get("activePersonaId"),
    }
    if capture:
        lock["capture"] = capture
    write_json(INSTANCE_DIR / "ids.lock.json", lock)

    cells = [cell for day in model["grid"].values() for cell in day]
    return {
        "personas": len(persona_slugs),
        "shows": len(show_slugs),
        "cells": len(cells),
        "filled": sum(1 for cell in cells if cell),
        "redacted": model.get("redacted", 0),
        "files": 2 + len(persona_slugs) + len(show_slugs) + 1,
    }


def describe_instance(root=None):
    """Human description of an existing instance/, for the refusal message."""
    root = Path(root or INSTANCE_DIR)
    persona_dir = root / "character" / "personas"
    show_dir = root / "character" / "shows"
    if persona_dir.is_dir() or show_dir.is_dir():
        return "%d personas, %d shows" % (
            len(list(persona_dir.glob("*.json"))), len(list(show_dir.glob("*.json"))))
    flat = sorted(path.name for path in root.glob("*.json"))
    return "flat capture (%s)" % ", ".join(flat) if flat else "empty directory"


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _entity_diff(repo, live):
    repo_ids, live_ids = set(repo), set(live)
    changed = sorted(i for i in repo_ids & live_ids if repo[i] != live[i])
    return {
        "added": sorted(repo_ids - live_ids),
        "removed": sorted(live_ids - repo_ids),
        "changed": changed,
        "unchanged": sorted((repo_ids & live_ids) - set(changed)),
    }


def compare_models(repo, live):
    """Drift of instance/ relative to live: added means 'in repo, not live'."""
    report = {kind: _entity_diff(repo[kind], live[kind]) for kind in ("personas", "shows")}

    grid_changed = []
    days = set(repo["grid"]) | set(live["grid"])
    for day in sorted(days):
        if (repo["grid"].get(day) or []) != (live["grid"].get(day) or []):
            grid_changed.append(day)
    report["grid"] = {"changed_days": grid_changed, "unchanged": not grid_changed,
                      "cells": sum(len(v) for v in live["grid"].values())}

    repo_settings = repo.get("settings") or {}
    changed_keys = sorted(k for k in set(repo_settings) | set(live["settings"])
                          if repo_settings.get(k) != live["settings"].get(k))
    report["settings"] = {"changed": changed_keys, "keys": len(live["settings"]),
                          "unchanged": not changed_keys}

    character_changed = [k for k in set(repo["character"]) | set(live["character"])
                         if repo["character"].get(k) != live["character"].get(k)]
    report["character"] = {"changed": sorted(character_changed),
                           "unchanged": not character_changed}
    report["override"] = {"unchanged": repo.get("override") == live.get("override"),
                          "repo": repo.get("override"), "live": live.get("override")}
    return report


def drift_count(report):
    total = 0
    for kind in ("personas", "shows"):
        total += len(report[kind]["added"]) + len(report[kind]["removed"]) + len(report[kind]["changed"])
    total += len(report["grid"]["changed_days"])
    total += len(report["settings"]["changed"])
    total += len(report["character"]["changed"])
    if not report["override"]["unchanged"]:
        total += 1
    return total


def print_diff(report):
    for kind in ("personas", "shows"):
        part = report[kind]
        line = "%d unchanged, %d changed, %d added, %d removed" % (
            len(part["unchanged"]), len(part["changed"]), len(part["added"]), len(part["removed"]))
        detail = []
        if part["changed"]:
            detail.append("changed: " + ", ".join(part["changed"]))
        if part["added"]:
            detail.append("added: " + ", ".join(part["added"]))
        if part["removed"]:
            detail.append("removed: " + ", ".join(part["removed"]))
        print("%-10s%s" % (kind, line + ("  (" + "; ".join(detail) + ")" if detail else "")))

    grid = report["grid"]
    print("%-10s%s" % ("grid", "unchanged (%d cells)" % grid["cells"] if grid["unchanged"]
                       else "changed days: " + ", ".join(grid["changed_days"])))
    settings = report["settings"]
    print("%-10s%s" % ("settings", "unchanged (%d keys)" % settings["keys"]
                       if settings["unchanged"]
                       else "%d of %d keys changed: %s" % (
                           len(settings["changed"]), settings["keys"],
                           ", ".join(settings["changed"]))))
    character = report["character"]
    print("%-10s%s" % ("character", "unchanged" if character["unchanged"]
                       else "changed: " + ", ".join(character["changed"])))
    if not report["override"]["unchanged"]:
        print("%-10s%s" % ("override", "repo=%s live=%s" % (
            report["override"]["repo"], report["override"]["live"])))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_status(config, args):
    tier, detail = detect_tier(config)
    if tier == 1 and not config.base_url:
        raise ConfigError(
            "tier 1 needs SUBWAVE_BASE_URL. Copy .env.example to .env and fill it in, "
            "or export it in the environment."
        )

    settings = schedule = None
    if tier == 2:
        settings = json.loads(read_state_file(config, "settings.json"))
        schedule = json.loads(read_state_file(config, "schedule.json"))

    now = None
    api_failed = None
    if config.base_url:
        try:
            status, now = http_json(config, "/api/now-playing")
            if status != 200:
                api_failed = "/api/now-playing returned HTTP %s" % status
                now = None
        except (ToolError, ConfigError) as exc:
            api_failed = str(exc)
            now = None
    else:
        api_failed = "SUBWAVE_BASE_URL not set"

    print("tier      %s" % TIER_LABEL[tier], flush=True)
    print("why       %s" % detail)
    print("endpoint  %s" % (config.base_url or "(not set)"))

    active = (now or {}).get("activeShow") or {}
    track = (now or {}).get("nowPlaying") or {}
    station = (settings or {}).get("station") or ((now or {}).get("dj") or {}).get("station")

    if station:
        extra = ""
        if settings:
            extra = "  [%s, %s]" % (settings.get("timezone", "?"), settings.get("locale", "?"))
        print("station   %s%s" % (station, extra))

    if now:
        persona = (active.get("persona") or {}).get("name")
        if active.get("name"):
            print("on air    %s%s" % (active["name"], " - %s" % persona if persona else ""))
        if track.get("title"):
            print("playing   %s - %s" % (track.get("title"), track.get("artist", "?")))
        listeners = now.get("listeners") or {}
        print("listeners %s now, peak %s" % (
            listeners.get("current", "?"), listeners.get("peak", "?")))
        print("stream    %s" % ("online" if now.get("streamOnline") else "offline"))
    elif api_failed:
        print("api       %s" % api_failed)

    if settings is not None:
        shows = schedule.get("shows") or []
        cells = [cell for day in (schedule.get("schedule") or {}).values() for cell in day]
        print("config    %d personas, %d shows, %d/%d grid cells filled" % (
            len(settings.get("personas") or []), len(shows),
            sum(1 for cell in cells if cell), len(cells)))
        print("reads     full fidelity (state files)")
        print("backup    complete: zip + schedule.json")
    else:
        print("reads     trimmed: 4 fields per persona, 7 of 26 per show, no schedule.json")
        print("backup    zip only - DOES NOT include shows or the grid")

    print("sync      %s" % _sync_line(config, tier, settings, schedule))
    print("writes    none: this build is read-only")
    return EXIT_ERROR if api_failed else EXIT_OK


def _sync_line(config, tier, settings, schedule):
    if not (INSTANCE_DIR / "ids.lock.json").is_file():
        return "no instance/ captured yet (run: subwave.py pull)"
    if tier != 2 or settings is None:
        return "cannot compare at tier 1 (trimmed reads)"
    try:
        repo = load_instance_model()
        live = build_live_model(settings, schedule)
        count = drift_count(compare_models(repo, live))
    except (ToolError, ValueError, KeyError) as exc:
        return "cannot compare instance/: %s" % exc
    if count == 0:
        return "in sync with instance/"
    return "%d change(s) vs instance/ (run: subwave.py diff)" % count


def cmd_pull(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)

    previous_lock = {}
    if INSTANCE_DIR.exists():
        if not args.force:
            raise ToolError(
                "instance/ already exists (%s). Refusing to overwrite: re-run with --force to "
                "replace it, which would lose that capture." % describe_instance())
        lock_path = INSTANCE_DIR / "ids.lock.json"
        if lock_path.is_file():
            previous_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        # Replace, do not merge: a stale file from an older layout would
        # otherwise sit next to the new character layer and confuse the next reader.
        shutil.rmtree(INSTANCE_DIR)

    losses = []
    if tier == 2:
        settings = json.loads(read_state_file(config, "settings.json"))
        schedule = json.loads(read_state_file(config, "schedule.json"))
    else:
        if not args.allow_lossy:
            raise ToolError(
                "tier 1 cannot capture a station completely: there is no schedule in the API "
                "backup, /api/schedule trims shows to 7 of 26 fields, and admin /api/settings is "
                "a UI summary rather than settings.json. Use host access "
                "(SUBWAVE_HOST + SUBWAVE_STATE_DIR), or re-run with --allow-lossy to accept a "
                "partial capture.")
        settings, schedule, losses = pull_lossy(config)

    model = build_live_model(settings, schedule)
    if model["settings"] is None:
        model["settings"] = {}
    capture = {
        "tier": tier,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lossy": bool(losses),
        "lost": list(losses),
    }
    summary = write_instance(model, previous_lock, capture)

    print("wrote     instance/ (%d files + ids.lock.json)" % summary["files"])
    print("redacted  %d of %d configured paths held a value -> %s"
          % (summary["redacted"], len(REDACT_PATHS), SENTINEL))
    print("personas  %d" % summary["personas"])
    print("shows     %d" % summary["shows"])
    print("grid      %d/%d cells filled" % (summary["filled"], summary["cells"]))
    if losses:
        print("lost      " + "; ".join(losses))
    return EXIT_OK


def pull_lossy(config):
    """Tier-1 capture: what the public/station API can give, and what it cannot."""
    losses = [
        "show definitions: 19 of 26 fields per show (the grid itself does survive via /api/schedule)",
        "persona definitions: 14 of 18 fields (soul, tts, skills, voiceStyle, ...)",
        "station settings: admin /api/settings is a UI summary, not settings.json, so "
        "instance/settings.json is a stub",
    ]
    status, state = http_json(config, "/api/state")
    status_personas, personas = http_json(config, "/api/personas")
    status_schedule, schedule_doc = http_json(config, "/api/schedule")

    if status != 200 or status_personas != 200 or status_schedule != 200:
        raise ToolError("the API did not return the endpoints a lossy pull needs")

    settings = {
        "station": (state.get("station") or {}).get("name"),
        "timezone": state.get("timezone"),
        "locale": state.get("locale"),
        "theme": state.get("theme"),
        "personas": personas.get("personas") or [],
    }
    schedule = {"shows": schedule_doc.get("shows") or [],
                "schedule": schedule_doc.get("schedule") or {},
                "override": schedule_doc.get("override")}
    return settings, schedule, losses


def cmd_diff(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)

    if not (INSTANCE_DIR / "ids.lock.json").is_file():
        raise ToolError("no instance/ to compare (run: subwave.py pull)")

    repo = load_instance_model()
    if tier != 2:
        return diff_trimmed(config, repo)
    settings = json.loads(read_state_file(config, "settings.json"))
    schedule = json.loads(read_state_file(config, "schedule.json"))
    live = build_live_model(settings, schedule)

    report = compare_models(repo, live)
    print("direction instance/ -> live")
    print_diff(report)
    count = drift_count(report)
    print("result    %s" % ("in sync" if count == 0 else "%d change(s)" % count))
    return EXIT_OK if count == 0 else EXIT_DRIFT


def take_backup(config, base_dir=None):
    """Copy settings.json + schedule.json outside the repo. Returns (path, settings, schedule)."""
    settings_text = read_state_file(config, "settings.json")
    schedule_text = read_state_file(config, "schedule.json")
    settings = json.loads(settings_text)
    schedule = json.loads(schedule_text)

    station = settings.get("station") or "station"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(base_dir).expanduser() if base_dir else Path.home() / ".subwave-backups"
    destination = base / slugify(station) / stamp

    resolved = destination.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ToolError(
            "refusing to write a backup inside the repo (%s): it would contain live secrets. "
            "Choose a directory outside %s." % (resolved, REPO_ROOT))

    destination.mkdir(parents=True, exist_ok=False)
    (destination / "settings.json").write_text(settings_text, encoding="utf-8")
    (destination / "schedule.json").write_text(schedule_text, encoding="utf-8")
    return destination, settings, schedule


def diff_trimmed(config, repo):
    """Tier-1 comparison over the fields the API actually exposes."""
    status, personas = http_json(config, "/api/personas")
    status_schedule, schedule = http_json(config, "/api/schedule")
    if status != 200 or status_schedule != 200:
        raise ToolError("the API did not return the endpoints a trimmed diff needs")

    live_personas = {p.get("id"): p for p in (personas.get("personas") or []) if p.get("id")}
    live_shows = {s.get("id"): s for s in (schedule.get("shows") or []) if s.get("id")}

    def visible_diff(repo_items, live_items):
        added = sorted(set(repo_items) - set(live_items))
        removed = sorted(set(live_items) - set(repo_items))
        changed = []
        for id_ in set(repo_items) & set(live_items):
            live = live_items[id_]
            repo_item = repo_items[id_]
            # Only fields the repo object actually carries: a trimmed read can
            # include derived keys (a single "mood") that no show document holds.
            if any(repo_item[key] != value for key, value in live.items() if key in repo_item):
                changed.append(id_)
        return {"added": added, "removed": removed, "changed": sorted(changed),
                "unchanged": sorted((set(repo_items) & set(live_items)) - set(changed))}

    report = {"personas": visible_diff(repo["personas"], live_personas),
              "shows": visible_diff(repo["shows"], live_shows)}

    grid_changed = [day for day in sorted(set(repo["grid"]) | set(schedule.get("schedule") or {}))
                    if (repo["grid"].get(day) or []) != ((schedule.get("schedule") or {}).get(day) or [])]
    report["grid"] = {"changed_days": grid_changed, "unchanged": not grid_changed,
                      "cells": sum(len(day) for day in (schedule.get("schedule") or {}).values())}
    report["settings"] = {"changed": [], "keys": 0, "unchanged": True}
    report["character"] = {"changed": [], "unchanged": True}
    report["override"] = {"unchanged": repo.get("override") == schedule.get("override"),
                          "repo": repo.get("override"), "live": schedule.get("override")}

    print("note      trimmed comparison: 14 persona fields and 19 show fields are not "
          "readable at tier 1, so they cannot drift here")
    print_diff(report)
    count = (len(report["personas"]["added"]) + len(report["personas"]["removed"])
             + len(report["personas"]["changed"]) + len(report["shows"]["added"])
             + len(report["shows"]["removed"]) + len(report["shows"]["changed"])
             + len(report["grid"]["changed_days"]))
    if not report["override"]["unchanged"]:
        count += 1
    print("result    %s" % ("in sync (readable fields)" if count == 0
                            else "%d change(s) in the readable fields" % count))
    return EXIT_OK if count == 0 else EXIT_DRIFT


def cmd_backup(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)
    if tier != 2:
        raise ToolError(
            "refusing to back up at tier 1: the API backup omits schedule.json entirely, and "
            "/api/schedule returns shows trimmed to 7 of 26 fields, so a tier-1 backup could not "
            "restore the programming. Add SUBWAVE_HOST and SUBWAVE_STATE_DIR for host access.")

    destination, settings, schedule = take_backup(config, args.dir)

    shows = schedule.get("shows") or []
    cells = [cell for day in (schedule.get("schedule") or {}).values() for cell in day]
    print("path      %s" % destination)
    print("shows     %d" % len(shows))
    print("grid      %d/%d cells" % (sum(1 for cell in cells if cell), len(cells)))
    print("files     settings.json, schedule.json (unredacted, outside the repo)")
    return EXIT_OK


# --------------------------------------------------------------------------
# apply: instance/ -> station
# --------------------------------------------------------------------------

def find_sentinels(node, path=""):
    """Every path still holding the redaction sentinel."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = "%s.%s" % (path, key) if path else key
            found += find_sentinels(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += find_sentinels(value, "%s[%d]" % (path, index))
    elif node == SENTINEL:
        found.append(path)
    return found


def rehydrate(body, live_settings, config):
    """Replace sentinels with real values in place. Returns paths we could not fill."""
    remaining = []

    def live_value(dotted):
        node = live_settings
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node if isinstance(node, str) and node else None

    def walk(node, path=""):
        if isinstance(node, dict):
            for key in list(node):
                value = node[key]
                here = "%s.%s" % (path, key) if path else key
                if isinstance(value, str) and value == SENTINEL:
                    replacement = None
                    if here == "privacy.password" and config.station_password:
                        replacement = config.station_password
                    elif here == "llm.apiKey" and os.environ.get("ANTHROPIC_API_KEY"):
                        replacement = os.environ["ANTHROPIC_API_KEY"]
                    elif live_settings is not None:
                        replacement = live_value(here)
                    if replacement:
                        node[key] = replacement
                    else:
                        remaining.append(here)
                elif isinstance(value, (dict, list)):
                    walk(value, here)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, "%s[%d]" % (path, index))

    walk(body)
    return remaining


def build_steps(model, lock):
    """Whole objects from the repo. Personas and settings first, shows second."""
    steps = []
    personas = list((model.get("personas") or {}).values())
    if personas:
        body = {"personas": personas}
        if model.get("activePersonaId"):
            body["activePersonaId"] = model["activePersonaId"]
        steps.append({"method": "POST", "path": "/api/settings", "entity": "settings",
                      "label": "personas + activePersonaId", "body": body,
                      "note": "%d personas" % len(personas)})
    settings = model.get("settings") or {}
    if settings:
        steps.append({"method": "POST", "path": "/api/settings", "entity": "settings",
                      "label": "settings sections", "body": copy.deepcopy(settings),
                      "note": "%d top-level keys" % len(settings)})
    for slug, show in sorted((model.get("show_files") or {}).items()):
        steps.append({"method": "POST", "path": "/api/shows", "entity": "show",
                      "slug": slug, "label": "show %s" % slug,
                      "body": {"show": copy.deepcopy(show)},
                      "note": "%d fields" % len(show)})
    return steps


def build_grid(model, lock):
    """The grid by server id, plus any cell we cannot resolve yet."""
    known = set((lock.get("shows") or {}).values())
    grid = {}
    unresolved = []
    for day, cells in (model.get("grid") or {}).items():
        resolved = []
        for cell in cells:
            if not cell:
                resolved.append(None)
            elif cell in known:
                resolved.append(cell)
            else:
                unresolved.append(cell)
                resolved.append(cell)
        grid[day] = resolved
    override = model.get("override")
    if override:
        override = override if override in known else None
    return {"schedule": grid, "override": override}, sorted(set(unresolved))


def payload_hash(steps, grid_body):
    blob = json.dumps(
        {"steps": [{"method": step["method"], "path": step["path"], "body": step["body"]}
                   for step in steps],
         "grid": grid_body},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def send_step(config, step):
    status, payload = http_json(config, step["path"], admin=True,
                                method=step["method"], body=step["body"])
    if status == 401:
        raise ToolError(
            "authentication failed (HTTP 401) on %s %s: check SUBWAVE_ADMIN_USER and "
            "SUBWAVE_ADMIN_PASS" % (step["method"], step["path"]))
    if status >= 400:
        detail = ""
        if isinstance(payload, dict):
            detail = payload.get("error") or json.dumps(payload)[:200]
        raise ToolError("%s %s failed: HTTP %s %s"
                        % (step["method"], step["path"], status, detail))
    return status, payload


def persist_new_id(step, payload, lock):
    """Write a server-assigned id back into the show file and the lockfile."""
    show = (payload or {}).get("show") or {}
    new_id = show.get("id")
    sent = (step.get("body") or {}).get("show") or {}
    if not new_id or sent.get("id") == new_id:
        return None
    slug = step.get("slug")
    path = INSTANCE_DIR / "character" / "shows" / ("%s.json" % slug)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["id"] = new_id
    write_json(path, document)
    lock.setdefault("shows", {})[slug] = new_id
    lock.setdefault("ids", {})[new_id] = slug
    write_json(INSTANCE_DIR / "ids.lock.json", lock)
    return slug, new_id


def cmd_apply(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)

    if args.prune:
        raise ToolError("--prune is designed but not implemented: apply never deletes.")

    lock_path = INSTANCE_DIR / "ids.lock.json"
    if not lock_path.is_file():
        raise ToolError("no instance/ to apply (run: subwave.py pull)")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    capture = lock.get("capture") or {}
    if capture.get("lossy"):
        lost = "; ".join(capture.get("lost") or []) or "unknown"
        raise ToolError(
            "refusing to apply a lossy capture (tier %s): %s. A show trimmed to 7 fields pushed "
            "back would strip 19 fields per show on the live station. Re-pull with host access."
            % (capture.get("tier"), lost))

    model = load_instance_model()
    live_settings = None
    if tier == 2:
        live_settings = json.loads(read_state_file(config, "settings.json"))

    steps = build_steps(model, lock)
    remaining = []
    for step in steps:
        remaining += rehydrate(step["body"], live_settings, config)
    if remaining:
        raise ToolError(
            "refusing to apply: %d secret path(s) still hold %s (%s). Writing that would "
            "overwrite a real credential with a placeholder. At tier 1 only "
            "SUBWAVE_STATION_PASSWORD and ANTHROPIC_API_KEY can be rehydrated; use host access "
            "for the rest." % (len(set(remaining)), SENTINEL, ", ".join(sorted(set(remaining)))))

    grid_body, unresolved = build_grid(model, lock)

    if not args.yes:
        print("mode      dry run - nothing is sent")
        for index, step in enumerate(steps, start=1):
            print("step %-4d %s %-14s %-28s (%s)"
                  % (index, step["method"], step["path"], step["label"], step["note"]))
        cells = sum(len(day) for day in grid_body["schedule"].values())
        print("step %-4d %s %-14s %-28s (%d cells%s)"
              % (len(steps) + 1, "PUT", "/api/schedule", "grid", cells,
                 "" if not unresolved else ", %d unresolved" % len(unresolved)))
        if unresolved:
            print("unresolved  %s - new shows get ids on the write, then the grid resolves"
                  % ", ".join(unresolved))
        print("guards    lossy capture: no; sentinel: no; mid-show check and backup happen on --yes")
        print("result    %d step(s) prepared, 0 requests sent" % (len(steps) + 1))
        return EXIT_OK

    if not args.force_now:
        status, now = http_json(config, "/api/now-playing")
        active = (now or {}).get("activeShow") or {}
        if status == 200 and active.get("name"):
            raise ToolError(
                "the station is mid-show (%s). Writing into a live programme is not the "
                "default: pass --force-now if that is intended." % active.get("name"))

    if args.no_backup:
        print("backup    skipped (--no-backup)")
    elif tier == 2:
        destination, _, _ = take_backup(config, args.backup_dir)
        print("backup    %s" % destination)
    else:
        raise ToolError(
            "refusing to write at tier 1 without a backup: a tier-1 backup cannot include the "
            "schedule or the full show definitions, so this write would be unprotected. Pass "
            "--no-backup to accept that explicitly.")

    digest = payload_hash(steps, grid_body)
    applied = lock.get("applied") or {}
    if applied.get("hash") == digest:
        print("result    no changes since the last apply (%s)" % applied.get("at"))
        return EXIT_OK

    for step in steps:
        status, payload = send_step(config, step)
        print("wrote     %s %-14s %-28s -> HTTP %s"
              % (step["method"], step["path"], step["label"], status))
        if step["entity"] == "show":
            assigned = persist_new_id(step, payload, lock)
            if assigned:
                print("id        %s -> %s (assigned by the station)" % assigned)

    # The grid can only be written once every show it references has an id.
    model = load_instance_model()
    grid_body, unresolved = build_grid(model, lock)
    if unresolved:
        raise ToolError("cannot write the grid: %d cell(s) reference unknown shows (%s)"
                        % (len(unresolved), ", ".join(unresolved)))
    status, _ = send_step(config, {"method": "PUT", "path": "/api/schedule",
                                   "body": grid_body, "label": "grid"})
    print("wrote     PUT %-14s %-28s -> HTTP %s" % ("/api/schedule", "grid", status))

    lock["applied"] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hash": payload_hash(build_steps(model, lock), grid_body),
        "tier": tier,
    }
    write_json(lock_path, lock)
    print("result    %d step(s) written; ids.lock.json updated" % (len(steps) + 1))
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="subwave.py",
        description="Read-only operations against a SUB/WAVE station.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="tier, station, on-air state, repo sync")
    status.set_defaults(func=cmd_status)

    pull = sub.add_parser("pull", help="capture live state into instance/")
    pull.add_argument("--force", action="store_true", help="replace an existing instance/")
    pull.add_argument("--allow-lossy", action="store_true",
                      help="tier 1: accept a partial capture and say what is lost")
    pull.set_defaults(func=cmd_pull)

    diff = sub.add_parser("diff", help="read-only drift report")
    diff.set_defaults(func=cmd_diff)

    apply_cmd = sub.add_parser("apply", help="push instance/ to the station (dry run by default)")
    apply_cmd.add_argument("--yes", action="store_true", help="perform the writes")
    apply_cmd.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    apply_cmd.add_argument("--force-now", action="store_true", help="write even while on air")
    apply_cmd.add_argument("--no-backup", action="store_true", help="write without a backup")
    apply_cmd.add_argument("--backup-dir", help="where to put the pre-apply backup")
    apply_cmd.add_argument("--prune", action="store_true", help="reserved; not implemented")
    apply_cmd.set_defaults(func=cmd_apply)

    backup = sub.add_parser("backup", help="copy settings.json + schedule.json outside the repo")
    backup.add_argument("--dir", help="base directory (default ~/.subwave-backups)")
    backup.set_defaults(func=cmd_backup)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config()
    try:
        return args.func(config, args)
    except ConfigError as exc:
        print("subwave: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG
    except ToolError as exc:
        print("subwave: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
