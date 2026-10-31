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


def ssh_run(config, remote_command, timeout=DEFAULT_TIMEOUT, binary=False, stdin=None):
    """Run a command on the host. Returns CompletedProcess."""
    if not config.host:
        raise ConfigError("SUBWAVE_HOST is not set")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", config.host, remote_command]
    try:
        return subprocess.run(argv, capture_output=True, text=not binary,
                              input=stdin, timeout=timeout)
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

# The six identity fields live in character.json and NOWHERE else. They used to
# be written into instance/settings.json as well, and because apply sent
# settings.json wholesale while character.json was only read, editing the
# character layer was a silent no-op: the stale copy won.
IDENTITY_KEYS = ("station", "stationDescription", "locale", "timezone", "theme", "djHouseRules")


def normalise_grid(grid):
    """7 days x 24 cells, with None for an empty hour.

    The station accepts null, "" and an absent day for "nothing" (schemas/
    schedule.ts), and stores null. Normalising here means pull round-trips a
    station with holes, and a blank day is 24 explicit nulls rather than a
    missing key.
    """
    week = {}
    for day in range(7):
        cells = (grid or {}).get(str(day)) or []
        week[str(day)] = [cells[index] if index < len(cells) else None
                          for index in range(24)]
    return week


def strip_settings(settings):
    """What belongs in instance/settings.json: no personas, no shows, no identity."""
    return {k: v for k, v in settings.items()
            if k not in ("personas", "activePersonaId", "shows") and k not in IDENTITY_KEYS}


def character_from(settings):
    """Identity fields. houseRules is normalised to a list: the station stores
    djHouseRules as a string (often empty), while the character layer wants one
    rule per list item."""
    character = {name: copy.deepcopy(settings.get(source)) for name, source in CHARACTER_FIELDS}
    # The station stores theme as {"active": id}; the character layer holds the id.
    theme = character.get("theme")
    if isinstance(theme, dict):
        character["theme"] = theme.get("active")

    rules = character.get("houseRules")
    if isinstance(rules, str):
        # The station stores djHouseRules as one newline-joined string. Pulling
        # it as a single element turned six rules into one blob.
        character["houseRules"] = [line.strip() for line in rules.split("\n") if line.strip()]
    elif isinstance(rules, list):
        character["houseRules"] = [str(rule).strip() for rule in rules if str(rule).strip()]
    else:
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
        "grid": normalise_grid(schedule.get("schedule")),
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


def _blocklist_key(item, kind):
    if not isinstance(item, dict):
        return repr(item)
    if kind == "rules":
        return item.get("id") or item.get("label") or "?"
    return "%s:%s" % (item.get("type"), item.get("id"))


def _wire_shape(item):
    """Drop toolkit-only keys (slug) so the station side compares like for like."""
    if not isinstance(item, dict):
        return item
    return {key: value for key, value in item.items() if key != "slug"}


def compare_skills(repo, live):
    added = sorted(set(repo) - set(live))
    removed = sorted(set(live) - set(repo))
    changed = sorted(slug for slug in set(repo) & set(live)
                     if repo[slug]["text"].strip() != live[slug]["text"].strip())
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged": sorted((set(repo) & set(live)) - set(changed))}


def compare_blocklist(repo, live):
    """Same added/removed/changed shape as personas and shows."""
    report = {}
    for kind in ("entries", "rules"):
        repo_items = {_blocklist_key(item, kind): _wire_shape(item)
                      for item in (repo.get(kind) or [])}
        live_items = {_blocklist_key(item, kind): _wire_shape(item)
                      for item in (live.get(kind) or [])}
        changed = sorted(key for key in set(repo_items) & set(live_items)
                         if repo_items[key] != live_items[key])
        report[kind] = {
            "added": sorted(set(repo_items) - set(live_items)),
            "removed": sorted(set(live_items) - set(repo_items)),
            "changed": changed,
            "unchanged": sorted((set(repo_items) & set(live_items)) - set(changed)),
        }
        if kind == "rules":
            # A rule's identity is its id; its label is what a human reads.
            report[kind]["labels"] = {
                key: (item.get("label") or key)
                # repo last so its label wins: that is the one being edited
                for key, item in list(live_items.items()) + list(repo_items.items())
            }
    return report


def print_blocklist_diff(report):
    for kind in ("entries", "rules"):
        part = report[kind]
        names = part.get("labels") or {}
        detail = []
        for label in ("changed", "added", "removed"):
            if part[label]:
                detail.append("%s: %s" % (label, ", ".join(names.get(key, key)
                                                           for key in part[label])))
        print("%-10s%d unchanged, %d changed, %d added, %d removed%s"
              % ("blocklist" if kind == "entries" else "",
                 len(part["unchanged"]), len(part["changed"]),
                 len(part["added"]), len(part["removed"]),
                 "  (" + "; ".join(detail) + ")" if detail else ""))


def print_skill_diff(report):
    detail = []
    for label in ("changed", "added", "removed"):
        if report[label]:
            detail.append("%s: %s" % (label, ", ".join(report[label])))
    print("%-10s%d unchanged, %d changed, %d added, %d removed%s"
          % ("skills", len(report["unchanged"]), len(report["changed"]),
             len(report["added"]), len(report["removed"]),
             "  (" + "; ".join(detail) + ")" if detail else ""))


def load_live_skills(config, tier):
    captured, _ = pull_skills(config, tier)
    return {slug: {"text": text} for slug, text in captured.items()}


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


# --------------------------------------------------------------------------
# blocklist: the third config document, and the one the station's own backup
# silently drops (backup/zip.ts INCLUDE_FILES lists jingles.json, jingles.m3u and
# sfx.json, not blocklist.json). Ours is meant to be complete, so it carries it.
# --------------------------------------------------------------------------

BLOCKLIST_FILE = "blocklist.json"
BLOCKLIST_ENTRY_TYPES = ("track", "album", "artist")
BLOCKLIST_RULE_FIELDS = ("genre", "tag", "mood", "artist", "album", "title", "playlist")
BLOCKLIST_RULES_MAX = 50
BLOCKLIST_LABEL_MAX = 64
BLOCKLIST_VALUES_MAX = 12
BLOCKLIST_VALUE_MAX = 64

# Fields the store mints and never accepts back; kept on capture so a rule can be
# identified later, stripped before any write.
BLOCKLIST_DERIVED = ("active", "matchCount")


def empty_blocklist():
    return {"entries": [], "rules": []}


def _clean_blocklist_item(item, keys):
    """Keep the stored shape only: the API adds live `active` and `matchCount`."""
    if not isinstance(item, dict):
        return item
    return {key: copy.deepcopy(item[key]) for key in keys if key in item}


def _clean_blocklist(payload):
    """Keep the stored shape: the API adds live `active` and `matchCount`."""
    payload = payload or {}
    return {
        "entries": [_clean_blocklist_item(entry, ("type", "id", "name", "artist", "album",
                                                  "addedAt"))
                    for entry in (payload.get("entries") or [])],
        "rules": [_clean_blocklist_item(rule, ("id", "label", "field", "values", "season",
                                               "showIds", "addedAt"))
                  for rule in (payload.get("rules") or [])],
    }


def read_blocklist_from_state(config, state=None):
    """Tier 2: the file IS the document, minus the fields we drop anyway.

    Reading it here keeps tier 2 free of a second credential. Someone who can
    read the state files as root should not also need the admin password to pull.
    """
    state = state or SshStateSource(config)
    if BLOCKLIST_FILE not in state.list_files():
        return empty_blocklist()
    try:
        payload = json.loads(state.read_bytes(BLOCKLIST_FILE).decode("utf-8"))
    except Exception as exc:
        raise ToolError("cannot read %s from the state dir (%s)" % (BLOCKLIST_FILE, exc))
    return _clean_blocklist(payload)


def fetch_blocklist(config):
    """Tier 1: GET /api/library/blocklist. Admin HTTP, and complete.

    This is the one entity tier 1 captures losslessly - unlike shows, which the
    API trims to 7 of 26 fields.
    """
    try:
        status, payload = http_json(config, "/api/library/blocklist", admin=True)
    except ConfigError as exc:
        raise ConfigError(
            "tier 1 reads the blocklist over the admin API, so it needs admin credentials: %s"
            % exc)
    if status == 404:
        return empty_blocklist()
    if status != 200:
        raise ToolError("GET /api/library/blocklist returned HTTP %s" % status)
    return _clean_blocklist(payload)


def blocklist_to_repo(document, lock):
    """Server show ids -> repo show slugs, so the document reads like the rest."""
    id_to_slug = {id_: slug for slug, id_ in (lock.get("shows") or {}).items()}
    result = copy.deepcopy(document)
    for rule in result.get("rules") or []:
        rule["showIds"] = [id_to_slug.get(id_, id_) for id_ in (rule.get("showIds") or [])]
    return result


def blocklist_to_live(document, lock):
    """Repo show slugs -> server show ids. Used by 3b; harmless here."""
    slug_to_id = lock.get("shows") or {}
    result = copy.deepcopy(document)
    for rule in result.get("rules") or []:
        rule["showIds"] = [slug_to_id.get(slug, slug) for slug in (rule.get("showIds") or [])]
    return result


def load_repo_blocklist(root=None):
    """instance/blocklist.json, or an empty document when the station has none."""
    path = Path(root or INSTANCE_DIR) / BLOCKLIST_FILE
    if not path.is_file():
        return empty_blocklist()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ToolError("%s is not valid JSON (%s)" % (path, exc))
    if not isinstance(document, dict):
        raise ToolError("%s must be a JSON object" % path)
    document.setdefault("entries", [])
    document.setdefault("rules", [])
    return document


def validate_blocklist(document, show_slugs, errors, warnings):
    """Field enum, length limits, season ranges, and show references."""
    entries = document.get("entries")
    rules = document.get("rules")
    if not isinstance(entries, list):
        errors.append("blocklist.json: 'entries' must be a list")
        entries = []
    if not isinstance(rules, list):
        errors.append("blocklist.json: 'rules' must be a list")
        rules = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append("blocklist.json: entries[%d] must be an object" % index)
            continue
        if entry.get("type") not in BLOCKLIST_ENTRY_TYPES:
            errors.append("blocklist.json: entries[%d].type %r must be one of: %s"
                          % (index, entry.get("type"), ", ".join(BLOCKLIST_ENTRY_TYPES)))
        if not entry.get("id"):
            errors.append("blocklist.json: entries[%d] has no id" % index)

    seen_slugs = {}
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        slug = rule.get("slug")
        if not slug:
            errors.append("blocklist.json: rules[%d] has no slug; every rule needs one "
                          "(it is how apply knows which station uuid to update)" % index)
        elif slug in seen_slugs:
            errors.append("blocklist.json: rules[%d] and rules[%d] share the slug %r"
                          % (seen_slugs[slug], index, slug))
        else:
            seen_slugs[slug] = index

    if len(rules) > BLOCKLIST_RULES_MAX:
        errors.append("blocklist.json: %d rules, the station accepts at most %d"
                      % (len(rules), BLOCKLIST_RULES_MAX))

    for index, rule in enumerate(rules):
        where = "blocklist.json: rules[%d]" % index
        if not isinstance(rule, dict):
            errors.append("%s must be an object" % where)
            continue
        label = rule.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append("%s.label is required" % where)
        elif len(label) > BLOCKLIST_LABEL_MAX:
            errors.append("%s.label is %d chars, the station accepts at most %d"
                          % (where, len(label), BLOCKLIST_LABEL_MAX))
        if rule.get("field") not in BLOCKLIST_RULE_FIELDS:
            errors.append("%s.field %r must be one of: %s"
                          % (where, rule.get("field"), ", ".join(BLOCKLIST_RULE_FIELDS)))
        values = rule.get("values")
        if not isinstance(values, list) or not values:
            errors.append("%s.values must be a non-empty list" % where)
        elif not 1 <= len(values) <= BLOCKLIST_VALUES_MAX:
            errors.append("%s.values has %d entries, the station accepts 1..%d"
                          % (where, len(values), BLOCKLIST_VALUES_MAX))
        else:
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    errors.append("%s.values must be non-empty strings" % where)
                elif len(value) > BLOCKLIST_VALUE_MAX:
                    errors.append("%s.values entry is %d chars, at most %d"
                                  % (where, len(value), BLOCKLIST_VALUE_MAX))
        season = rule.get("season")
        if season is not None:
            if not isinstance(season, dict):
                errors.append("%s.season must be an object with from/to month and day" % where)
            else:
                for key in ("from", "to"):
                    window = season.get(key)
                    if not isinstance(window, dict):
                        errors.append("%s.season.%s must be an object" % (where, key))
                        continue
                    month, day = window.get("month"), window.get("day")
                    if not isinstance(month, int) or not 1 <= month <= 12:
                        errors.append("%s.season.%s.month must be 1..12 (got %r)"
                                      % (where, key, month))
                    if not isinstance(day, int) or not 1 <= day <= 31:
                        errors.append("%s.season.%s.day must be 1..31 (got %r)"
                                      % (where, key, day))
        for slug in rule.get("showIds") or []:
            if slug in show_slugs:
                continue
            if isinstance(slug, str) and re.match(r"^[a-z0-9_]{3,32}$", slug):
                # A server id left in the document is inert on the station: it
                # simply never matches a show. Warn, do not fail the capture.
                warnings.append("%s.showIds %r looks like a server id, not a show slug; "
                                "it will never match" % (where, slug))
            else:
                warnings.append("%s.showIds %r is not a show in this repo" % (where, slug))


def cmd_pull(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)

    previous_lock = {}
    previous_blocklist_lock = {"version": 1, "rules": {}, "ids": {}}
    if INSTANCE_DIR.exists():
        if not args.force:
            raise ToolError(
                "instance/ already exists (%s). Refusing to overwrite: re-run with --force to "
                "replace it, which would lose that capture." % describe_instance())
        lock_path = INSTANCE_DIR / "ids.lock.json"
        if lock_path.is_file():
            previous_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if (INSTANCE_DIR / BLOCKLIST_LOCK).is_file():
            previous_blocklist_lock = load_blocklist_lock()
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

    # Read the blocklist before writing anything, so a failure cannot leave
    # instance/ half-written. Tier 2 reads the file it already has access to;
    # tier 1 has no filesystem, so it uses the admin API.
    blocklist_live = (read_blocklist_from_state(config) if tier == 2
                      else fetch_blocklist(config))

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

    # The blocklist is the third config document, and admin HTTP returns it whole
    # at BOTH tiers - the one entity tier 1 captures losslessly.
    lock_now = json.loads((INSTANCE_DIR / "ids.lock.json").read_text(encoding="utf-8"))
    blocklist = blocklist_to_repo(blocklist_live, lock_now)

    # A rule's slug is ours; its uuid is the station's. Keep both so a re-apply
    # updates the same rule instead of minting a near-duplicate.
    assign_rule_slugs(blocklist["rules"], previous_blocklist_lock.get("rules") or {})
    write_json(INSTANCE_DIR / BLOCKLIST_FILE, blocklist)
    rule_lock = {"version": 1, "rules": {}, "ids": {}}
    for rule in blocklist["rules"]:
        uuid = rule.get("id")
        if uuid:
            rule_lock["rules"][rule_slug(rule)] = uuid
            rule_lock["ids"][uuid] = rule_slug(rule)
    save_blocklist_lock(rule_lock)
    summary["files"] += 1

    print("wrote     instance/ (%d files + ids.lock.json)" % summary["files"])
    print("blocklist %d entries, %d rules"
          % (len(blocklist["entries"]), len(blocklist["rules"])))

    captured_skills, builtin_skills = pull_skills(config, tier)
    write_repo_skills(captured_skills)
    print("skills    %d custom captured%s"
          % (len(captured_skills),
             "" if not builtin_skills else
             " (%d built-in not captured: they are the image's, not the operator's)"
             % len(builtin_skills)))
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
    repo_blocklist = load_repo_blocklist()
    live_blocklist = blocklist_to_repo(fetch_blocklist(config), json.loads(
        (INSTANCE_DIR / "ids.lock.json").read_text(encoding="utf-8")))
    blocklist_report = compare_blocklist(repo_blocklist, live_blocklist)
    print_blocklist_diff(blocklist_report)
    skills_report = {"added": [], "removed": [], "changed": [], "unchanged": []}
    if skills_root().is_dir():
        skills_report = compare_skills(load_repo_skills(), load_live_skills(config, 1))
        print_skill_diff(skills_report)
    count = drift_count(report)
    count += sum(len(blocklist_report[kind][label]) for kind in ("entries", "rules")
                 for label in ("added", "removed", "changed"))
    count += sum(len(skills_report[label]) for label in ("added", "removed", "changed"))
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

    def write_bytes(self, relative, payload):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def remove(self, relative):
        target = self.root / relative
        if target.exists():
            target.unlink()


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

    def write_bytes(self, relative, payload):
        path = "%s/%s" % (self.config.state_dir, relative)
        parent = path.rsplit("/", 1)[0]
        made = ssh_run(self.config, "docker exec %s mkdir -p '%s'"
                       % (self.config.container, parent))
        if made.returncode != 0:
            raise ToolError("cannot create %s: %s"
                            % (parent, (made.stderr or "").strip() or "unknown error"))
        result = ssh_run(self.config,
                         "docker exec -i %s sh -c \"cat > '%s'\""
                         % (self.config.container, path),
                         binary=True, stdin=payload)
        if result.returncode != 0:
            raise ToolError("cannot write %s: %s" % (
                relative, (result.stderr or b"").decode("utf-8", "replace").strip()
                or "unknown error"))

    def remove(self, relative):
        path = "%s/%s" % (self.config.state_dir, relative)
        result = ssh_run(self.config, "docker exec %s rm -f '%s'"
                         % (self.config.container, path))
        if result.returncode != 0:
            raise ToolError("cannot remove %s: %s"
                            % (path, (result.stderr or "").strip() or "unknown error"))


def select_backup_files(files, with_library=False):
    """Split a state listing into what we copy and what we skip, with reasons."""
    wanted, skipped = [], []
    for relative in files:
        name = relative.split("/")[0]
        if relative in ("settings.json", "schedule.json", BLOCKLIST_FILE):
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
        if relative in ("settings.json", "schedule.json", BLOCKLIST_FILE):
            continue
        if "/" in relative:
            head = relative.split("/")[0]
            groups[head] = groups.get(head, 0) + 1
        else:
            loose.append(relative)
    print("path      %s" % destination)
    print("files     %d (%d bytes)" % (manifest["files"], manifest["bytes"]))
    carried = ["settings.json", "schedule.json"]
    if BLOCKLIST_FILE in manifest["copied"]:
        carried.append(BLOCKLIST_FILE)
    print("copied    %s%s" % (", ".join(carried),
                              ", " + ", ".join(sorted(loose)) if loose else ""))
    for head in sorted(groups):
        print("          %s/ (%d file%s)" % (head, groups[head], "" if groups[head] == 1 else "s"))
    for note in manifest.get("notes") or []:
        print("note      %s" % note)
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
    # Two backups in the same second must not collide: the stamp is per second
    # and a prune always backs up before deleting.
    suffix = 2
    while destination.exists():
        destination = base / slugify(station) / ("%s-%d" % (stamp, suffix))
        suffix += 1

    resolved = destination.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ToolError(
            "refusing to write a backup inside the repo (%s): it would contain live secrets. "
            "Choose a directory outside %s." % (resolved, REPO_ROOT))

    destination.mkdir(parents=True, exist_ok=False)
    (destination / "settings.json").write_text(settings_text, encoding="utf-8")
    (destination / "schedule.json").write_text(schedule_text, encoding="utf-8")

    blocklist_text = None
    if BLOCKLIST_FILE in state.list_files():
        blocklist_text = state.read_bytes(BLOCKLIST_FILE).decode("utf-8")
        (destination / BLOCKLIST_FILE).write_text(blocklist_text, encoding="utf-8")

    total = len(settings_text) + len(schedule_text) + (len(blocklist_text) if blocklist_text else 0)
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
        "files": 2 + (1 if blocklist_text is not None else 0) + len(wanted),
        "bytes": total,
        "copied": ["settings.json", "schedule.json"]
                  + ([BLOCKLIST_FILE] if blocklist_text is not None else [])
                  + wanted,
        "notes": ["the station's own API zip omits blocklist.json, so a stock export/restore "
                  "silently loses every ban; this backup carries it"],
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
    live_blocklist = blocklist_to_repo(fetch_blocklist(config), json.loads(
        (INSTANCE_DIR / "ids.lock.json").read_text(encoding="utf-8")))
    blocklist_report = compare_blocklist(load_repo_blocklist(), live_blocklist)
    print_blocklist_diff(blocklist_report)
    skills_report = {"added": [], "removed": [], "changed": [], "unchanged": []}
    if skills_root().is_dir():
        skills_report = compare_skills(load_repo_skills(), load_live_skills(config, 1))
        print_skill_diff(skills_report)
    count = (len(report["personas"]["added"]) + len(report["personas"]["removed"])
             + len(report["personas"]["changed"]) + len(report["shows"]["added"])
             + len(report["shows"]["removed"]) + len(report["shows"]["changed"])
             + len(report["grid"]["changed_days"]))
    if not report["override"]["unchanged"]:
        count += 1
    count += sum(len(blocklist_report[kind][label]) for kind in ("entries", "rules")
                 for label in ("added", "removed", "changed"))
    count += sum(len(skills_report[label]) for label in ("added", "removed", "changed"))
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


def rules_to_string(rules):
    """The character layer holds one rule per list item; the station holds one
    newline-joined string. Join here so a 3-rule list round-trips losslessly."""
    if isinstance(rules, str):
        return rules
    return "\n".join(str(rule).strip() for rule in (rules or []) if str(rule).strip())


def normalise_festivals(festivals):
    """Mirror the station's own transform (schemas/settings.ts): every festival
    is stored with month, day, name, mood, description and windowDays. Sending
    the short form made the station add defaults, so the payload churned on every
    apply and diff-after-apply was never clean."""
    if not isinstance(festivals, list):
        return festivals
    normalised = []
    for entry in festivals:
        if not isinstance(entry, dict):
            normalised.append(entry)
            continue
        normalised.append({
            "month": int(entry.get("month", 1)),
            "day": int(entry.get("day", 1)),
            "name": str(entry.get("name") or "").strip(),
            "mood": str(entry.get("mood") or "").strip(),
            "description": str(entry.get("description") or "").strip(),
            "windowDays": int(entry.get("windowDays") or 0),
        })
    return normalised


def settings_payload(model):
    """The settings section as it must be SENT.

    settings.json no longer carries the identity fields - character.json is their
    only home - so they are merged back here. Without this, apply wrote the stale
    copy and editing the character layer did nothing.
    """
    payload = copy.deepcopy(model.get("settings") or {})
    character = model.get("character") or {}
    for field, target in CHARACTER_FIELDS:
        if field not in character:
            continue
        value = character[field]
        if target == "djHouseRules":
            value = rules_to_string(value)
        elif target == "theme" and isinstance(value, str):
            value = {"active": value}
        payload[target] = value
    payload["festivals"] = normalise_festivals(payload.get("festivals"))
    return payload


# Prefixes of every secret path, for deciding what a message may say. A
# verification step must never be able to print a credential, so this is
# structural: the path decides, not the value.
SECRET_PATH_PREFIXES = tuple(
    ".".join(part for part in path if part != "*") for path in REDACT_PATHS)


def _is_secret_path(path):
    return any(path == prefix or path.startswith(prefix + ".")
               for prefix in SECRET_PATH_PREFIXES)


def _describe(path, expected, actual):
    """One problem line. Secret paths are named, never printed."""
    clean = path.rstrip(".") or "value"
    if _is_secret_path(clean):
        return "%s (secret; the station does not hold what was sent)" % clean
    return "%s (sent %r, station has %r)" % (clean, expected, actual)


def _diff_values(expected, actual, path=""):
    """Paths where the station does not hold what we sent."""
    problems = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                problems.append("%s%s (missing on the station)" % (path, key))
            else:
                problems += _diff_values(expected[key], actual[key], "%s%s." % (path, key))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            problems.append("%s (sent %d item(s), station has %d)"
                            % (path.rstrip(".") or "list", len(expected), len(actual)))
        else:
            for index, (left, right) in enumerate(zip(expected, actual)):
                problems += _diff_values(left, right, "%s[%d]." % (path, index))
    elif expected != actual:
        problems.append(_describe(path, expected, actual))
    return problems


def verify_written(config, tier, steps, grid_body):
    """Re-read the station and prove every field we sent actually landed.

    A silent no-op is the worst failure this tool has, so this fails loudly and
    names the field rather than warning.
    """
    problems = []
    if tier == 2:
        settings_now = json.loads(read_state_file(config, "settings.json"))
        schedule_now = json.loads(read_state_file(config, "schedule.json"))
        for step in steps:
            if step["entity"] == "settings":
                for key, sent in step["body"].items():
                    problems += _diff_values(sent, settings_now.get(key), "%s." % key)
            elif step["entity"] == "show":
                sent = step["body"]["show"]
                # A scaffolded show was sent without an id, so match by name.
                found = next((show for show in (schedule_now.get("shows") or [])
                              if (sent.get("id") and show.get("id") == sent.get("id"))
                              or (not sent.get("id") and show.get("name") == sent.get("name"))), None)
                if found is None:
                    problems.append("shows: %r is not on the station" % sent.get("name"))
                else:
                    problems += _diff_values(sent, found, "show %r: " % sent.get("name"))
        problems += _diff_values(grid_body.get("schedule"),
                                 (schedule_now.get("schedule") or {}), "schedule.")
    else:
        status, schedule_now = http_json(config, "/api/schedule")
        if status != 200:
            problems.append("could not read the station back (HTTP %s)" % status)
        else:
            problems += _diff_values(grid_body.get("schedule"),
                                     (schedule_now.get("schedule") or {}), "schedule.")
            for step in steps:
                if step["entity"] != "show":
                    continue
                sent = step["body"]["show"]
                found = next((show for show in (schedule_now.get("shows") or [])
                              if (sent.get("id") and show.get("id") == sent.get("id"))
                              or (not sent.get("id") and show.get("name") == sent.get("name"))), None)
                if found is None:
                    problems.append("shows: %r is not on the station" % sent.get("name"))
                    continue
                for key, value in found.items():
                    if key in sent and sent[key] != value:
                        problems.append("show %r: %s (sent %r, station has %r)"
                                        % (sent.get("name"), key, sent[key], value))
    return problems


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
    settings = settings_payload(model)
    if settings:
        steps.append({"method": "POST", "path": "/api/settings", "entity": "settings",
                      "label": "settings + character", "body": settings,
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


def payload_hash(steps, grid_body, blocklist=None, skills=None):
    """Everything apply writes, so a blocklist- or skill-only edit is not a no-op."""
    blob = json.dumps(
        {"steps": [{"method": step["method"], "path": step["path"], "body": step["body"]}
                   for step in steps],
         "grid": grid_body,
         "blocklist": blocklist or {},
         "skills": skills or {}},
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


def apply_blocklist(config, document, lock):
    """Entries then rules. A 409 on an entry is success: already blocked."""
    lock = copy.deepcopy(lock)
    lock.setdefault("rules", {})
    lock.setdefault("ids", {})

    for entry in document.get("entries") or []:
        status, _ = http_json(config, "/api/library/blocklist", admin=True, method="POST",
                              body=blocklist_wire_entry(entry))
        label = "%s %s" % (entry.get("type"), entry.get("id"))
        if status == 409:
            print("blocked   %s (already blocked)" % label)
        elif status >= 400:
            raise ToolError("POST /api/library/blocklist failed for %s: HTTP %s"
                            % (label, status))
        else:
            print("blocked   %s" % label)

    for rule in document.get("rules") or []:
        slug = rule_slug(rule)
        uuid = lock["rules"].get(slug)
        body = blocklist_wire_rule(rule)
        if uuid:
            status, payload = http_json(
                config, "/api/library/blocklist/rules/%s" % uuid, admin=True,
                method="PUT", body=body)
            verb = "updated"
        else:
            status, payload = http_json(
                config, "/api/library/blocklist/rules", admin=True, method="POST", body=body)
            verb = "added"
        if status >= 400:
            raise ToolError("%s rule %r failed: HTTP %s"
                            % (verb, slug, status))
        minted = (payload or {}).get("id")
        if minted and minted != uuid:
            lock["rules"][slug] = minted
            lock["ids"][minted] = slug
            rule["id"] = minted
        print("rule      %s %s" % (verb, slug))

    save_blocklist_lock(lock)
    save_repo_blocklist(document)
    return lock


# --------------------------------------------------------------------------
# prune: the only destructive operation in the toolkit
#
# Entities present on the STATION but absent from the repo are removed. It is
# opt-in, never implied by a plain apply, dry by default, and always preceded by
# a backup. The station's delete routes have no undo - a re-added rule gets a new
# uuid, a deleted persona's shows lose their host - so a cascade is never silent.
# --------------------------------------------------------------------------

DAY_LABELS = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}


def _grid_cells_for(show_id, grid):
    """(day, hour) pairs where this show occupies a cell."""
    cells = []
    for day, row in (grid or {}).items():
        for hour, cell in enumerate(row or []):
            if cell == show_id:
                cells.append((str(day), hour))
    return cells


def prune_plan(config, model, blocklist_lock, settings_now=None, schedule_now=None):
    """What a prune would delete, what stands in the way, and what cannot be undone.

    Takes the station documents as they were BEFORE the writes. The grid write
    clears any cell that is not in the repo, so reading the grid afterwards would
    always report "occupies nothing" and the guard would never fire.
    """
    if settings_now is None:
        settings_now = json.loads(read_state_file(config, "settings.json"))
    if schedule_now is None:
        schedule_now = json.loads(read_state_file(config, "schedule.json"))

    live_personas = {p.get("id"): p for p in (settings_now.get("personas") or []) if p.get("id")}
    live_shows = {s.get("id"): s for s in (schedule_now.get("shows") or []) if s.get("id")}
    grid = schedule_now.get("schedule") or {}

    repo_personas = {id_ for id_ in (model.get("personas") or {}) if not id_.startswith("new:")}
    repo_shows = {id_ for id_ in (model.get("shows") or {}) if not id_.startswith("new:")}

    shows = {id_: show for id_, show in live_shows.items() if id_ not in repo_shows}
    personas = {id_: persona for id_, persona in live_personas.items()
                if id_ not in repo_personas}

    hard, blocked, items = [], [], []

    active = settings_now.get("activePersonaId")
    if active in personas:
        hard.append("persona %r is the active persona; a station with no DJ cannot be pruned"
                    % (personas[active].get("name") or active))
        personas.pop(active, None)

    if personas and len(live_personas) - len(personas) == 0:
        hard.append("refusing to delete every persona: a station with no persona has no DJ")
        personas = {}

    if shows and len(live_shows) - len(shows) == 0:
        hard.append("refusing to delete the last show: the grid would have nothing to schedule")
        shows = {}

    for id_, show in sorted(shows.items()):
        cells = _grid_cells_for(id_, grid)
        label = show.get("name") or id_
        if cells:
            where = ", ".join("%s %02d:00" % (DAY_LABELS[int(day)], hour) for day, hour in cells)
            blocked.append("show %r occupies %d grid cell(s): %s" % (label, len(cells), where))
        items.append({"kind": "show", "id": id_, "label": label, "cells": cells})

    for id_, persona in sorted(personas.items()):
        hosts = [show.get("name") or show.get("id")
                 for show in live_shows.values() if show.get("personaId") == id_]
        if hosts:
            blocked.append("persona %r hosts %d show(s): %s"
                           % (persona.get("name") or id_, len(hosts), ", ".join(hosts)))
        items.append({"kind": "persona", "id": id_, "label": persona.get("name") or id_,
                      "hosts": hosts})

    rules = []
    for slug in pending_removals(load_repo_blocklist(), blocklist_lock):
        uuid = (blocklist_lock.get("rules") or {}).get(slug)
        if uuid:
            rules.append({"kind": "rule", "id": uuid, "label": slug, "slug": slug})
            items.append(rules[-1])

    return {"items": items, "hard": hard, "blocked": blocked}


def print_prune_plan(plan, dry_run):
    for item in plan["items"]:
        if item["kind"] == "show":
            detail = ("occupies %d cell(s)" % len(item["cells"])) if item["cells"] else \
                     "in no grid cell"
        elif item["kind"] == "persona":
            detail = ("hosts %s" % ", ".join(item["hosts"])) if item["hosts"] else "hosts nothing"
        else:
            detail = "pending removal in blocklist.lock.json"
        print("delete    %-8s %-24s (%s)" % (item["kind"], item["label"], detail))
    for line in plan["blocked"]:
        print("blocked   %s" % line)
    for line in plan["hard"]:
        print("refused   %s" % line)


def clear_show_cells(show_ids, lock):
    """After a forced show delete, empty its cells in the repo so the next apply agrees."""
    path = INSTANCE_DIR / "character" / "schedule.json"
    if not path.is_file():
        return 0
    document = json.loads(path.read_text(encoding="utf-8"))
    id_to_slug = {uuid: slug for slug, uuid in (lock.get("shows") or {}).items()}
    slugs = {id_to_slug.get(id_, id_) for id_ in show_ids}
    cleared = 0
    for day, row in (document.get("schedule") or {}).items():
        for hour, cell in enumerate(row or []):
            if cell in slugs:
                row[hour] = None
                cleared += 1
    if cleared:
        write_json(path, document)
    return cleared


def run_prune(config, plan, force):
    """Delete, one entity at a time, printing each. Returns the number deleted."""
    deleted = 0
    for item in plan["items"]:
        if item["kind"] == "show":
            status, _ = http_json(config, "/api/shows/%s" % item["id"], admin=True,
                                  method="DELETE")
            if status >= 400:
                raise ToolError("DELETE /api/shows/%s failed: HTTP %s" % (item["id"], status))
            print("deleted   show %r" % item["label"])
            if item["cells"]:
                ids_lock = json.loads(
                    (INSTANCE_DIR / "ids.lock.json").read_text(encoding="utf-8"))
                cleared = clear_show_cells([item["id"]], ids_lock)
                print("cleared   %d grid cell(s) that pointed at it, in the repo" % cleared)
            deleted += 1
        elif item["kind"] == "persona":
            status, _ = http_json(config, "/api/personas/%s" % item["id"], admin=True,
                                  method="DELETE")
            if status >= 400:
                raise ToolError("DELETE /api/personas/%s failed: HTTP %s" % (item["id"], status))
            print("deleted   persona %r" % item["label"])
            if item["hosts"]:
                print("dangling  %s now has no host; validate will report it"
                      % ", ".join(item["hosts"]))
            deleted += 1
        else:
            status, _ = http_json(config, "/api/library/blocklist/rules/%s" % item["id"],
                                  admin=True, method="DELETE")
            if status >= 400:
                raise ToolError("DELETE /api/library/blocklist/rules/%s failed: HTTP %s"
                                % (item["id"], status))
            print("deleted   blocklist rule %r (pending removal)" % item["label"])
            deleted += 1
    return deleted


def cmd_apply(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)

    if args.prune and args.no_backup:
        raise ToolError("--prune cannot run with --no-backup: deleting on the station has no undo, "
                        "so a backup is mandatory. Drop --no-backup.")
    if args.prune and tier != 2:
        raise ToolError("--prune needs host access: a tier-1 backup cannot be taken, and a delete "
                        "without one is not offered.")

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
        if args.prune:
            plan = prune_plan(config, load_instance_model(), load_blocklist_lock())
            print("prune     %d deletion(s) planned" % len(plan["items"]))
            print_prune_plan(plan, dry_run=True)
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
        destination, _ = take_backup(config, args.backup_dir)
        print("backup    %s" % destination)
    else:
        raise ToolError(
            "refusing to write at tier 1 without a backup: a tier-1 backup cannot include the "
            "schedule or the full show definitions, so this write would be unprotected. Pass "
            "--no-backup to accept that explicitly.")

    repo_blocklist = load_repo_blocklist()
    blocklist_lock = load_blocklist_lock()
    prune_settings_before = prune_schedule_before = None
    if args.prune:
        prune_settings_before = json.loads(read_state_file(config, "settings.json"))
        prune_schedule_before = json.loads(read_state_file(config, "schedule.json"))

    repo_skills_text = {slug: entry["text"] for slug, entry in load_repo_skills().items()}
    digest = payload_hash(steps, grid_body, repo_blocklist, repo_skills_text)
    applied = lock.get("applied") or {}
    if applied.get("hash") == digest and not args.prune:
        print("result    no changes since the last apply (%s)" % applied.get("at"))
        return EXIT_OK
    if applied.get("hash") == digest:
        # Nothing to write, but a prune was asked for: fall through to it.
        print("result    no changes to write")

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

    # Blocklist last, and it never deletes. A rule removed locally is reported
    # and left alone: deletes are their own task.
    for slug in pending_removals(repo_blocklist, blocklist_lock):
        print("pending   rule %r is in the lock but not in the file; apply does not delete "
              "(that is the deletes task)" % slug)
    apply_blocklist(config, repo_blocklist, blocklist_lock)
    apply_skills(config, load_repo_skills())

    # The writes succeeded, so record that before anything else can fail: a
    # verification that throws must not erase the fact that the station changed.
    lock["applied"] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hash": payload_hash(build_steps(model, lock), grid_body, load_repo_blocklist(),
                             {slug: entry["text"]
                              for slug, entry in load_repo_skills().items()}),
        "tier": tier,
    }
    write_json(lock_path, lock)

    # Prove it landed. A 200 means "accepted", not "stored what we sent", and a
    # silent no-op is the worst failure this tool has. Compare against the steps
    # that were SENT (rehydrated), never a rebuild from the model: rebuilding
    # compares against __REDACTED__ and cries wolf on every secret.
    problems = verify_written(config, tier, steps, grid_body)
    if problems:
        shown = problems[:10]
        more = "" if len(problems) <= 10 else " (+%d more)" % (len(problems) - 10)
        raise ToolError(
            "the station did not store what was sent, so the change did NOT land:\n  %s%s"
            % ("\n  ".join(shown), more))
    print("result    %d step(s) written; ids.lock.json updated" % (len(steps) + 1))

    # Prune last, after the writes and after verification: a failed write must
    # never trigger a delete, and verification raising stops us before this line.
    if args.prune:
        plan = prune_plan(config, load_instance_model(), blocklist_lock,
                          settings_now=prune_settings_before,
                          schedule_now=prune_schedule_before)
        print("prune     %d deletion(s) planned" % len(plan["items"]))
        print_prune_plan(plan, dry_run=False)
        if plan["hard"]:
            raise ToolError(
                "refusing to prune: %s. The writes above were applied; nothing was deleted."
                % plan["hard"][0])
        if plan["blocked"] and not args.force_prune:
            raise ToolError(
                "refusing to prune: %s. The writes above were applied; nothing was deleted. "
                "Pass --force-prune to delete anyway." % plan["blocked"][0])
        if not plan["items"]:
            print("result    nothing to prune")
            return EXIT_OK
        deleted = run_prune(config, plan, args.force_prune)
        print("result    %d deletion(s) done" % deleted)
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
    counts = {"personas": 0, "shows": 0, "cells": 0, "filled": 0, "empty": 0}

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
                        # An empty hour is legal: the station runs autonomously
                        # for that hour (Admin & Settings > Shows). Rejecting it
                        # blocked a legitimate console action.
                        counts["empty"] += 1
                    elif cell not in shows:
                        errors.append("schedule.json: day %s hour %02d names %r, which is not "
                                      "a show file" % (day, hour, cell))
                    else:
                        counts["filled"] += 1

    validate_skills(root, errors, warnings)

    blocklist_path = root / BLOCKLIST_FILE
    if blocklist_path.is_file():
        blocklist = _load_json(blocklist_path, unreadable)
        if blocklist is not None:
            validate_blocklist(blocklist, set(shows), errors, warnings)

            lock_path = root / BLOCKLIST_LOCK
            blocklist_lock = _load_json(lock_path, unreadable) if lock_path.is_file() else None
            if isinstance(blocklist_lock, dict):
                uuids = blocklist_lock.get("rules") or {}
                seen_uuids = {}
                for slug, uuid in sorted(uuids.items()):
                    if uuid in seen_uuids:
                        errors.append("blocklist.lock.json: %r and %r share the uuid %r"
                                      % (seen_uuids[uuid], slug, uuid))
                    seen_uuids[uuid] = slug
                for slug in pending_removals(blocklist, blocklist_lock):
                    warnings.append("blocklist.lock.json: rule %r is in the lock but not in "
                                    "the file - a pending removal, which apply will not act on"
                                    % slug)

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
    detail = "%d/%d cells filled" % (counts["filled"], counts["cells"])
    if counts["empty"]:
        detail += ", %d empty (legal: the station runs autonomously)" % counts["empty"]
    print("result    ok (%d personas, %d shows, %s), %d warning(s)"
          % (counts["personas"], counts["shows"], detail, len(result["warnings"])))
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


# --------------------------------------------------------------------------
# blocklist authoring: slugs the toolkit owns, uuids the station mints
#
# A rule's id is a server-minted uuid and there is no slug on the station. If we
# re-applied without a lock, every apply would mint a NEW uuid for the same rule,
# filling RULES_MAX=50 with near-duplicates that each block a slightly different
# subset - while matchCount still looked plausible. That is the trap this lock
# exists to prevent.
# --------------------------------------------------------------------------

BLOCKLIST_LOCK = "blocklist.lock.json"


def load_blocklist_lock(root=None):
    path = Path(root or INSTANCE_DIR) / BLOCKLIST_LOCK
    if not path.is_file():
        return {"version": 1, "rules": {}, "ids": {}}
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ToolError("%s is not valid JSON (%s)" % (path, exc))
    lock.setdefault("version", 1)
    lock.setdefault("rules", {})
    lock.setdefault("ids", {})
    return lock


def save_blocklist_lock(lock, root=None):
    write_json(Path(root or INSTANCE_DIR) / BLOCKLIST_LOCK, lock)


def assign_rule_slugs(rules, lock_rules):
    """Give every rule a slug, stable by uuid. Mutates and returns the rules."""
    by_uuid = {uuid: slug for slug, uuid in lock_rules.items()}
    used = set(lock_rules)
    for rule in rules:
        uuid = rule.get("id")
        slug = None
        if uuid and uuid in by_uuid:
            slug = by_uuid[uuid]
        elif rule.get("slug"):
            slug = rule["slug"]
        if not slug:
            base = slugify(rule.get("label") or "rule")
            slug = base
            suffix = 2
            while slug in used:
                slug = "%s-%d" % (base, suffix)
                suffix += 1
        rule["slug"] = slug
        used.add(slug)
    return rules


def rule_slug(rule):
    return rule.get("slug") or slugify(rule.get("label") or "rule")


def blocklist_wire_rule(rule):
    """The rule as the station accepts it: no slug, no minted fields."""
    body = {key: copy.deepcopy(rule[key])
            for key in ("label", "field", "values", "season", "showIds") if key in rule}
    return body


def blocklist_wire_entry(entry):
    return {key: copy.deepcopy(entry[key])
            for key in ("type", "id", "name", "artist", "album") if key in entry}


def pending_removals(blocklist, lock):
    """Rules the lock knows but the file no longer has: reported, never deleted."""
    present = {rule_slug(rule) for rule in (blocklist.get("rules") or [])}
    return sorted(slug for slug in (lock.get("rules") or {}) if slug not in present)


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
        clearing = bool(getattr(args, "clear", False))
        slug = args.slug
        if clearing:
            if slug:
                raise ToolError("--clear empties the hours; it takes no show slug")
            target = None
        else:
            if not slug:
                raise ToolError("add-to-grid needs a show slug, or --clear to empty the hours")
            if slug not in _slugs(root, "shows"):
                raise ToolError("no show %r in instance/character/shows; known: %s"
                                % (slug, ", ".join(_slugs(root, "shows")) or "none"))
            target = slug
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
                if current == target:
                    continue
                changes.append((day, hour, current))
                # Emptying a cell never conflicts; filling one over a show does.
                if current and not clearing:
                    conflicts.append((day, hour, current))

        day_label = {v: k for k, v in DAY_NAMES.items()}
        if not changes:
            print("grid      no change: those hours are already %s"
                  % ("empty" if clearing else "owned by %s" % target))
            return EXIT_OK
        for day, hour, current in changes:
            print("cell      %s %02d:00  %s -> %s"
                  % (day_label[day], hour, current or "(empty)",
                     "(empty)" if clearing else target))
        if conflicts and not args.replace:
            sys.stdout.flush()  # show the cells before the refusal on stderr
            raise ToolError("%d cell(s) already hold another show (%s). Re-run with --replace to "
                            "take them over." % (len(conflicts), ", ".join(
                                sorted({c for _, _, c in conflicts}))))

        for day, hour, _ in changes:
            cells = grid.setdefault(str(day), [None] * 24)
            while len(cells) < 24:
                cells.append(None)
            cells[hour] = target
        schedule_doc["schedule"] = grid
        write_json(path, schedule_doc)
        print("grid      %d cell(s) %s across %s"
              % (len(changes), "emptied" if clearing else "changed",
                 ", ".join(sorted({day_label[d] for d, _, _ in changes}))))
        return EXIT_OK

    if action in ("edit-persona", "edit-show"):
        return edit_entity(root, "persona" if action == "edit-persona" else "show", args)

    raise ToolError("unknown character command %r" % action)


# --------------------------------------------------------------------------
# restore: put a backup back, in the order a station can survive
# --------------------------------------------------------------------------

class BackupError(ToolError):
    """The backup cannot be restored as-is."""


def load_backup(directory):
    """Read a backup and prove it is complete before anything is written.

    A backup that cannot restore the programming must not be restored as if it
    could: half a station is worse than none.
    """
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise BackupError("no backup directory at %s" % root)

    def read_json(name):
        path = root / name
        if not path.is_file():
            raise BackupError("%s is missing: this is not a complete backup" % path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BackupError("%s is not valid JSON (%s)" % (path, exc))

    manifest = read_json("manifest.json")
    settings = read_json("settings.json")
    schedule = read_json("schedule.json")

    # manifest against the files actually present
    listed = set(manifest.get("copied") or [])
    present = set()
    for name in ("settings.json", "schedule.json", BLOCKLIST_FILE):
        if (root / name).is_file():
            present.add(name)
    state_dir = root / "state"
    if state_dir.is_dir():
        for path in state_dir.rglob("*"):
            if path.is_file():
                present.add(path.relative_to(state_dir).as_posix())
    missing = sorted(listed - present)
    if missing:
        raise BackupError(
            "the manifest lists %d file(s) that are not in the backup, first: %s. "
            "This backup is incomplete." % (len(missing), missing[0]))

    # the programming must be internally consistent
    shows = schedule.get("shows") or []
    if not shows:
        raise BackupError("schedule.json has no shows: nothing to restore")
    show_ids = {show.get("id") for show in shows}
    grid = schedule.get("schedule") or {}
    if sorted(grid) != [str(day) for day in range(7)]:
        raise BackupError("schedule.json must have days 0..6, found %s" % sorted(grid))
    for day, cells in grid.items():
        if not isinstance(cells, list) or len(cells) != 24:
            raise BackupError("schedule.json day %s must have 24 cells" % day)
        for hour, cell in enumerate(cells):
            if cell and cell not in show_ids:
                raise BackupError(
                    "schedule.json day %s hour %02d names %r, which is not a show in this "
                    "backup: it cannot restore the programming" % (day, hour, cell))

    if not isinstance(settings.get("personas"), list) or not settings.get("personas"):
        raise BackupError("settings.json has no personas")

    blocklist_text = None
    if (root / BLOCKLIST_FILE).is_file():
        try:
            json.loads((root / BLOCKLIST_FILE).read_text(encoding="utf-8"))
        except Exception as exc:
            raise BackupError("%s is not valid JSON (%s)" % (root / BLOCKLIST_FILE, exc))
        blocklist_text = (root / BLOCKLIST_FILE).read_bytes()

    assets = sorted(relative for relative in present
                    if relative not in ("settings.json", "schedule.json", BLOCKLIST_FILE))
    return {
        "root": root,
        "manifest": manifest,
        "settings": settings,
        "schedule": schedule,
        "settings_text": (root / "settings.json").read_bytes(),
        "schedule_text": (root / "schedule.json").read_bytes(),
        "blocklist_text": blocklist_text,
        "assets": assets,
    }


def _read_asset(backup, relative):
    return (backup["root"] / "state" / relative).read_bytes()


def _state_documents(state):
    """Read the two config documents back from wherever we just wrote them."""
    return (json.loads(state.read_bytes("settings.json").decode("utf-8")),
            json.loads(state.read_bytes("schedule.json").decode("utf-8")))


def _state_is_consistent(state):
    """The post-write check. Returns a problem string, or None."""
    try:
        settings, schedule = _state_documents(state)
    except Exception as exc:
        return "the written documents are not valid JSON (%s)" % exc
    shows = schedule.get("shows") or []
    if not shows:
        return "the written schedule has no shows"
    show_ids = {show.get("id") for show in shows}
    for day, cells in (schedule.get("schedule") or {}).items():
        for hour, cell in enumerate(cells):
            if cell and cell not in show_ids:
                return "day %s hour %02d names %r, which is not a show" % (day, hour, cell)
    if not (settings.get("personas") or []):
        return "the written settings have no personas"
    return None


def cmd_restore(config, args):
    tier, detail = detect_tier(config)
    print("tier      %s" % TIER_LABEL[tier], flush=True)
    if tier != 2:
        raise ToolError(
            "a tier-1 restore is impossible: the admin API cannot write schedule.json, so the "
            "programming could not be put back. Add SUBWAVE_HOST and SUBWAVE_STATE_DIR.")

    state = SshStateSource(config)
    backup = load_backup(args.directory)
    print("backup    %s" % backup["root"])
    print("verified  manifest matches %d file(s); %d shows; grid resolves"
          % (len(backup["manifest"].get("copied") or []),
             len(backup["schedule"].get("shows") or [])))

    # The on-air guard blocks the WRITE, not the plan: a dry run writes nothing,
    # so it is still useful on a live station and only warns.
    on_air = None
    if not args.force_now:
        status, now = http_json(config, "/api/now-playing")
        active = (now or {}).get("activeShow") or {}
        if status == 200 and active.get("name"):
            on_air = active.get("name")
    if on_air and args.yes:
        raise ToolError(
            "refusing to restore while the station is on air (%s). Restoring replaces the "
            "config and the assets under a running mixer. Wait for an off-air window, or pass "
            "--force-now to override that guard deliberately." % on_air)
    if on_air:
        print("warning   the station is on air (%s): a real restore would be refused. "
              "Pass --force-now to override that." % on_air)

    targets = [("settings.json", backup["settings_text"]),
               ("schedule.json", backup["schedule_text"])]
    if backup.get("blocklist_text") is not None:
        targets.append((BLOCKLIST_FILE, backup["blocklist_text"]))
    for relative in backup["assets"]:
        targets.append((relative, None))  # read lazily: assets can be large

    print("order     1. state files (settings.json, schedule.json)")
    if backup["assets"]:
        dirs = sorted({relative.split("/")[0] for relative in backup["assets"] if "/" in relative})
        print("          2. assets: %s" % ", ".join(dirs))
    print("          3. then restart the mixer: the controller owns these files and "
          "regenerates the Icecast config from them on restart")

    if not args.yes:
        print("mode      dry run - nothing is written")
        for relative, _ in targets:
            print("would     %s/%s" % (config.state_dir, relative))
        print("result    %d file(s) would be written; 0 written" % len(targets))
        return EXIT_OK

    # snapshot what we are about to overwrite, so a bad restore can be undone
    existing = set(state.list_files())
    snapshot = {}
    for relative, _ in targets:
        if relative in existing:
            snapshot[relative] = state.read_bytes(relative)

    written = []
    try:
        for relative, payload in targets:
            state.write_bytes(relative, payload if payload is not None
                              else _read_asset(backup, relative))
            written.append(relative)
            print("wrote     %s/%s" % (config.state_dir, relative))
    except ToolError as exc:
        _rollback(state, snapshot, written)
        raise ToolError("restore failed part-way (%s); the previous state was put back" % exc)

    problem = _state_is_consistent(state)
    if problem:
        _rollback(state, snapshot, written)
        raise ToolError("the restored station did not validate (%s); the previous state was put "
                        "back. Nothing was left half-written." % problem)

    print("result    %d file(s) restored and verified" % len(targets))
    print("next      restart the mixer, then: subwave.py status")
    return EXIT_OK


def _rollback(state, snapshot, written):
    """Put back every file we replaced, and remove the ones we created."""
    for relative in written:
        if relative in snapshot:
            state.write_bytes(relative, snapshot[relative])
        else:
            try:
                state.remove(relative)
            except Exception:
                pass


# --------------------------------------------------------------------------
# blocklist commands: local file edits, like `character`
# --------------------------------------------------------------------------

def save_repo_blocklist(document, root=None):
    write_json(Path(root or INSTANCE_DIR) / BLOCKLIST_FILE, document)


def _parse_season(from_text, to_text):
    def one(text):
        month, _, day = text.partition("-")
        try:
            return {"month": int(month), "day": int(day)}
        except ValueError:
            raise ToolError("season dates are MM-DD (got %r)" % text)
    if not from_text and not to_text:
        return None
    if not (from_text and to_text):
        raise ToolError("--season-from and --season-to go together")
    return {"from": one(from_text), "to": one(to_text)}


def _rule_from_args(args, existing=None):
    rule = copy.deepcopy(existing) if existing else {}
    if args.label is not None:
        rule["label"] = args.label
    if args.field is not None:
        rule["field"] = args.field
    if args.value:
        rule["values"] = list(args.value)
    if args.season_from or args.season_to:
        rule["season"] = _parse_season(args.season_from, args.season_to)
    if args.show is not None:
        rule["showIds"] = list(args.show)
    rule.setdefault("season", None)
    rule.setdefault("showIds", [])
    return rule


def _check_rule(rule, root, label="rule"):
    errors, warnings = [], []
    document = {"entries": [], "rules": [rule]}
    validate_blocklist(document, set(_slugs(root, "shows")), errors, warnings)
    if errors:
        raise ToolError("%s: %s" % (label, errors[0]))


def cmd_blocklist(config, args):
    """Authoring is local only: this never touches the network."""
    root = INSTANCE_DIR
    if not (root / "character").is_dir() and not (root / BLOCKLIST_FILE).is_file():
        raise ToolError("no instance/ to author (run: subwave.py pull, or character init)")
    document = load_repo_blocklist(root)
    action = args.blocklist_command

    if action == "list":
        for entry in document["entries"]:
            print("entry     %-6s %s  %s" % (entry.get("type"), entry.get("id"),
                                             entry.get("name") or ""))
        if not document["entries"]:
            print("entry     (none)")
        for rule in document["rules"]:
            season = rule.get("season")
            window = ""
            if isinstance(season, dict):
                window = "  season %02d-%02d..%02d-%02d" % (
                    season.get("from", {}).get("month", 0),
                    season.get("from", {}).get("day", 0),
                    season.get("to", {}).get("month", 0),
                    season.get("to", {}).get("day", 0))
            shows = rule.get("showIds") or []
            print("rule      %-24s %-9s %s%s%s" % (
                rule_slug(rule), rule.get("field"),
                ", ".join(rule.get("values") or []), window,
                "  shows: " + ", ".join(shows) if shows else ""))
        if not document["rules"]:
            print("rule      (none)")
        return EXIT_OK

    if action == "block":
        existing = [entry for entry in document["entries"]
                    if entry.get("type") == args.type and entry.get("id") == args.id]
        if existing:
            raise ToolError("%s %s is already blocked" % (args.type, args.id))
        entry = {"type": args.type, "id": args.id}
        for key in ("name", "artist", "album"):
            value = getattr(args, key)
            if value:
                entry[key] = value
        document["entries"].append(entry)
        save_repo_blocklist(document, root)
        print("blocked   %s %s" % (args.type, args.id))
        return EXIT_OK

    if action == "unblock":
        before = len(document["entries"])
        document["entries"] = [entry for entry in document["entries"]
                               if not (entry.get("type") == args.type
                                       and entry.get("id") == args.id)]
        if len(document["entries"]) == before:
            raise ToolError("%s %s is not blocked" % (args.type, args.id))
        save_repo_blocklist(document, root)
        print("unblocked %s %s" % (args.type, args.id))
        print("note      apply does not delete on the station; this is a local removal")
        return EXIT_OK

    if action == "rm":
        before = len(document["rules"])
        document["rules"] = [rule for rule in document["rules"]
                             if rule_slug(rule) != args.slug]
        if len(document["rules"]) == before:
            raise ToolError("no rule %r" % args.slug)
        save_repo_blocklist(document, root)
        print("removed   rule %s (locally)" % args.slug)
        print("note      apply will report it as a pending removal and will not delete it")
        return EXIT_OK

    raise ToolError("unknown blocklist command %r" % action)


def cmd_blocklist_rule(config, args):
    root = INSTANCE_DIR
    document = load_repo_blocklist(root)
    action = args.rule_command

    if action == "add":
        rule = _rule_from_args(args)
        for required in ("label", "field", "values"):
            if not rule.get(required):
                raise ToolError("--%s is required" % ("value" if required == "values" else required))
        lock_rules = load_blocklist_lock(root).get("rules") or {}
        used = {rule_slug(existing) for existing in document["rules"]} | set(lock_rules)
        base = slugify(rule["label"])
        slug = base
        suffix = 2
        while slug in used:
            slug = "%s-%d" % (base, suffix)
            suffix += 1
        rule["slug"] = slug
        _check_rule(rule, root)
        document["rules"].append(rule)
        save_repo_blocklist(document, root)
        print("rule      added %s (%s: %s)" % (slug, rule["field"], ", ".join(rule["values"])))
        return EXIT_OK

    if action == "edit":
        match = next((rule for rule in document["rules"]
                      if rule_slug(rule) == args.slug), None)
        if match is None:
            raise ToolError("no rule %r; known: %s" % (args.slug, ", ".join(
                rule_slug(rule) for rule in document["rules"]) or "none"))
        updated = _rule_from_args(args, existing=match)
        updated["slug"] = args.slug
        _check_rule(updated, root)
        document["rules"][document["rules"].index(match)] = updated
        save_repo_blocklist(document, root)
        print("rule      edited %s" % args.slug)
        return EXIT_OK

    raise ToolError("unknown rule command %r" % action)


# --------------------------------------------------------------------------
# station skills: the DJ's real-world segments
#
# NOT to be confused with skills/ at the repo root, which holds AGENT
# procedures. These are the station's own skills: instance/skills/<slug>/SKILL.md,
# operator content like personas and shows.
#
# tool.mjs is deliberately NOT managed here. The station quarantines new tool
# code as tool.mjs.pending and only runs it after a human reviews it and POSTs
# the sha256 to /dj/skills/:slug/tool/trust. A git apply that installed and
# trusted arbitrary JavaScript would bypass that boundary, so the toolkit stays
# out of it and says so.
# --------------------------------------------------------------------------

STATION_SKILLS_DIR = "skills"
SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
SKILL_COOLDOWN_RE = re.compile(r"^[0-9]+(?:m|h|d)?$")
SKILL_REQUIRED = ("name", "label", "cooldown")


def skills_root(root=None):
    return Path(root or INSTANCE_DIR) / STATION_SKILLS_DIR


def parse_skill_md(text):
    """(frontmatter dict, body). A missing block is an error, not a warning."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ToolError("SKILL.md must open with a --- frontmatter block")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1)
                   if line.strip() == "---")
    except StopIteration:
        raise ToolError("SKILL.md frontmatter is not closed with ---")
    data = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ToolError("SKILL.md frontmatter line is not key: value (%r)" % line)
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[end + 1:]).strip()
    return data, body


def render_skill_md(data, body):
    lines = ["---"]
    for key, value in data.items():
        lines.append("%s: %s" % (key, value))
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines) + "\n"


def load_repo_skills(root=None):
    """{slug: {"data", "body", "text"}} for every skill in instance/skills."""
    base = skills_root(root)
    skills = {}
    if not base.is_dir():
        return skills
    for path in sorted(base.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        try:
            data, body = parse_skill_md(text)
        except ToolError:
            data, body = None, None
        skills[path.parent.name] = {"data": data, "body": body, "text": text}
    return skills


def builtin_skill_slugs(config, state):
    """The slugs the image ships, read from the container.

    Returns None when the container cannot be asked: capturing a built-in into
    the operator's layer is worse than capturing no skills at all, so the caller
    skips rather than guesses.
    """
    try:
        for candidate in ("/app/src/skills/builtins", "/app/dist/skills/builtins"):
            result = ssh_run(config, "docker exec %s sh -c 'ls %s 2>/dev/null'"
                             % (config.container, candidate))
            names = [line.strip() for line in (result.stdout or "").splitlines()
                     if line.strip()]
            if names:
                return set(names)
    except (ToolError, ConfigError):
        return None
    return None


def pull_skills(config, tier):
    """Custom station skills. Built-ins are the image's content, not the operator's."""
    captured, skipped_builtin = {}, []
    if tier == 2:
        state = SshStateSource(config)
        builtins = builtin_skill_slugs(config, state)
        if builtins is None:
            # Without the shipped list we cannot tell a built-in from a custom
            # skill, and capturing the image's own skills as operator content
            # would be wrong. Skip, loudly.
            return {}, []
        for relative in state.list_files():
            parts = relative.split("/")
            if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
                slug = parts[1]
                if slug in builtins:
                    skipped_builtin.append(slug)
                    continue
                captured[slug] = state.read_bytes(relative).decode("utf-8")
    else:
        status, payload = http_json(config, "/api/dj/skills", admin=True)
        if status != 200:
            raise ToolError("GET /api/dj/skills returned HTTP %s" % status)
        for skill in (payload or {}).get("skills") or []:
            slug = skill.get("kind") or skill.get("slug")
            if not slug:
                continue
            if skill.get("seeded"):
                skipped_builtin.append(slug)
                continue
            file_status, file = http_json(config, "/api/dj/skills/%s/file" % slug, admin=True)
            if file_status != 200:
                raise ToolError("GET /api/dj/skills/%s/file returned HTTP %s"
                                % (slug, file_status))
            data = {"name": slug}
            for key in ("label", "cooldown", "cron", "context", "tags", "window", "requiresKey"):
                value = file.get(key)
                if value not in (None, "", []):
                    data[key] = ", ".join(value) if isinstance(value, list) else value
            captured[slug] = render_skill_md(data, file.get("brief") or "")
    return captured, skipped_builtin


def write_repo_skills(captured, root=None):
    base = skills_root(root)
    if base.exists():
        shutil.rmtree(base)
    for slug, text in captured.items():
        path = base / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def validate_skills(root, errors, warnings):
    base = skills_root(root)
    if not base.is_dir():
        return
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        slug = directory.name
        path = directory / "SKILL.md"
        if not path.is_file():
            errors.append("skills/%s: no SKILL.md" % slug)
            continue
        if not SKILL_SLUG_RE.match(slug):
            errors.append("skills/%s: directory name is not a valid skill slug "
                          "(^[a-z0-9][a-z0-9-]{0,48}$)" % slug)
        try:
            data, body = parse_skill_md(path.read_text(encoding="utf-8"))
        except ToolError as exc:
            errors.append("skills/%s/SKILL.md: %s" % (slug, exc))
            continue
        for field in SKILL_REQUIRED:
            if not data.get(field):
                errors.append("skills/%s/SKILL.md: missing frontmatter field %r" % (slug, field))
        if data.get("name") and data["name"] != slug:
            errors.append("skills/%s/SKILL.md: name %r does not match the directory"
                          % (slug, data["name"]))
        cooldown = data.get("cooldown")
        if cooldown and not SKILL_COOLDOWN_RE.match(cooldown):
            errors.append("skills/%s/SKILL.md: cooldown %r must look like \"45m\", \"6h\", "
                          "\"2d\", or a bare number of minutes" % (slug, cooldown))
        if not body.strip():
            errors.append("skills/%s/SKILL.md: the prompt body is empty" % slug)
        if (directory / "tool.mjs").exists():
            warnings.append("skills/%s/tool.mjs is not managed by this toolkit: the station "
                            "quarantines tool code behind its pending/trust review" % slug)


SKILL_WIRE_FIELDS = ("label", "cooldown", "cron", "context", "tags", "window", "requiresKey")


def skill_wire_body(slug, entry, for_create=False):
    """Only the fields the station's schema knows. Unknown frontmatter (a tool's
    own knobs) stays in the repo and is not sent."""
    data = entry.get("data") or {}
    body = {key: data[key] for key in SKILL_WIRE_FIELDS if key in data}
    body["brief"] = entry.get("body") or ""
    if for_create:
        body["name"] = slug
    return body


def apply_skills(config, repo_skills):
    """Create with POST, update with PUT. Never deletes: that is prune's job, and
    prune does not cover skills yet."""
    status, payload = http_json(config, "/api/dj/skills", admin=True)
    if status != 200:
        raise ToolError("GET /api/dj/skills returned HTTP %s" % status)
    existing = {skill.get("kind") or skill.get("slug"): skill
                for skill in (payload or {}).get("skills") or []}

    for slug, entry in sorted(repo_skills.items()):
        current = existing.get(slug)
        if current and current.get("seeded"):
            print("skipped   skill %r is built-in; the toolkit does not write those" % slug)
            continue
        if current:
            code, response = http_json(config, "/api/dj/skills/%s/file" % slug, admin=True,
                                       method="PUT", body=skill_wire_body(slug, entry))
            verb = "updated"
        else:
            code, response = http_json(config, "/api/dj/skills", admin=True, method="POST",
                                       body=skill_wire_body(slug, entry, for_create=True))
            verb = "created"
        if code >= 400:
            detail = ""
            if isinstance(response, dict):
                detail = response.get("error") or ""
            raise ToolError("%s skill %r failed: HTTP %s %s" % (verb, slug, code, detail))
        print("skill     %s %s" % (verb, slug))


def cmd_skills(config, args):
    """Local file edits, like `character` and `blocklist`."""
    root = INSTANCE_DIR
    base = skills_root(root)
    action = args.skills_command
    skills = load_repo_skills(root)

    if action == "list":
        if not skills:
            print("skill     (none captured)")
        for slug, entry in sorted(skills.items()):
            data = entry.get("data") or {}
            print("skill     %-24s %-16s %s" % (slug, data.get("cooldown") or "-",
                                               data.get("label") or ""))
        return EXIT_OK

    if action == "show":
        entry = skills.get(args.slug)
        if entry is None:
            raise ToolError("no skill %r; known: %s" % (args.slug, ", ".join(sorted(skills))
                                                        or "none"))
        print(entry["text"])
        return EXIT_OK

    if action == "new":
        slug = args.slug
        if not SKILL_SLUG_RE.match(slug):
            raise ToolError("%r is not a valid skill slug (^[a-z0-9][a-z0-9-]{0,48}$)" % slug)
        if (base / slug).exists():
            raise ToolError("skill %r already exists" % slug)
        data = {"name": slug, "label": title_from_slug(slug), "cooldown": "60m"}
        body = ("TODO: what this segment does, when it should fire, and how the DJ should "
                "sound doing it.")
        path = base / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_skill_md(data, body), encoding="utf-8")
        print("skill     created %s" % slug)
        print("next      fill in the prompt, then: subwave.py validate")
        return EXIT_OK

    if action == "edit":
        entry = skills.get(args.slug)
        if entry is None:
            raise ToolError("no skill %r; known: %s" % (args.slug, ", ".join(sorted(skills))
                                                        or "none"))
        data = dict(entry.get("data") or {})
        body = entry.get("body") or ""
        if args.label is not None:
            data["label"] = args.label
        if args.cooldown is not None:
            data["cooldown"] = args.cooldown
        if args.brief is not None:
            body = args.brief
        path = base / args.slug / "SKILL.md"
        path.write_text(render_skill_md(data, body), encoding="utf-8")
        print("skill     edited %s" % args.slug)
        return EXIT_OK

    if action == "rm":
        path = base / args.slug
        if not path.is_dir():
            raise ToolError("no skill %r" % args.slug)
        shutil.rmtree(path)
        print("skill     removed %s (locally)" % args.slug)
        print("note      apply does not delete station skills; prune does not cover them yet")
        return EXIT_OK

    raise ToolError("unknown skills command %r" % action)


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

    skills_cmd = sub.add_parser("skills", help="author station skills locally (no network)")
    skills_sub = skills_cmd.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list", help="captured station skills")
    skills_list.set_defaults(func=cmd_skills)
    skills_new = skills_sub.add_parser("new", help="scaffold a skill")
    skills_new.add_argument("slug")
    skills_new.set_defaults(func=cmd_skills)
    skills_show = skills_sub.add_parser("show", help="print a skill's SKILL.md")
    skills_show.add_argument("slug")
    skills_show.set_defaults(func=cmd_skills)
    skills_edit = skills_sub.add_parser("edit", help="change a skill's fields")
    skills_edit.add_argument("slug")
    skills_edit.add_argument("--label")
    skills_edit.add_argument("--cooldown")
    skills_edit.add_argument("--brief")
    skills_edit.set_defaults(func=cmd_skills)
    skills_rm = skills_sub.add_parser("rm", help="remove a skill locally")
    skills_rm.add_argument("slug")
    skills_rm.set_defaults(func=cmd_skills)

    blocklist = sub.add_parser("blocklist", help="author the blocklist locally (no network)")
    bl_sub = blocklist.add_subparsers(dest="blocklist_command", required=True)

    bl_list = bl_sub.add_parser("list", help="entries and rules")
    bl_list.set_defaults(func=cmd_blocklist)

    bl_block = bl_sub.add_parser("block", help="block a track, album or artist")
    bl_block.add_argument("--type", required=True, choices=("artist", "album", "track"))
    bl_block.add_argument("--id", required=True, help="the Navidrome id")
    bl_block.add_argument("--name")
    bl_block.add_argument("--artist")
    bl_block.add_argument("--album")
    bl_block.set_defaults(func=cmd_blocklist)

    bl_unblock = bl_sub.add_parser("unblock", help="remove a block locally")
    bl_unblock.add_argument("--type", required=True, choices=("artist", "album", "track"))
    bl_unblock.add_argument("--id", required=True)
    bl_unblock.set_defaults(func=cmd_blocklist)

    bl_rm = bl_sub.add_parser("rm", help="remove a rule locally")
    bl_rm.add_argument("slug")
    bl_rm.set_defaults(func=cmd_blocklist)

    bl_rule = bl_sub.add_parser("rule", help="add or edit a rule")
    rule_sub = bl_rule.add_subparsers(dest="rule_command", required=True)
    for name, help_text in (("add", "add a rule"), ("edit", "edit a rule by slug")):
        # NB: do not call this `parser` - build_parser returns the name `parser`,
        # and rebinding it made --help print this leaf and broke the whole CLI.
        rule_parser = rule_sub.add_parser(name, help=help_text)
        if name == "edit":
            rule_parser.add_argument("slug")
        rule_parser.add_argument("--label")
        rule_parser.add_argument("--field", choices=BLOCKLIST_RULE_FIELDS)
        rule_parser.add_argument("--value", action="append", default=[],
                                 help="one value; repeat up to 12")
        rule_parser.add_argument("--season-from",
                                 help="MM-DD; while in season the rule does NOT block")
        rule_parser.add_argument("--season-to", help="MM-DD; from > to wraps the year end")
        rule_parser.add_argument("--show", action="append", default=None,
                                 help="show slug; omit for station-wide")
        rule_parser.set_defaults(func=cmd_blocklist_rule)

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
    char_grid.add_argument("slug", nargs="?")
    char_grid.add_argument("--days", required=True, help="comma separated: mon,tue")
    char_grid.add_argument("--from", dest="from_hour", type=int, required=True, help="first hour, 0-24")
    char_grid.add_argument("--to", dest="to_hour", type=int, required=True, help="hour after the last")
    char_grid.add_argument("--replace", action="store_true",
                           help="take over cells that hold another show")
    char_grid.add_argument("--clear", action="store_true",
                           help="empty those hours instead: the station runs autonomously then")
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
    apply_cmd.add_argument("--prune", action="store_true",
                           help="also delete entities that exist on the station but not in the repo")
    apply_cmd.add_argument("--force-prune", action="store_true",
                           help="delete even when a grid cell or a hosted show depends on it")
    apply_cmd.set_defaults(func=cmd_apply)

    restore = sub.add_parser("restore", help="put a backup back (dry run by default)")
    restore.add_argument("directory", help="a directory written by backup")
    restore.add_argument("--yes", action="store_true", help="perform the restore")
    restore.add_argument("--force-now", action="store_true",
                         help="restore even while the station is on air")
    restore.set_defaults(func=cmd_restore)

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
