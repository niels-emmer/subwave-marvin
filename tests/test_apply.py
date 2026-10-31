"""Tests for `apply`, run against tests/mock_station.py only.

No test here contacts a live station: every request goes to a loopback mock.
"""

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import unittest

import sys
from pathlib import Path

# Both invocation styles must work: `discover -s tests` puts this directory on
# sys.path, a dotted `unittest tests.test_smoke` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_station
from test_subwave import load_tool

subwave = load_tool()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "station.json"


class ApplyTestCase(unittest.TestCase):
    """Seeds instance/ from the same fixture the mock serves, at tier 1."""

    lossy = False

    def setUp(self):
        self.server = mock_station.start_mock()
        self.addCleanup(self.server.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self._saved_instance = subwave.INSTANCE_DIR
        subwave.INSTANCE_DIR = self.tmp / "instance"
        self.addCleanup(lambda: setattr(subwave, "INSTANCE_DIR", self._saved_instance))

        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        model = subwave.build_live_model(document["settings"], document["schedule"])
        subwave.write_instance(model, {}, capture={
            "tier": 2 if not self.lossy else 1,
            "at": "2026-10-31T00:00:00Z",
            "lossy": self.lossy,
            "lost": ["show definitions: 19 of 26 fields"] if self.lossy else [],
        })
        # the blocklist is a config document too; seed it so diff has a baseline,
        # and give its rules the slugs validate now requires
        blocklist = json.loads(json.dumps(self.server.station.blocklist))
        subwave.assign_rule_slugs(blocklist["rules"], {})
        subwave.write_json(subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE, blocklist)
        # and the lock, or apply would treat each seeded rule as NEW and mint a
        # second uuid for it - the exact trap the lock exists to prevent
        lock = {"version": 1, "rules": {}, "ids": {}}
        for rule in blocklist["rules"]:
            if rule.get("id"):
                lock["rules"][rule["slug"]] = rule["id"]
                lock["ids"][rule["id"]] = rule["slug"]
        subwave.write_json(subwave.INSTANCE_DIR / subwave.BLOCKLIST_LOCK, lock)

    # -- helpers -----------------------------------------------------------
    def config(self, **overrides):
        environ = {
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
        }
        environ.update(overrides)
        return subwave.Config(dotenv={}, environ=environ)

    def run_apply(self, config=None, **flags):
        args = argparse.Namespace(
            yes=flags.get("yes", False),
            force_now=flags.get("force_now", True),
            no_backup=flags.get("no_backup", True),
            prune=flags.get("prune", False),
            backup_dir=None,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_apply(config or self.config(), args)
        return code, buffer.getvalue()

    def log(self):
        return self.server.station.requests


class DryRunTests(ApplyTestCase):
    def test_dry_run_sends_nothing(self):
        code, output = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.log(), [], "a dry run must not send a single request")
        self.assertIn("dry run", output)
        self.assertIn("0 requests sent", output)

    def test_dry_run_lists_every_step(self):
        _, output = self.run_apply()
        self.assertIn("POST /api/settings", output)
        self.assertIn("POST /api/shows", output)
        self.assertIn("PUT", output)
        self.assertIn("/api/schedule", output)


class WriteTests(ApplyTestCase):
    def test_apply_sends_whole_show_objects(self):
        code, _ = self.run_apply(yes=True)
        self.assertEqual(code, 0)
        show_bodies = [entry["body"]["show"] for entry in self.log()
                       if entry["path"] == "/api/shows"]
        self.assertTrue(show_bodies)
        for body in show_bodies:
            self.assertEqual(len(body), 26, "a show must be sent whole, never trimmed")
            self.assertIn("playlistIds", body)
            self.assertIn("eras", body)

    def test_personas_are_sent_whole(self):
        self.run_apply(yes=True)
        settings_posts = [entry for entry in self.log()
                          if entry["path"] == "/api/settings" and "personas" in entry["body"]]
        self.assertTrue(settings_posts)
        persona = settings_posts[0]["body"]["personas"][0]
        self.assertEqual(len(persona), 18)

    def test_grid_is_written_last_and_references_real_ids(self):
        self.run_apply(yes=True)
        paths = [entry["path"] for entry in self.log()]
        self.assertEqual(paths[-1], "/api/schedule")
        self.assertLess(paths.index("/api/shows"), len(paths) - 1)

        grid = [entry for entry in self.log() if entry["path"] == "/api/schedule"][0]["body"]
        cells = [cell for day in grid["schedule"].values() for cell in day]
        self.assertEqual(len(cells), 168)
        known = {show["id"] for show in self.server.station.schedule["shows"]}
        for cell in cells:
            self.assertIn(cell, known, "the grid must only reference ids that exist")

    def test_mid_show_refuses_without_force_now(self):
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True, force_now=False)
        self.assertIn("mid-show", str(caught.exception))
        writes = [e for e in self.log() if e["method"] != "GET"]
        self.assertEqual(writes, [], "nothing may be written while refusing")

    def test_prune_is_not_implemented(self):
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True, prune=True)
        self.assertIn("not implemented", str(caught.exception))


class AuthTests(ApplyTestCase):
    def test_wrong_password_gives_a_clear_401_error(self):
        config = self.config(SUBWAVE_ADMIN_PASS="wrong-password")
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(config=config, yes=True)
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("SUBWAVE_ADMIN_PASS", message)

    def test_missing_credentials_are_a_config_error(self):
        config = self.config(SUBWAVE_ADMIN_USER="", SUBWAVE_ADMIN_PASS="")
        with self.assertRaises(subwave.ConfigError):
            self.run_apply(config=config, yes=True)


class GuardTests(ApplyTestCase):
    def test_sentinel_is_refused_before_any_request(self):
        settings_path = subwave.INSTANCE_DIR / "settings.json"
        document = json.loads(settings_path.read_text())
        document["privacy"]["password"] = subwave.SENTINEL
        settings_path.write_text(json.dumps(document, indent=2))

        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        self.assertIn("__REDACTED__", str(caught.exception))
        self.assertEqual(self.log(), [], "the refusal must happen before any request")

    def test_tier_1_requires_no_backup_flag(self):
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True, no_backup=False)
        self.assertIn("backup", str(caught.exception).lower())
        self.assertEqual([e for e in self.log() if e["method"] != "GET"], [])

    def test_tier_1_writes_with_no_backup(self):
        code, output = self.run_apply(yes=True, no_backup=True)
        self.assertEqual(code, 0)
        self.assertIn("skipped (--no-backup)", output)


class LossyCaptureTests(ApplyTestCase):
    lossy = True

    def test_lossy_capture_is_refused_outright(self):
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        message = str(caught.exception)
        self.assertIn("lossy", message)
        self.assertIn("19 fields", message)
        self.assertEqual(self.log(), [], "a lossy capture is refused before any request")


class IdAssignmentTests(ApplyTestCase):
    def add_new_show(self):
        path = subwave.INSTANCE_DIR / "character" / "shows" / "brand-new.json"
        document = json.loads(
            (subwave.INSTANCE_DIR / "character" / "shows" / "late-harbour.json").read_text())
        document.pop("id", None)
        document["name"] = "Brand New Show"
        path.write_text(json.dumps(document, indent=2))
        return path

    def test_new_show_gets_an_id_and_the_lockfile_is_updated(self):
        path = self.add_new_show()
        self.run_apply(yes=True)

        written = json.loads(path.read_text())
        self.assertTrue(written.get("id", "").startswith("s_"))
        lock = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())
        self.assertEqual(lock["shows"]["brand-new"], written["id"])
        self.assertEqual(lock["ids"][written["id"]], "brand-new")

    def test_a_second_apply_is_a_no_op(self):
        self.add_new_show()
        self.run_apply(yes=True)
        before = len(self.log())
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no changes", output)
        self.assertEqual(len(self.log()), before, "a no-op apply must not send anything")


class DiffAfterApplyTests(ApplyTestCase):
    def test_diff_is_clean_after_apply(self):
        self.run_apply(yes=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_diff(self.config(), argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertIn("in sync", buffer.getvalue())

    def test_tier_1_diff_never_claims_a_full_match(self):
        self.run_apply(yes=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            subwave.cmd_diff(self.config(), argparse.Namespace())
        output = buffer.getvalue()
        self.assertIn("not a full match", output)
        self.assertIn("trimmed comparison", output)

    def test_tier_1_diff_says_what_it_cannot_see(self):
        self.run_apply(yes=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            subwave.cmd_diff(self.config(), argparse.Namespace())
        self.assertIn("trimmed comparison", buffer.getvalue())


class BackupBeforeWriteTests(ApplyTestCase):
    """apply --yes without --no-backup takes a backup first.

    This path had no coverage: every other apply test passes --no-backup, so a
    return-signature change in take_backup shipped broken and 166 green tests did
    not notice.
    """

    def setUp(self):
        super().setUp()
        self.backup_root = self.tmp / "backups"
        # Tier 2 with the mock's state dir standing in for the host.
        self._saved_source = subwave.SshStateSource
        self._saved_tier = subwave.detect_tier
        self._saved_read = subwave.read_state_file
        subwave.SshStateSource = lambda config: subwave.LocalStateSource(
            self.server.state_dir)
        # The mock keeps the station in memory; the state dir is only its seed.
        # Reading back the seed would compare against pre-apply state.
        subwave.read_state_file = lambda config, name: json.dumps(
            self.server.station.settings if name == "settings.json"
            else self.server.station.schedule)
        subwave.detect_tier = lambda config, probe=None: (2, "mock state dir")
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        subwave.SshStateSource = self._saved_source
        subwave.read_state_file = self._saved_read
        subwave.detect_tier = self._saved_tier

    def test_apply_without_no_backup_takes_one_and_proceeds(self):
        before = len(self.log())
        args = argparse.Namespace(yes=True, force_now=True, no_backup=False,
                                  prune=False, backup_dir=str(self.backup_root))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_apply(self.config(), args)
        output = buffer.getvalue()

        self.assertEqual(code, 0, output)
        self.assertIn("backup    ", output)
        self.assertNotIn("skipped", output)

        stamps = sorted(self.backup_root.glob("*/*"))
        self.assertEqual(len(stamps), 1, "exactly one backup should be taken")
        self.assertTrue((stamps[0] / "settings.json").is_file())
        self.assertTrue((stamps[0] / "schedule.json").is_file())
        self.assertTrue((stamps[0] / "RESTORE.md").is_file())

        writes = [entry for entry in self.log()[before:] if entry["method"] != "GET"]
        self.assertTrue(writes, "apply must still write after taking the backup")


class IdempotenceTests(ApplyTestCase):
    def test_apply_twice_reports_no_changes_the_second_time(self):
        self.run_apply(yes=True)
        first = len(self.log())
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no changes", output)
        self.assertEqual(len(self.log()), first)


if __name__ == "__main__":
    unittest.main()
