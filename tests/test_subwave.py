"""Tests for scripts/subwave.py. No network, no ssh, no instance/ access.

Fixtures come from examples/demo (the fictional station), never from instance/.
"""

import importlib.util
import json
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo" / "character"


def load_tool():
    spec = importlib.util.spec_from_file_location("subwave", ROOT / "scripts" / "subwave.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


subwave = load_tool()


# -- fixtures ---------------------------------------------------------------

def demo_documents():
    """A settings document and a schedule document built from the demo station."""
    character = json.loads((DEMO / "character.json").read_text())
    personas = [json.loads(p.read_text())
                for p in sorted((DEMO / "personas").glob("*.json"))]
    show_files = {p.stem: json.loads(p.read_text())
                  for p in sorted((DEMO / "shows").glob("*.json"))}
    shows = list(show_files.values())
    # The demo grid holds slugs (the repo shape). A "live" document holds ids,
    # so translate here: this is the server side of the same station.
    grid = {day: [show_files[cell]["id"] if cell in show_files else cell for cell in cells]
            for day, cells in
            json.loads((DEMO / "schedule.json").read_text())["schedule"].items()}

    settings = {
        "station": character["name"],
        "stationDescription": character["description"],
        "locale": character["locale"],
        "timezone": character["timezone"],
        "theme": character["theme"],
        "djHouseRules": character["houseRules"],
        "personas": personas,
        "activePersonaId": personas[0]["id"],
        "privacy": {"password": "not-a-real-password", "listenerAuth": False},  # nosecret: fixture value, never a real credential
        "llm": {
            "apiKey": "fake-llm-key",
            "keys": {"primary": "fake-key-one", "backup": "fake-key-two"},
            "fallback": {"apiKey": "fake-fallback-key"},
            "model": "some-model",
        },
        "tts": {"cloud": {"apiKey": "fake-tts-key", "compatApiKey": "fake-compat-key"}},
        "search": {"apiKey": "fake-search-key"},
        "embedding": {"apiKey": "fake-embedding-key"},
        "scrobble": {
            "lastfm": {"apiKey": "fake-lastfm-key", "apiSecret": "fake-lastfm-secret",
                       "sessionKey": "fake-lastfm-session"},
            "listenbrainz": {"userToken": "fake-listenbrainz-token"},
        },
        "stream": {"maxListeners": 8, "bitrate": 192},
    }
    schedule = {"shows": shows, "schedule": grid, "override": None}
    return settings, schedule


# -- redaction --------------------------------------------------------------

class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.settings, _ = demo_documents()
        self.redacted, self.count = subwave.redact(self.settings)

    def test_every_configured_path_becomes_the_sentinel(self):
        expected = [
            ("privacy", "password"), ("llm", "apiKey"), ("llm", "keys", "primary"),
            ("llm", "keys", "backup"), ("llm", "fallback", "apiKey"),
            ("tts", "cloud", "apiKey"), ("tts", "cloud", "compatApiKey"),
            ("search", "apiKey"), ("embedding", "apiKey"),
            ("scrobble", "lastfm", "apiKey"), ("scrobble", "lastfm", "apiSecret"),
            ("scrobble", "lastfm", "sessionKey"),
            ("scrobble", "listenbrainz", "userToken"),
        ]
        for path in expected:
            node = self.redacted
            for key in path:
                node = node[key]
            self.assertEqual(node, subwave.SENTINEL, "path %s not redacted" % (path,))

    def test_redaction_count_is_every_value_it_touched(self):
        # 11 named paths plus two llm.keys children.
        self.assertEqual(self.count, 13)

    def test_non_secret_values_are_untouched(self):
        self.assertEqual(self.redacted["stream"]["maxListeners"], 8)
        self.assertEqual(self.redacted["llm"]["model"], "some-model")
        self.assertEqual(self.redacted["station"], self.settings["station"])
        self.assertFalse(self.redacted["privacy"]["listenerAuth"])

    def test_sentinel_only_appears_where_a_secret_was(self):
        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield from walk(value, "%s.%s" % (path, key) if path else key)
            elif node == subwave.SENTINEL:
                yield path

        marked = set(walk(self.redacted))
        originals = {}

        def collect(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    collect(value, "%s.%s" % (path, key) if path else key)
            else:
                originals[path] = node

        collect(self.settings)
        for path in marked:
            self.assertNotEqual(originals.get(path), subwave.SENTINEL,
                                "sentinel replaced a value that was not a secret: %s" % path)

    def test_original_document_is_not_mutated(self):
        self.assertEqual(self.settings["privacy"]["password"], "not-a-real-password")


# -- slugs and the lockfile -------------------------------------------------

class SlugTests(unittest.TestCase):
    def test_basic_slug(self):
        self.assertEqual(subwave.slugify("Nell Harrow"), "nell-harrow")
        self.assertEqual(subwave.slugify("  The  Late   Show! "), "the-late-show")

    def test_collisions_get_numeric_suffixes(self):
        items = [{"id": "p_1", "name": "Same Name"}, {"id": "p_2", "name": "Same Name"},
                 {"id": "p_3", "name": "Same Name"}]
        self.assertEqual(subwave.assign_slugs(items, {}),
                         {"same-name": "p_1", "same-name-2": "p_2", "same-name-3": "p_3"})

    def test_a_rename_keeps_the_old_slug_via_the_lockfile(self):
        previous = {"old-host": "p_1"}
        items = [{"id": "p_1", "name": "Completely New Name"}]
        self.assertEqual(subwave.assign_slugs(items, previous), {"old-host": "p_1"})

    def test_known_ids_do_not_consume_new_slugs(self):
        previous = {"keeper": "p_1"}
        items = [{"id": "p_1", "name": "Keeper"}, {"id": "p_2", "name": "Keeper"}]
        self.assertEqual(subwave.assign_slugs(items, previous),
                         {"keeper": "p_1", "keeper-2": "p_2"})


# -- split and round trip ---------------------------------------------------

class SplitTests(unittest.TestCase):
    def setUp(self):
        self.settings, self.schedule = demo_documents()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = subwave.INSTANCE_DIR
        subwave.INSTANCE_DIR = self.tmp
        self.addCleanup(lambda: setattr(subwave, "INSTANCE_DIR", self._saved))

    def test_round_trip_preserves_ids_and_content(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        repo = subwave.load_instance_model(self.tmp)

        self.assertEqual(sorted(repo["personas"]), sorted(live["personas"]))
        self.assertEqual(sorted(repo["shows"]), sorted(live["shows"]))
        self.assertEqual(repo["personas"], live["personas"])
        self.assertEqual(repo["shows"], live["shows"])
        self.assertEqual(repo["grid"], live["grid"])
        self.assertEqual(repo["character"], live["character"])
        self.assertEqual(repo["settings"], live["settings"])

    def test_diff_is_clean_straight_after_a_pull(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        repo = subwave.load_instance_model(self.tmp)
        self.assertEqual(subwave.drift_count(subwave.compare_models(repo, live)), 0)

    def test_character_layer_files_exist(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        self.assertTrue((self.tmp / "settings.json").is_file())
        self.assertTrue((self.tmp / "character" / "character.json").is_file())
        self.assertTrue((self.tmp / "character" / "schedule.json").is_file())
        self.assertTrue((self.tmp / "ids.lock.json").is_file())

    def test_settings_file_excludes_personas_and_active_persona(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        written = json.loads((self.tmp / "settings.json").read_text())
        self.assertNotIn("personas", written)
        self.assertNotIn("activePersonaId", written)
        self.assertIn("stream", written)

    def test_lockfile_records_both_directions(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        lock = json.loads((self.tmp / "ids.lock.json").read_text())
        for slug, id_ in lock["shows"].items():
            self.assertEqual(lock["ids"][id_], slug)
        self.assertEqual(lock["activePersonaId"], self.settings["activePersonaId"])

    def test_secrets_are_redacted_on_disk(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        blob = (self.tmp / "settings.json").read_text()
        for secret in ("not-a-real-password", "fake-llm-key", "fake-lastfm-secret"):
            self.assertNotIn(secret, blob)


# -- grid -------------------------------------------------------------------

class GridTests(unittest.TestCase):
    def setUp(self):
        self.settings, self.schedule = demo_documents()

    def test_all_168_cells_survive_the_split(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        cells = [cell for day in live["grid"].values() for cell in day]
        self.assertEqual(len(cells), 168)
        self.assertTrue(all(cells))

    def test_every_cell_resolves_to_a_known_show(self):
        live = subwave.build_live_model(self.settings, self.schedule)
        known = set(live["shows"])
        for day, cells in live["grid"].items():
            for cell in cells:
                self.assertIn(cell, known, "day %s has an unresolvable cell" % day)

    def test_grid_is_written_as_slugs(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = subwave.INSTANCE_DIR
        subwave.INSTANCE_DIR = tmp
        self.addCleanup(lambda: setattr(subwave, "INSTANCE_DIR", saved))
        live = subwave.build_live_model(self.settings, self.schedule)
        subwave.write_instance(live, {})
        written = json.loads((tmp / "character" / "schedule.json").read_text())
        values = {cell for cells in written["schedule"].values() for cell in cells}
        self.assertTrue(all("_" not in value for value in values),
                        "grid should hold slugs, not server ids: %s" % values)


# -- tier detection ---------------------------------------------------------

class TierTests(unittest.TestCase):
    def config(self, **env):
        return subwave.Config(dotenv={}, environ=env)

    def test_no_host_settings_is_tier_1(self):
        tier, detail = subwave.detect_tier(self.config())
        self.assertEqual(tier, 1)
        self.assertIn("SUBWAVE_HOST", detail)
        self.assertIn("SUBWAVE_STATE_DIR", detail)

    def test_host_without_state_dir_is_tier_1(self):
        tier, _ = subwave.detect_tier(self.config(SUBWAVE_HOST="somehost"))
        self.assertEqual(tier, 1)

    def test_state_dir_without_host_is_tier_1(self):
        tier, _ = subwave.detect_tier(self.config(SUBWAVE_STATE_DIR="/var/sub-wave"))
        self.assertEqual(tier, 1)

    def test_both_set_but_probe_fails_is_tier_1_with_the_reason(self):
        config = self.config(SUBWAVE_HOST="somehost", SUBWAVE_STATE_DIR="/var/sub-wave")
        tier, detail = subwave.detect_tier(config, probe=lambda: (False, "host probe failed: nope"))
        self.assertEqual(tier, 1)
        self.assertIn("nope", detail)

    def test_both_set_and_probe_ok_is_tier_2(self):
        config = self.config(SUBWAVE_HOST="somehost", SUBWAVE_STATE_DIR="/var/sub-wave")
        tier, _ = subwave.detect_tier(config, probe=lambda: (True, "ok"))
        self.assertEqual(tier, 2)

    def test_environment_beats_dotenv(self):
        config = subwave.Config(dotenv={"SUBWAVE_BASE_URL": "http://from-file"},
                                environ={"SUBWAVE_BASE_URL": "http://from-env"})
        self.assertEqual(config.base_url, "http://from-env")

    def test_container_defaults(self):
        self.assertEqual(self.config().container, "sub-wave-controller")
        self.assertEqual(self.config(SUBWAVE_CONTAINER="custom").container, "custom")


class SshQuotingTests(unittest.TestCase):
    """A path reaches a REMOTE shell, so it must be quoted as one word.

    Skill directory names and backup-supplied filenames end up inside a
    `docker exec` command string. Before this, a single quote in a name closed
    the quoting and the rest of the name ran as shell code on the host.
    """

    NASTY = "skills/o'brien; touch pwned; 'x/SKILL.md"

    def setUp(self):
        self.config = subwave.Config(dotenv={}, environ={
            "SUBWAVE_HOST": "station.example",
            "SUBWAVE_STATE_DIR": "/var/sub-wave",
            "SUBWAVE_CONTAINER": "sub-wave-controller",
        })
        self.commands = []
        self._saved = subwave.ssh_run
        self.addCleanup(lambda: setattr(subwave, "ssh_run", self._saved))

        def fake(config, command, **kwargs):
            self.commands.append(command)
            if kwargs.get("binary"):
                return SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        subwave.ssh_run = fake

    def test_a_state_file_read_quotes_the_path(self):
        subwave.read_state_file(self.config, "settings.json")
        self.assertEqual(self.commands, [
            "docker exec sub-wave-controller cat %s"
            % shlex.quote("/var/sub-wave/settings.json")])

    def test_a_path_with_a_quote_semicolon_and_space_is_one_argument(self):
        subwave.SshStateSource(self.config).read_bytes(self.NASTY)
        expected = "/var/sub-wave/" + self.NASTY
        self.assertEqual(
            self.commands,
            ["docker exec sub-wave-controller cat %s" % shlex.quote(expected)])
        # What the remote shell would actually see: the path, and no extra word.
        self.assertEqual(shlex.split(self.commands[0]),
                         ["docker", "exec", "sub-wave-controller", "cat", expected])

    def test_a_write_passes_the_path_as_a_positional_argument(self):
        subwave.SshStateSource(self.config).write_bytes(self.NASTY, b"payload")
        path = "/var/sub-wave/" + self.NASTY
        parent = path.rsplit("/", 1)[0]
        self.assertEqual(self.commands[0],
                         "docker exec sub-wave-controller mkdir -p %s" % shlex.quote(parent))
        self.assertEqual(
            self.commands[1],
            "docker exec -i sub-wave-controller sh -c 'cat > \"$1\"' _ %s"
            % shlex.quote(path))
        self.assertEqual(shlex.split(self.commands[1])[-1], path)

    def test_a_remove_and_a_listing_quote_the_path(self):
        source = subwave.SshStateSource(self.config)
        source.remove(self.NASTY)
        source.list_files()
        self.assertEqual(self.commands[0],
                         "docker exec sub-wave-controller rm -f %s"
                         % shlex.quote("/var/sub-wave/" + self.NASTY))
        self.assertEqual(
            self.commands[1],
            "docker exec sub-wave-controller sh -c 'cd \"$1\" && find . -type f' _ %s"
            % shlex.quote("/var/sub-wave"))


class EnvExampleTests(unittest.TestCase):
    """`.env.example` is how an operator learns which variables exist.

    A key the code reads but the example omits is undiscoverable: you cannot set
    what you are never told about. Nothing else enforced this parity, and it had
    already drifted — SUBWAVE_CONTAINER was in Config.KEYS and used by every
    tier-2 `docker exec` while being absent from the file people copy.
    """

    def documented(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        return {
            line.split("=", 1)[0].strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        }

    def test_every_config_key_is_documented(self):
        missing = set(subwave.Config.KEYS) - self.documented()
        self.assertEqual(missing, set(),
                         "Config.KEYS read but not documented in .env.example: %s"
                         % ", ".join(sorted(missing)))

    def test_no_documented_key_is_unknown(self):
        unknown = self.documented() - set(subwave.Config.KEYS)
        self.assertEqual(unknown, set(),
                         ".env.example documents keys the loader never reads: %s"
                         % ", ".join(sorted(unknown)))


if __name__ == "__main__":
    unittest.main()
