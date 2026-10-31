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

# The station's own id contract (controller/src/routes/public.ts mirrors
# settings.ID_RE): lowercase alphanumerics and underscores, 3-32 chars. No
# dashes. A scaffolded id must satisfy this or the station rejects or normalises it.
ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")

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
    ("name", "station"),
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


def ssh_run(config, remote_command, timeout=DEFAULT_TIMEOUT, binary=False):
    """Run a command on the host. Returns CompletedProcess."""
    if not config.host:
        raise ConfigError("SUBWAVE_HOST is not set")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", config.host, remote_command]
    try:
        return subprocess.run(argv, capture_output=True, text=not binary, timeout=timeout)
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
    """Identity fields. houseRules is normalised to a list: the station stores
    djHouseRules as a string (often empty), while the character layer wants one
    rule per list item."""
    character = {name: copy.deepcopy(settings.get(source)) for name, source in CHARACTER_FIELDS}
    rules = character.get("houseRules")
    if isinstance(rules, str):
        character["houseRules"] = [rules.strip()] if rules.strip() else []
    elif not isinstance(rules, list):
        character["houseRules"] = []
    return character


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


# What a restore needs beyond the two config documents. The API zip carries
# these but omits schedule.json; ours carries schedule.json, so it must carry
# these too, or neither backup is complete.
ASSET_DIRS = ("skills", "sfx", "jingles", "beds", "themes", "persona-avatars")
ASSET_MANIFESTS = ("sfx.json", "jingles.json", "beds.json")

# Never copied, whatever the flags say.
NEVER_COPY = {
    "secrets.env": "credentials, never backed up",
    "icecast-secrets.env": "credentials, never backed up",
    "icecast_listener_auth.txt": "derived listener-auth config",
}
LIBRARY_FILES = ("library.db", "library.db-wal", "library.db-shm")


class LocalStateSource:
    """A state dir on this machine. Used by tests; the shape is the real one."""

    def __init__(self, root):
        self.root = Path(root)

    def list_files(self):
        return sorted(path.relative_to(self.root).as_posix()
                      for path in self.root.rglob("*") if path.is_file())

    def read_bytes(self, relative):
        return (self.root / relative).read_bytes()


class SshStateSource:
    """The real thing: a root-owned state dir inside the controller container."""

    def __init__(self, config):
        self.config = config

    def list_files(self):
        command = ("docker exec %s sh -c 'cd %s && find . -type f'"
                   % (self.config.container, self.config.state_dir))
        result = ssh_run(self.config, command)
        if result.returncode != 0:
            raise ToolError("cannot list %s in %s: %s"
                            % (self.config.state_dir, self.config.container,
                               (result.stderr or "").strip() or "unknown error"))
        return sorted(line[2:] for line in result.stdout.splitlines() if line.startswith("./"))

    def read_bytes(self, relative):
        command = "docker exec %s cat '%s/%s'" % (
            self.config.container, self.config.state_dir, relative)
        result = ssh_run(self.config, command, binary=True)
        if result.returncode != 0:
            raise ToolError("cannot read %s: %s" % (
                relative, (result.stderr or b"").decode("utf-8", "replace").strip()
                or "unknown error"))
        return result.stdout


def select_backup_files(files, with_library=False):
    """Split a state listing into what we copy and what we skip, with reasons."""
    wanted, skipped = [], []
    for relative in files:
        name = relative.split("/")[0]
        if relative in ("settings.json", "schedule.json"):
            continue  # written at the top level of the backup
        if relative in NEVER_COPY:
            skipped.append((relative, NEVER_COPY[relative]))
        elif relative in LIBRARY_FILES:
            if with_library:
                wanted.append(relative)
            else:
                skipped.append((relative, "audio analysis, expensive to rebuild and large "
                                          "(use --with-library to include it)"))
        elif name in ASSET_DIRS or relative in ASSET_MANIFESTS:
            wanted.append(relative)
        else:
            skipped.append((relative, "runtime data, not part of a restore"))
    return wanted, skipped


def _restore_note(settings, state_dir, copied_dirs, skipped, with_library):
    lines = [
        "# Restore",
        "",
        "Copy these back with the controller stopped. Order matters: config first, then assets.",
        "",
        "## 1. State files",
        "",
        "    settings.json  -> %s/settings.json" % state_dir,
        "    schedule.json  -> %s/schedule.json" % state_dir,
        "",
        "## 2. Assets",
        "",
    ]
    for directory in copied_dirs:
        lines.append("    state/%s/  -> %s/%s/" % (directory, state_dir, directory))
    lines += [
        "",
        "## 3. Restart",
        "",
        "Restart the mixer if `privacy.listenerAuth`, `stream.maxListeners`, `opus` or `aac`",
        "changed: those only take effect when the controller regenerates the Icecast config.",
        "",
        "## Notes",
        "",
    ]
    if with_library:
        lines.append("- `library.db` was included.")
    else:
        lines.append("- `library.db`: audio analysis, expensive to rebuild but large. "
                     "Re-run the analyzer after a restore.")
    for relative, reason in skipped:
        if relative in LIBRARY_FILES:
            continue
        lines.append("- `%s`: %s" % (relative, reason))
    lines.append("")
    return "\n".join(lines)


def print_manifest(destination, manifest):
    groups, loose = {}, []
    for relative in manifest["copied"]:
        if relative in ("settings.json", "schedule.json"):
            continue
        if "/" in relative:
            head = relative.split("/")[0]
            groups[head] = groups.get(head, 0) + 1
        else:
            loose.append(relative)
    print("path      %s" % destination)
    print("files     %d (%d bytes)" % (manifest["files"], manifest["bytes"]))
    print("copied    settings.json, schedule.json%s"
          % (", " + ", ".join(sorted(loose)) if loose else ""))
    for head in sorted(groups):
        print("          %s/ (%d file%s)" % (head, groups[head], "" if groups[head] == 1 else "s"))
    print("skipped   %d path(s)" % len(manifest["skipped"]))
    seen = set()
    for entry in manifest["skipped"]:
        head = entry["path"].split("/")[0]
        if head in seen:
            continue
        seen.add(head)
        print("          %-24s %s" % (head, entry["reason"]))


def take_backup(config, base_dir=None, state=None, with_library=False):
    """Copy everything a restore needs, outside the repo. Returns (path, manifest)."""
    state = state or SshStateSource(config)
    settings_text = state.read_bytes("settings.json").decode("utf-8")
    schedule_text = state.read_bytes("schedule.json").decode("utf-8")
    settings = json.loads(settings_text)
    json.loads(schedule_text)

    wanted, skipped = select_backup_files(state.list_files(), with_library)

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

    total = len(settings_text) + len(schedule_text)
    for relative in wanted:
        payload = state.read_bytes(relative)
        target = destination / "state" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        total += len(payload)

    copied_dirs = sorted({relative.split("/")[0] for relative in wanted
                          if "/" in relative and relative.split("/")[0] in ASSET_DIRS})
    (destination / "RESTORE.md").write_text(
        _restore_note(settings, config.state_dir or "/var/sub-wave", copied_dirs,
                      skipped, with_library), encoding="utf-8")

    manifest = {
        "station": station,
        "at": stamp,
        "files": 2 + len(wanted),
        "bytes": total,
        "copied": ["settings.json", "schedule.json"] + wanted,
        "skipped": [{"path": path, "reason": reason} for path, reason in skipped],
        "with_library": with_library,
    }
    write_json(destination / "manifest.json", manifest)
    return destination, manifest


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
    print("result    %s" % ("in sync on the readable fields only (not a full match)"
                            if count == 0
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

    destination, manifest = take_backup(config, args.dir, with_library=args.with_library)
    print_manifest(destination, manifest)
    print("restore   %s" % (destination / "RESTORE.md"))
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
        # Name them: after an edit, the dry run must say which entity changed.
        names = ", ".join(str(p.get("name") or p.get("id") or "?") for p in personas)
        steps.append({"method": "POST", "path": "/api/settings", "entity": "settings",
                      "label": "personas + activePersonaId", "body": body,
                      "note": "%d personas: %s" % (len(personas), names)})
    settings = model.get("settings") or {}
    if settings:
        steps.append({"method": "POST", "path": "/api/settings", "entity": "settings",
                      "label": "settings sections", "body": copy.deepcopy(settings),
                      "note": "%d top-level keys" % len(settings)})
    for slug, show in sorted((model.get("show_files") or {}).items()):
        payload = copy.deepcopy(show)
        if is_placeholder(payload.get("id"), "show"):
            # a scaffolded show has only a local placeholder: omit it so the
            # station assigns a real id, which persist_new_id writes back
            payload.pop("id", None)
        steps.append({"method": "POST", "path": "/api/shows", "entity": "show",
                      "slug": slug, "label": "show %s" % slug,
                      "body": {"show": payload},
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

    # validate first: a typo'd field would be silently dropped by a shallow merge
    report = validate_instance()
    if report["unreadable"]:
        raise ToolError("cannot apply: instance/ is unreadable (%s). Run: subwave.py validate"
                        % "; ".join(report["unreadable"]))
    if report["errors"]:
        raise ToolError(
            "refusing to apply: validate found %d error(s), first: %s. Run: subwave.py validate"
            % (len(report["errors"]), report["errors"][0]))

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


# --------------------------------------------------------------------------
# validate: instance/ before anything is applied
# --------------------------------------------------------------------------

PERSONA_FIELDS = frozenset((
    "avatar", "djMode", "frequency", "humour", "id", "language", "linkStyle",
    "localColour", "musicLean", "name", "scriptLength", "skills", "soul",
    "tagline", "tags", "tts", "voiceStyle", "warmth",
))

SHOW_FIELDS = frozenset((
    "banter", "energies", "eras", "excludedPlaylistIds", "fadeAtShowEnd", "filtersStrict",
    "genres", "guestPersonaIds", "id", "maxTrackSeconds", "minTrackLengthSeconds", "moods",
    "name", "pauseTalk", "personaId", "playlistExhaust", "playlistIds", "playlistInOrder",
    "playlistStrict", "preparationSkill", "programme", "segmentSkill", "tags", "themeId",
    "topic", "vocals",
))

# Edit-time value contracts. `validate` stays permissive about values (a newer
# server may know more of a vocabulary, and an unknown field is only a warning);
# `character edit-*` is strict, because a string where an integer belongs, or an
# enum outside the set, is dropped or ignored by the station's shallow merge.
#
# SOURCE OF TRUTH: upstream SUB/WAVE, `controller/src/schemas/persona.ts`
# (PERSONA_FREQUENCIES, PERSONA_SCRIPT_LENGTHS, PERSONA_LINK_STYLES), re-exported
# by `controller/src/settings/vocab.ts` as FREQUENCIES / SCRIPT_LENGTHS /
# LINK_STYLES. Re-check these against that file before changing them; do not
# widen a list by guessing. Order is the product's ladder, not alphabetical:
# `controller/src/settings/persona.ts` bumps a persona "one rung up" with
# `FREQUENCIES.indexOf(...)`, so silent -> aggressive is meaningful.
PERSONA_ENUMS = {
    "frequency": ("silent", "quiet", "moderate", "chatty", "aggressive"),
    "scriptLength": ("one-liner", "concise", "extended", "storyteller"),
    "linkStyle": ("natural", "announce"),
}

# Show side, from upstream `controller/src/schemas/show.ts`:
# - SHOW_ENERGY = low, medium, high (an ordered ladder)
# - SHOW_VOCALS = instrumental, vocal; the field also accepts '' ("any")
# - moods is deliberately NOT an enum: `showStringList({ values: ctx.moodNames })`
#   validates against the station's own installed mood names, which this toolkit
#   cannot read, so moods stays a free-form list here.
SHOW_ENUMS = {
    "energies": ("low", "medium", "high"),
    "vocals": ("", "instrumental", "vocal"),
}

# kind -> field -> how one `--set field=value` becomes a JSON value. `id` is
# deliberately absent: it is immutable and handled before this lookup.
PERSONA_TYPES = {
    "avatar": "string", "djMode": "boolean", "frequency": "enum", "humour": "integer",
    "language": "string", "linkStyle": "enum", "localColour": "integer",
    "musicLean": "list", "name": "string", "scriptLength": "enum", "skills": "list",
    "soul": "string", "tagline": "string", "tags": "list", "tts": "json",
    "voiceStyle": "string", "warmth": "integer",
}

SHOW_TYPES = {
    "banter": "boolean", "energies": "enum-list", "eras": "json",
    "excludedPlaylistIds": "list", "fadeAtShowEnd": "boolean", "filtersStrict": "boolean",
    "genres": "list", "guestPersonaIds": "list", "maxTrackSeconds": "integer",
    "minTrackLengthSeconds": "integer", "moods": "list", "name": "string",
    "pauseTalk": "boolean", "personaId": "string", "playlistExhaust": "boolean",
    "playlistIds": "list", "playlistInOrder": "boolean", "playlistStrict": "boolean",
    "preparationSkill": "string", "programme": "boolean", "segmentSkill": "string",
    "tags": "list", "themeId": "string", "topic": "string", "vocals": "enum",
}

CHARACTER_REQUIRED = ("name", "description", "locale", "timezone", "theme", "houseRules")

# One contract for every id, taken from the station: no dashes, 3-32 chars.
ID_PATTERNS = {"persona": ID_RE, "show": ID_RE}


def _load_json(path, unreadable):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        unreadable.append("%s: missing" % path)
    except Exception as exc:
        unreadable.append("%s: not valid JSON (%s)" % (path, exc))
    return None


def _check_fields(name, document, fields, errors, warnings, what):
    """Required fields are errors; unknown fields are warnings.

    `id` is never required: the station assigns ids, so a newly authored
    entity legitimately has none yet.
    """
    if not isinstance(document, dict):
        errors.append("%s: a %s must be a JSON object" % (name, what))
        return
    for field in sorted(fields - set(document) - {"id"}):
        errors.append("%s: missing field %r" % (name, field))
    for field in sorted(set(document) - fields):
        warnings.append("%s: unknown field %r (kept as-is; a newer server may know it)"
                        % (name, field))


def _duplicate_ids(documents, errors, what):
    seen = {}
    for slug, document in sorted(documents.items()):
        id_ = document.get("id") if isinstance(document, dict) else None
        if not id_:
            continue
        if id_ in seen:
            errors.append("%s %r and %r share the id %r" % (what, seen[id_], slug, id_))
        else:
            seen[id_] = slug


def validate_instance(root=None):
    """Check instance/ against the shape the server returns.

    Returns {"errors", "warnings", "unreadable", "counts"}. Warnings never
    change the exit code; unreadable input is not the same as invalid input.
    """
    root = Path(root or INSTANCE_DIR)
    errors, warnings, unreadable = [], [], []
    counts = {"personas": 0, "shows": 0, "cells": 0, "filled": 0}

    lock = _load_json(root / "ids.lock.json", unreadable)

    personas = {}
    for path in sorted((root / "character" / "personas").glob("*.json")):
        document = _load_json(path, unreadable)
        if document is None:
            continue
        counts["personas"] += 1
        personas[path.stem] = document
        _check_fields(path.name, document, PERSONA_FIELDS, errors, warnings, "persona")

    shows = {}
    for path in sorted((root / "character" / "shows").glob("*.json")):
        document = _load_json(path, unreadable)
        if document is None:
            continue
        counts["shows"] += 1
        shows[path.stem] = document
        _check_fields(path.name, document, SHOW_FIELDS, errors, warnings, "show")

    _duplicate_ids(personas, errors, "persona")
    _duplicate_ids(shows, errors, "show")

    character = _load_json(root / "character" / "character.json", unreadable)
    if character is not None:
        if not isinstance(character, dict):
            errors.append("character.json: must be a JSON object")
        else:
            for field in CHARACTER_REQUIRED:
                if field not in character:
                    errors.append("character.json: missing field %r" % field)
            for field in sorted(set(character) - set(CHARACTER_REQUIRED)):
                warnings.append("character.json: unknown field %r" % field)
            for field in ("name", "description", "locale", "timezone"):
                if field in character and not isinstance(character[field], str):
                    errors.append("character.json: %r must be a string" % field)
            if "theme" in character:
                theme = character["theme"]
                ok = isinstance(theme, str) or (
                    isinstance(theme, dict) and isinstance(theme.get("active"), str))
                if not ok:
                    errors.append("character.json: 'theme' must be a theme id string, or an "
                                  "object with an 'active' string")
            if "houseRules" in character:
                rules = character["houseRules"]
                if not (isinstance(rules, list) and all(isinstance(r, str) for r in rules)):
                    errors.append("character.json: 'houseRules' must be a list of strings")

    schedule_doc = _load_json(root / "character" / "schedule.json", unreadable)
    if schedule_doc is not None:
        grid = schedule_doc.get("schedule") if isinstance(schedule_doc, dict) else None
        if not isinstance(grid, dict):
            errors.append("schedule.json: missing a 'schedule' object")
        else:
            if sorted(grid) != [str(day) for day in range(7)]:
                errors.append("schedule.json: grid must have days 0..6, found %s" % sorted(grid))
            for day in sorted(grid):
                cells = grid[day]
                if not isinstance(cells, list) or len(cells) != 24:
                    errors.append("schedule.json: day %s must have 24 cells" % day)
                    continue
                counts["cells"] += len(cells)
                for hour, cell in enumerate(cells):
                    if cell in (None, ""):
                        errors.append("schedule.json: day %s hour %02d is empty; every cell "
                                      "must name a show" % (day, hour))
                    elif cell not in shows:
                        errors.append("schedule.json: day %s hour %02d names %r, which is not "
                                      "a show file" % (day, hour, cell))
                    else:
                        counts["filled"] += 1

    if isinstance(lock, dict):
        lock_personas = lock.get("personas") or {}
        lock_shows = lock.get("shows") or {}

        for slug in sorted(set(lock_personas) - set(personas)):
            warnings.append("ids.lock.json: persona %r has an id but no file" % slug)
        for slug in sorted(set(lock_shows) - set(shows)):
            warnings.append("ids.lock.json: show %r has an id but no file" % slug)

        seen = {}
        for kind, mapping in (("persona", lock_personas), ("show", lock_shows)):
            for slug, id_ in sorted(mapping.items()):
                if not isinstance(id_, str) or not ID_PATTERNS[kind].match(id_):
                    errors.append("%s/%s.json: id %r violates the station's ID_RE "
                                  "(^[a-z0-9_]{3,32}$, no dashes)"
                                  % (kind + "s", slug, id_))
                    continue
                if id_ in seen:
                    errors.append("ids.lock.json: id %r is used by both %r and %r"
                                  % (id_, seen[id_], slug))
                else:
                    seen[id_] = slug

        # A file with no id and no lock entry is a NEW entity, which is the
        # documented authoring flow. Every other combination is inconsistent.
        for kind, documents, mapping in (("persona", personas, lock_personas),
                                         ("show", shows, lock_shows)):
            for slug, document in sorted(documents.items()):
                file_id = document.get("id") if isinstance(document, dict) else None
                lock_id = mapping.get(slug)
                if file_id and not lock_id:
                    errors.append("ids.lock.json: %s %r has id %r in its file but no lock "
                                  "entry" % (kind, slug, file_id))
                elif lock_id and not file_id:
                    errors.append("ids.lock.json: %s %r has id %r in the lockfile but none in "
                                  "its file - applying it would create a duplicate"
                                  % (kind, slug, lock_id))
                elif file_id and lock_id and file_id != lock_id:
                    errors.append("ids.lock.json: %s %r maps to %r but its file says %r"
                                  % (kind, slug, lock_id, file_id))

        capture = lock.get("capture") or {}
        if capture.get("lossy"):
            detail = "; ".join(capture.get("lost") or []) or "no detail recorded"
            errors.append("ids.lock.json: the capture is marked lossy (%s). A trimmed capture "
                          "must not be applied" % detail)

    return {"errors": errors, "warnings": warnings, "unreadable": unreadable, "counts": counts}


def cmd_validate(config, args):
    # --dir lets CI validate examples/demo, the committed schema reference.
    # Without it this still validates instance/, exactly as before.
    root = Path(args.dir).expanduser() if getattr(args, "dir", None) else INSTANCE_DIR
    result = validate_instance(root)
    print("validate  %s" % root)
    if result["unreadable"]:
        for line in result["unreadable"]:
            print("unreadable %s" % line)
        print("result    cannot validate: input is missing or not JSON")
        return EXIT_CONFIG

    for line in result["errors"]:
        print("error     %s" % line)
    for line in result["warnings"]:
        print("warning   %s" % line)

    counts = result["counts"]
    if result["errors"]:
        print("result    %d error(s), %d warning(s) - fix the errors before applying"
              % (len(result["errors"]), len(result["warnings"])))
        return EXIT_ERROR
    print("result    ok (%d personas, %d shows, %d/%d cells filled), %d warning(s)"
          % (counts["personas"], counts["shows"], counts["filled"], counts["cells"],
             len(result["warnings"])))
    return EXIT_OK


# --------------------------------------------------------------------------
# character: author the character layer, locally, with no network
# --------------------------------------------------------------------------

DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# A scaffolded entity gets a placeholder id so the lockfile can hold it before
# the station has ever seen it. Shows have theirs dropped on the wire so the
# station assigns a real one; personas are sent inside the settings array, which
# the station stores as given, so their id is preserved.
PLACEHOLDER_PREFIX = {"persona": "p_local_", "show": "s_local_"}


def placeholder_id(kind, slug):
    """Deterministic, and valid under ID_RE: no dashes, max 32 chars."""
    body = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    return (PLACEHOLDER_PREFIX[kind] + body)[:32].rstrip("_")


def is_placeholder(id_, kind):
    return isinstance(id_, str) and id_.startswith(PLACEHOLDER_PREFIX[kind])


def title_from_slug(slug):
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)


def persona_defaults(slug, name=None):
    """All 18 fields, at values that pass validate with no warnings."""
    return {
        "id": placeholder_id("persona", slug),
        "name": name or title_from_slug(slug),
        "tagline": "",
        "frequency": "moderate",
        "scriptLength": "concise",
        "djMode": True,
        "linkStyle": "natural",
        "humour": 5,
        "localColour": 5,
        "warmth": 5,
        "soul": ("IDENTITY\nTODO: who this host is, in two or three sentences. Their job, their temperament, "
                 "what they sound like at this hour.\n\n"
                 "VOICE\nTODO: how they talk. Sentence length, what they notice, what they never do.\n\n"
                 "CATCHPHRASES\nTODO: one or two lines they could actually say."),
        "musicLean": [],
        "language": "",
        "voiceStyle": "",
        "avatar": "",
        "tts": {"engine": "kokoro", "cloudProvider": "", "voice": "", "gainDb": 0, "speed": 1},
        "skills": [],
        "tags": ["todo"],
    }


def show_defaults(slug, name=None, persona_id=""):
    """All 26 fields, at values that pass validate with no warnings."""
    return {
        "id": placeholder_id("show", slug),
        "name": name or title_from_slug(slug),
        "topic": "TODO: the brief for this show, in one or two sentences.",
        "personaId": persona_id,
        "guestPersonaIds": [],
        "banter": False,
        "pauseTalk": False,
        "programme": True,
        "segmentSkill": "",
        "preparationSkill": "",
        "moods": [],
        "themeId": "",
        "genres": [],
        "energies": [],
        "eras": [],
        "vocals": "",
        "filtersStrict": False,
        "maxTrackSeconds": 600,
        "minTrackLengthSeconds": 60,
        "fadeAtShowEnd": False,
        "playlistIds": [],
        "playlistStrict": False,
        "playlistExhaust": False,
        "playlistInOrder": False,
        "excludedPlaylistIds": [],
        "tags": ["todo"],
    }


def blank_character():
    return {
        "name": "New Station",
        "description": "TODO: what this station is for, in one sentence.",
        "locale": "en-GB",
        "timezone": "Europe/London",
        "theme": "classic-dark",
        "houseRules": ["TODO: a rule the hosts must follow."],
    }


def _read_lock(root):
    path = root / "ids.lock.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_lock(root, lock):
    write_json(root / "ids.lock.json", lock)


def _require_character(root):
    if not (root / "character").is_dir():
        raise ToolError("no character layer at %s - run: subwave.py character init" % (root / "character"))


def _slugs(root, kind):
    return sorted(p.stem for p in (root / "character" / kind).glob("*.json"))


def _split_list(raw):
    """Comma-separated values, or a JSON array when it starts with '['."""
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ToolError("a list takes comma-separated values or a JSON array (%s)" % exc)
        if not isinstance(value, list):
            raise ToolError("a list takes comma-separated values or a JSON array (got %r)" % raw)
        return value
    items = [part.strip() for part in text.split(",")]
    if any(not item for item in items):
        raise ToolError("empty item in list %r" % raw)
    return items


def parse_field_value(kind, field, raw, force=False):
    """Turn one `--set field=value` into the JSON value that field must hold.

    Explicit by design: an integer field rejects "6.5" instead of stringifying
    it, a boolean accepts only true/false, and an enum names its allowed set.
    `force` (from --force-value) lets an off-vocabulary enum value through with a
    loud warning, for a station that knows a value this tool does not; it never
    relaxes integers, booleans or the JSON shape.
    """
    spec = (PERSONA_TYPES if kind == "persona" else SHOW_TYPES)[field]
    enums = PERSONA_ENUMS if kind == "persona" else SHOW_ENUMS
    text = raw.strip()

    if spec == "string":
        return raw
    if spec == "integer":
        try:
            return int(text)
        except ValueError:
            raise ToolError("%s is an integer; %r is not one (values are never coerced)"
                            % (field, raw))
    if spec == "boolean":
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise ToolError("%s is a boolean: use true or false (got %r)" % (field, raw))
        return lowered == "true"
    if spec == "enum":
        allowed = enums[field]
        if text not in allowed:
            if not force:
                raise ToolError("%s must be one of: %s (got %r)"
                                % (field, ", ".join(allowed), raw))
            print("warning   %s %r is outside the known vocabulary (%s); --force-value sends "
                  "it anyway, and the station may still reject or normalise it"
                  % (field, text, ", ".join(allowed)), file=sys.stderr)
        return text
    if spec == "enum-list":
        allowed = enums[field]
        items = _split_list(raw)
        bad = [item for item in items if item not in allowed]
        if bad:
            if not force:
                raise ToolError("%s must be one of: %s (got %s)"
                                % (field, ", ".join(allowed),
                                   ", ".join(repr(item) for item in bad)))
            print("warning   %s %s outside the known vocabulary (%s); --force-value sends "
                  "them anyway, and the station may still reject or normalise them"
                  % (field, ", ".join(repr(item) for item in bad), ", ".join(allowed)),
                  file=sys.stderr)
        return items
    if spec == "list":
        return _split_list(raw)
    if spec == "json":
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ToolError("%s takes a JSON value (%s)" % (field, exc))
        if not isinstance(value, (list, dict)):
            raise ToolError("%s takes a JSON object or array (got %r)" % (field, raw))
        return value
    raise ToolError("internal: no edit type for %s.%s" % (kind, field))


def _short(value, limit=60):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def edit_entity(root, kind, args):
    """Change only the fields named by --set. Pure local file editing.

    Unnamed fields keep their value *and* their position: the document is loaded
    and written as an ordered dict, so assigning an existing key never moves it.
    The slug is the filename and the id is the station's identity, so neither is
    ever derived from a new name.
    """
    directory = "personas" if kind == "persona" else "shows"
    path = root / "character" / directory / ("%s.json" % args.slug)
    if not path.is_file():
        raise ToolError("no %s %r in %s; known: %s"
                        % (kind, args.slug, path.parent, ", ".join(_slugs(root, directory))
                           or "none"))
    fields = PERSONA_FIELDS if kind == "persona" else SHOW_FIELDS
    types = PERSONA_TYPES if kind == "persona" else SHOW_TYPES

    if not args.set:
        raise ToolError("nothing to change: pass at least one --set FIELD=VALUE")

    original_text = path.read_text(encoding="utf-8")
    document = json.loads(original_text)
    if not isinstance(document, dict):
        raise ToolError("%s must be a JSON object" % path)

    # Refuse to build on an invalid instance: an edit is not a repair tool, and
    # an error that predates the edit would be blamed on it.
    before_report = validate_instance(root)
    if before_report["errors"]:
        raise ToolError("instance/ already fails validate (%d error(s), first: %s). Fix that "
                        "before editing."
                        % (len(before_report["errors"]), before_report["errors"][0]))

    changes, seen = [], set()
    for assignment in args.set:
        if "=" not in assignment:
            raise ToolError("--set expects FIELD=VALUE (got %r)" % assignment)
        field, raw = assignment.split("=", 1)
        field = field.strip()
        if field == "id":
            raise ToolError("id is immutable: it is the station's identity for this %s and the "
                            "lockfile keys on it. Change other fields instead." % kind)
        if field not in fields:
            raise ToolError("unknown %s field %r. Fields: %s"
                            % (kind, field, ", ".join(sorted(fields))))
        if field in seen:
            raise ToolError("%s is set twice; give each field once" % field)
        if field not in types:
            raise ToolError("internal: no edit type for %s.%s" % (kind, field))
        seen.add(field)

        value = parse_field_value(kind, field, raw,
                                  force=getattr(args, "force_value", False))
        present = field in document
        if present and document[field] == value:
            continue
        changes.append((field, document.get(field), value, present))
        document[field] = value

    if not changes:
        print("no change %s %s: the named field(s) already hold those values"
              % (kind, args.slug))
        return EXIT_OK

    write_json(path, document)
    after_report = validate_instance(root)
    if after_report["errors"]:
        # Never leave a half-edited entity behind: validate is the gate apply
        # trusts, so an edit that breaks it must not survive.
        path.write_text(original_text, encoding="utf-8")
        raise ToolError("refusing to leave %s invalid (%s); the file was restored"
                        % (path.name, after_report["errors"][0]))

    print("edit      %s %s (%s)" % (kind, args.slug, path.name))
    for field, before, value, present in changes:
        print("changed   %-22s %s -> %s"
              % (field, _short(before) if present else "(absent)", _short(value)))
    print("kept      id %s, slug %s, %d other field(s) unchanged"
          % (document.get("id") or "(none)", args.slug, len(document) - len(changes)))
    print("next      subwave.py validate && subwave.py apply --dry-run")
    return EXIT_OK


def cmd_character(config, args):
    """Dispatch. Never touches the network: pure local file authoring."""
    root = INSTANCE_DIR
    action = args.character_command

    if action == "init":
        if root.exists() and not args.force:
            raise ToolError("instance/ already exists (%s). Refusing to overwrite: re-run with "
                            "--force to replace it." % describe_instance(root))
        if root.exists():
            shutil.rmtree(root)

        if args.source == "demo":
            source = REPO_ROOT / "examples" / "demo"
            if not source.is_dir():
                raise ToolError("examples/demo is missing from this checkout")
            shutil.copytree(source / "character", root / "character")
            shutil.copy2(source / "ids.lock.json", root / "ids.lock.json")
            print("init      copied examples/demo (the fictional station)")
        else:
            persona_slug, show_slug = "first-host", "first-show"
            persona = persona_defaults(persona_slug)
            show = show_defaults(show_slug, persona_id=persona["id"])
            write_json(root / "character" / "character.json", blank_character())
            write_json(root / "character" / "personas" / ("%s.json" % persona_slug), persona)
            write_json(root / "character" / "shows" / ("%s.json" % show_slug), show)
            write_json(root / "character" / "schedule.json",
                       {"schedule": {str(day): [show_slug] * 24 for day in range(7)},
                        "override": None})
            _save_lock(root, {
                "version": 1,
                "personas": {persona_slug: persona["id"]},
                "shows": {show_slug: show["id"]},
                "ids": {persona["id"]: persona_slug, show["id"]: show_slug},
                "capture": {"tier": 2, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "lossy": False, "lost": []},
            })
            print("init      wrote a blank character (%s, %s)" % (persona_slug, show_slug))
        print("next      subwave.py validate")
        return EXIT_OK

    _require_character(root)

    if action == "new-persona":
        slug = args.slug
        path = root / "character" / "personas" / ("%s.json" % slug)
        if path.exists():
            raise ToolError("persona %r already exists (%s)" % (slug, path))
        persona = persona_defaults(slug, args.name)
        write_json(path, persona)
        lock = _read_lock(root) or {"version": 1, "personas": {}, "shows": {}, "ids": {}}
        lock.setdefault("personas", {})[slug] = persona["id"]
        lock.setdefault("ids", {})[persona["id"]] = slug
        _save_lock(root, lock)
        print("persona   %s -> %s" % (slug, persona["id"]))
        print("note      placeholder id: personas are sent inside the settings array, which the "
              "station stores as given, so this id is preserved")
        print("next      fill in soul, then: subwave.py validate")
        return EXIT_OK

    if action == "new-show":
        slug = args.slug
        path = root / "character" / "shows" / ("%s.json" % slug)
        if path.exists():
            raise ToolError("show %r already exists (%s)" % (slug, path))
        lock = _read_lock(root) or {"version": 1, "personas": {}, "shows": {}, "ids": {}}
        personas = lock.get("personas") or {}
        if args.persona:
            if args.persona not in personas:
                raise ToolError("no persona %r in the lockfile; known: %s"
                                % (args.persona, ", ".join(sorted(personas)) or "none"))
            persona_slug, persona_id = args.persona, personas[args.persona]
        elif len(personas) == 1:
            persona_slug, persona_id = next(iter(personas.items()))
        else:
            raise ToolError("--persona is required; known personas: %s"
                            % (", ".join(sorted(personas)) or "none"))
        show = show_defaults(slug, args.name, persona_id)
        write_json(path, show)
        lock.setdefault("shows", {})[slug] = show["id"]
        lock.setdefault("ids", {})[show["id"]] = slug
        _save_lock(root, lock)
        print("show      %s -> %s (host %s)" % (slug, show["id"], persona_slug))
        print("note      placeholder id: it is dropped on apply, and POST /api/shows assigns a "
              "real one")
        print("note      no grid entry yet; use: character add-to-grid %s --days ..." % slug)
        print("next      fill in topic/genres/moods, then: subwave.py validate")
        return EXIT_OK

    if action == "add-to-grid":
        slug = args.slug
        if slug not in _slugs(root, "shows"):
            raise ToolError("no show %r in instance/character/shows; known: %s"
                            % (slug, ", ".join(_slugs(root, "shows")) or "none"))
        days = []
        for token in args.days.split(","):
            name = token.strip().lower()[:3]
            if name not in DAY_NAMES:
                raise ToolError("unknown day %r; use sun,mon,tue,wed,thu,fri,sat" % token.strip())
            days.append(DAY_NAMES[name])
        if not 0 <= args.from_hour <= 24 or not 0 <= args.to_hour <= 24:
            raise ToolError("hours must be 0..24")
        if args.from_hour >= args.to_hour:
            raise ToolError("--from must be earlier than --to (hours are half-open: 09 to 12 is "
                            "09:00, 10:00, 11:00)")

        path = root / "character" / "schedule.json"
        schedule_doc = json.loads(path.read_text(encoding="utf-8"))
        grid = schedule_doc.get("schedule") or {}
        hours = list(range(args.from_hour, args.to_hour))

        changes, conflicts = [], []
        for day in days:
            cells = grid.get(str(day)) or []
            for hour in hours:
                current = cells[hour] if hour < len(cells) else None
                if current == slug:
                    continue
                changes.append((day, hour, current))
                if current:
                    conflicts.append((day, hour, current))

        day_label = {v: k for k, v in DAY_NAMES.items()}
        if not changes:
            print("grid      no change: %s already owns those cells" % slug)
            return EXIT_OK
        for day, hour, current in changes:
            print("cell      %s %02d:00  %s -> %s"
                  % (day_label[day], hour, current or "(empty)", slug))
        if conflicts and not args.replace:
            sys.stdout.flush()  # show the cells before the refusal on stderr
            raise ToolError("%d cell(s) already hold another show (%s). Re-run with --replace to "
                            "take them over." % (len(conflicts), ", ".join(
                                sorted({c for _, _, c in conflicts}))))

        for day, hour, _ in changes:
            cells = grid.setdefault(str(day), [None] * 24)
            while len(cells) < 24:
                cells.append(None)
            cells[hour] = slug
        schedule_doc["schedule"] = grid
        write_json(path, schedule_doc)
        print("grid      %d cell(s) changed across %s" % (len(changes), ", ".join(
            sorted({day_label[d] for d, _, _ in changes}))))
        return EXIT_OK

    if action in ("edit-persona", "edit-show"):
        return edit_entity(root, "persona" if action == "edit-persona" else "show", args)

    raise ToolError("unknown character command %r" % action)


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

    character = sub.add_parser("character", help="author the character layer locally (no network)")
    char_sub = character.add_subparsers(dest="character_command", required=True)

    char_init = char_sub.add_parser("init", help="create instance/character/")
    char_init.add_argument("--from", dest="source", choices=("demo", "blank"), default="blank",
                           help="demo copies examples/demo; blank writes a placeholder station")
    char_init.add_argument("--force", action="store_true", help="replace an existing instance/")
    char_init.set_defaults(func=cmd_character)

    char_persona = char_sub.add_parser("new-persona", help="scaffold a persona with all 18 fields")
    char_persona.add_argument("slug")
    char_persona.add_argument("--name")
    char_persona.set_defaults(func=cmd_character)

    char_show = char_sub.add_parser("new-show", help="scaffold a show with all 26 fields")
    char_show.add_argument("slug")
    char_show.add_argument("--name")
    char_show.add_argument("--persona", help="persona slug to host it")
    char_show.set_defaults(func=cmd_character)

    char_grid = char_sub.add_parser("add-to-grid", help="fill grid cells with a show")
    char_grid.add_argument("slug")
    char_grid.add_argument("--days", required=True, help="comma separated: mon,tue")
    char_grid.add_argument("--from", dest="from_hour", type=int, required=True, help="first hour, 0-24")
    char_grid.add_argument("--to", dest="to_hour", type=int, required=True, help="hour after the last")
    char_grid.add_argument("--replace", action="store_true", help="take over cells that hold another show")
    char_grid.set_defaults(func=cmd_character)

    char_edit_persona = char_sub.add_parser(
        "edit-persona", help="change named fields of a persona; every other field is untouched")
    char_edit_persona.add_argument("slug")
    char_edit_persona.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                                   help="field to change, repeatable (e.g. --set warmth=7)")
    char_edit_persona.add_argument("--force-value", dest="force_value", action="store_true",
                                   help="allow a value outside the known vocabulary (warns loudly; "
                                        "for a station that knows more than this tool)")
    char_edit_persona.set_defaults(func=cmd_character)

    char_edit_show = char_sub.add_parser(
        "edit-show", help="change named fields of a show; every other field is untouched")
    char_edit_show.add_argument("slug")
    char_edit_show.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                                help="field to change, repeatable (e.g. --set banter=true)")
    char_edit_show.add_argument("--force-value", dest="force_value", action="store_true",
                                help="allow a value outside the known vocabulary (warns loudly; "
                                     "for a station that knows more than this tool)")
    char_edit_show.set_defaults(func=cmd_character)

    validate_cmd = sub.add_parser("validate", help="check instance/ before applying")
    validate_cmd.add_argument("--dir", default=None,
                              help="validate this station directory instead of instance/ "
                                   "(e.g. examples/demo)")
    validate_cmd.set_defaults(func=cmd_validate)

    apply_cmd = sub.add_parser("apply", help="push instance/ to the station (dry run by default)")
    apply_cmd.add_argument("--yes", action="store_true", help="perform the writes")
    apply_cmd.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    apply_cmd.add_argument("--force-now", action="store_true", help="write even while on air")
    apply_cmd.add_argument("--no-backup", action="store_true", help="write without a backup")
    apply_cmd.add_argument("--backup-dir", help="where to put the pre-apply backup")
    apply_cmd.add_argument("--prune", action="store_true", help="reserved; not implemented")
    apply_cmd.set_defaults(func=cmd_apply)

    backup = sub.add_parser("backup", help="copy config + assets outside the repo")
    backup.add_argument("--dir", help="base directory (default ~/.subwave-backups)")
    backup.add_argument("--with-library", action="store_true",
                        help="also copy library.db (audio analysis, large)")
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
