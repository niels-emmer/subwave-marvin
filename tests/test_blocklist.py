"""The blocklist is the third config document, and the station's own backup drops it.

`backup/zip.ts` INCLUDE_FILES lists jingles.json, jingles.m3u and sfx.json — not
blocklist.json — so a stock export/restore silently loses every ban. Ours is the
one meant to be complete.

No authoring here: capture, validate, diff, backup, restore only.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_station
from test_subwave import load_tool

subwave = load_tool()
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "station.json"


class BlocklistTestCase(unittest.TestCase):
    def setUp(self):
        self.server = mock_station.start_mock()
        self.addCleanup(self.server.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        for key in ("SUBWAVE_HOST", "SUBWAVE_STATE_DIR"):
            os.environ.pop(key, None)
        os.environ.update({
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
        })

        self._saved = (subwave.INSTANCE_DIR, subwave.SshStateSource,
                       subwave.read_state_file, subwave.detect_tier,
                       subwave.builtin_skill_slugs)
        subwave.INSTANCE_DIR = self.tmp / "instance"
        subwave.SshStateSource = lambda config: subwave.LocalStateSource(
            self.server.state_dir)
        subwave.read_state_file = lambda config, name: json.dumps(
            self.server.station.settings if name == "settings.json"
            else self.server.station.schedule)
        subwave.detect_tier = lambda config, probe=None: (2, "mock")
        subwave.builtin_skill_slugs = lambda config, state: {"curiosity"}
        self.addCleanup(self._restore)

        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        model = subwave.build_live_model(document["settings"], document["schedule"])
        subwave.write_instance(model, {}, capture={
            "tier": 2, "at": "2026-10-31T00:00:00Z", "lossy": False, "lost": []})

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def _restore(self):
        (subwave.INSTANCE_DIR, subwave.SshStateSource,
         subwave.read_state_file, subwave.detect_tier,
         subwave.builtin_skill_slugs) = self._saved

    # -- helpers -----------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = subwave.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def blocklist_path(self):
        return subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE

    def repo_blocklist(self):
        return json.loads(self.blocklist_path().read_text())

    def write_blocklist(self, document):
        self.blocklist_path().write_text(json.dumps(document, indent=2))

    def set_station_blocklist(self, document):
        """The station's own document: memory and state file, as tier 2 reads it."""
        self.server.station.blocklist = (json.loads(json.dumps(document))
                                         if document is not None
                                         else {"entries": [], "rules": []})
        path = Path(self.server.state_dir) / subwave.BLOCKLIST_FILE
        if document is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(document, indent=2))


class PullTests(BlocklistTestCase):
    def test_entries_and_rules_pull_intact(self):
        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        document = self.repo_blocklist()

        self.assertEqual(len(document["entries"]), 1)
        self.assertEqual(document["entries"][0]["id"], "subsonic-1")
        self.assertEqual(document["entries"][0]["type"], "track")
        self.assertEqual(len(document["rules"]), 1)
        rule = document["rules"][0]
        self.assertEqual(rule["label"], "No christmas music")
        self.assertEqual(rule["field"], "genre")
        self.assertEqual(rule["values"], ["Christmas"])
        self.assertIn("id", rule, "the store-minted rule id must survive")

    def test_derived_fields_are_not_captured(self):
        self.run_cli("pull", "--force")
        document = self.repo_blocklist()
        for item in document["entries"] + document["rules"]:
            self.assertNotIn("active", item, "live fields must not be captured")
            self.assertNotIn("matchCount", item)

    def test_an_absent_blocklist_pulls_as_an_empty_document(self):
        self.set_station_blocklist(None)
        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.repo_blocklist(), {"entries": [], "rules": []})

        capture = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())["capture"]
        self.assertFalse(capture["lossy"], "a missing blocklist is not a lossy capture")

    def test_show_ids_are_captured_as_slugs(self):
        show_id = self.server.station.schedule["shows"][0]["id"]
        document = self.server.station.blocklist
        document["rules"][0]["showIds"] = [show_id]
        self.set_station_blocklist(document)
        self.run_cli("pull", "--force")
        rule = self.repo_blocklist()["rules"][0]
        self.assertNotIn(show_id, rule["showIds"], "show ids become slugs")
        self.assertTrue(rule["showIds"])


class CredentialTests(BlocklistTestCase):
    """Tier 2 must not acquire a second credential.

    Tier 2 exists because you have host access; a user who can read the state
    files as root should not also need the admin password to pull.
    """

    def test_tier_2_pull_needs_no_admin_credentials(self):
        os.environ.pop("SUBWAVE_ADMIN_USER", None)
        os.environ.pop("SUBWAVE_ADMIN_PASS", None)

        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.repo_blocklist()["entries"]), 1)
        self.assertEqual(len(self.repo_blocklist()["rules"]), 1)

        api_calls = [entry for entry in self.server.station.requests
                     if entry["path"] == "/api/library/blocklist"]
        self.assertEqual(api_calls, [], "tier 2 reads the state file, not the API")

    def test_tier_1_pull_reads_the_blocklist_over_the_api(self):
        subwave.detect_tier = lambda config, probe=None: (1, "mock")

        code, output = self.run_cli("pull", "--allow-lossy", "--force")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.repo_blocklist()["rules"]), 1)
        self.assertIn("No christmas music", json.dumps(self.repo_blocklist()))

        api_calls = [entry for entry in self.server.station.requests
                     if entry["path"] == "/api/library/blocklist"]
        self.assertTrue(api_calls, "tier 1 has no filesystem, so it uses the API")


class ValidateTests(BlocklistTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli("pull", "--force")

    def test_a_valid_document_passes(self):
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)

    def test_a_bad_field_value_is_rejected(self):
        document = self.repo_blocklist()
        document["rules"][0]["field"] = "bpm"
        self.write_blocklist(document)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("field", output)
        self.assertIn("genre", output, "the message must name the allowed set")

    def test_too_many_values_are_rejected(self):
        document = self.repo_blocklist()
        document["rules"][0]["values"] = ["v%d" % index for index in range(13)]
        self.write_blocklist(document)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("1..12", output)

    def test_an_out_of_range_season_is_rejected(self):
        document = self.repo_blocklist()
        document["rules"][0]["season"] = {"from": {"month": 13, "day": 1},
                                          "to": {"month": 2, "day": 1}}
        self.write_blocklist(document)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("month", output)
        self.assertIn("1..12", output)

    def test_a_bad_entry_type_is_rejected(self):
        document = self.repo_blocklist()
        document["entries"][0]["type"] = "podcast"
        self.write_blocklist(document)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("track", output)

    def test_a_server_id_in_show_ids_is_a_warning_not_an_error(self):
        document = self.repo_blocklist()
        document["rules"][0]["showIds"] = ["s_demo_first"]
        self.write_blocklist(document)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("warning", output)
        self.assertIn("server id", output)


class DiffTests(BlocklistTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli("pull", "--force")

    def test_a_changed_rule_is_reported(self):
        document = self.repo_blocklist()
        document["rules"][0]["label"] = "No christmas music at all"
        self.write_blocklist(document)

        code, output = self.run_cli("diff")
        self.assertEqual(code, 1, "drift must exit 1")
        self.assertIn("changed", output)
        self.assertIn("No christmas music at all", output)

    def test_an_unchanged_blocklist_is_clean(self):
        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)


class BackupRestoreTests(BlocklistTestCase):
    def test_backup_carries_the_blocklist_and_says_why(self):
        destination, manifest = subwave.take_backup(self.config(), self.tmp / "backups")
        self.assertIn(subwave.BLOCKLIST_FILE, manifest["copied"])
        self.assertTrue((destination / subwave.BLOCKLIST_FILE).is_file())

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            subwave.print_manifest(destination, manifest)
        output = buffer.getvalue()
        self.assertIn("blocklist.json", output)
        self.assertIn("omits blocklist.json", output,
                      "the manifest must say why our backup exists")

    def test_restore_puts_the_blocklist_back(self):
        destination, _ = subwave.take_backup(self.config(), self.tmp / "backups")
        original = (Path(self.server.state_dir) / subwave.BLOCKLIST_FILE).read_bytes()
        (Path(self.server.state_dir) / subwave.BLOCKLIST_FILE).unlink()

        args = type("A", (), {"directory": str(destination), "yes": True,
                              "force_now": True})()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_restore(self.config(), args)
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(
            (Path(self.server.state_dir) / subwave.BLOCKLIST_FILE).read_bytes(), original)

    def test_restore_writes_the_blocklist_before_assets(self):
        destination, _ = subwave.take_backup(self.config(), self.tmp / "backups")
        args = type("A", (), {"directory": str(destination), "yes": False,
                              "force_now": True})()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            subwave.cmd_restore(self.config(), args)
        lines = [line for line in buffer.getvalue().splitlines() if line.startswith("would")]
        blocklist_at = next(index for index, line in enumerate(lines) if "blocklist.json" in line)
        first_asset = next(index for index, line in enumerate(lines) if "/skills/" in line)
        self.assertLess(blocklist_at, first_asset, "config goes back before assets")

    def config(self):
        return subwave.Config(dotenv={}, environ={
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
            "SUBWAVE_HOST": "mock-host",
            "SUBWAVE_STATE_DIR": "/var/sub-wave",
        })


if __name__ == "__main__":
    unittest.main()
