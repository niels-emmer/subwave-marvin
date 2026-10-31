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
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTAINER = "sub-wave-controller"
DEFAULT_TIMEOUT = 20

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


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

    # -- requirements ------------------------------------------------------
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

    def redacted(self):
        """A copy safe to print: no credential values."""
        shown = {}
        for key, value in self.values.items():
            if key in ("SUBWAVE_ADMIN_PASS", "SUBWAVE_STATION_PASSWORD"):
                shown[key] = "set" if value else "unset"
            else:
                shown[key] = value
        return shown


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def http_json(config, path, admin=False, station=False, timeout=DEFAULT_TIMEOUT):
    """GET a JSON endpoint. Returns (status_code, payload)."""
    config.require_http()
    url = config.base_url + path
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if admin:
        if not (config.admin_user and config.admin_pass):
            raise ConfigError(
                "this endpoint needs SUBWAVE_ADMIN_USER and SUBWAVE_ADMIN_PASS"
            )
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
        return exc.code, {"error": detail or exc.reason}
    except urllib.error.URLError as exc:
        raise ToolError("cannot reach %s (%s)" % (url, exc.reason))
    except json.JSONDecodeError as exc:
        raise ToolError("%s did not return JSON (%s)" % (url, exc))


def ssh_run(config, remote_command, timeout=DEFAULT_TIMEOUT):
    """Run a command on the host. Returns CompletedProcess."""
    if not config.host:
        raise ConfigError("SUBWAVE_HOST is not set")
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        config.host,
        remote_command,
    ]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ToolError("ssh is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ToolError("ssh to %s timed out after %ss" % (config.host, timeout))


def read_state_file(config, filename):
    """Read a file from the controller container's state dir (tier 2)."""
    path = "%s/%s" % (config.state_dir, filename)
    # The path is single-quoted so the REMOTE shell does not expand anything,
    # while the local shell never sees it: the whole string is one argv element.
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

def detect_tier(config):
    """Return (tier, detail). Never assume: tier 2 is probed, not inferred."""
    if not config.has_tier2_settings():
        return 1, "missing " + ", ".join(config.missing_tier2())
    path = "%s/settings.json" % config.state_dir
    result = ssh_run(config, "docker exec %s test -r '%s'" % (config.container, path))
    if result.returncode == 0:
        return 2, "ssh %s, docker exec %s" % (config.host, config.container)
    detail = (result.stderr or "").strip().splitlines()
    return 1, "host probe failed: " + (detail[-1] if detail else "exit %d" % result.returncode)


TIER_LABEL = {
    1: "1 - HTTP only (no host access)",
    2: "2 - host access (ssh + docker exec)",
}


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

    # Full-fidelity sources first, so each fact is printed once.
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

    print("tier      %s" % TIER_LABEL[tier])
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

    print("writes    none: this build is read-only")
    return EXIT_ERROR if api_failed else EXIT_OK


def not_implemented(name):
    def handler(config, args):
        print("%s: not implemented in this build" % name, file=sys.stderr)
        return EXIT_ERROR
    return handler


def build_parser():
    parser = argparse.ArgumentParser(
        prog="subwave.py",
        description="Read-only operations against a SUB/WAVE station.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="tier, station, on-air state")
    status.set_defaults(func=cmd_status)

    pull = sub.add_parser("pull", help="live state into the repo")
    pull.set_defaults(func=not_implemented("pull"))

    diff = sub.add_parser("diff", help="read-only drift report")
    diff.set_defaults(func=not_implemented("diff"))

    backup = sub.add_parser("backup", help="back up the station")
    backup.set_defaults(func=not_implemented("backup"))

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
