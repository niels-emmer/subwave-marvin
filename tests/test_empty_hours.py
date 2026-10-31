"""Empty hours are legal, and the tool must round-trip them.

The station's schema (controller/src/schemas/schedule.ts) says a slot is a show
id or null; `emptyWeek()` fills every hour with null, and the slot schema also
tolerates "" and an absent day. The admin console lets an operator leave an hour
empty - the station runs autonomously then. Our validator rejected it, blocking
a legitimate console action.
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


class EmptyHoursTests(unittest.TestCase):
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

    def grid_path(self):
        return subwave.INSTANCE_DIR / "character" / "schedule.json"

    def repo_grid(self):
        return json.loads(self.grid_path().read_text())["schedule"]

    def station_grid(self):
        return self.server.station.schedule["schedule"]


class RoundTripTests(EmptyHoursTests):
    def test_a_station_with_a_hole_pulls_and_reapplies_unchanged(self):
        # A hole in the station's grid, as the admin console would leave it.
        self.station_grid()["3"][5] = None
        self.station_grid()["0"][0] = None

        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        self.assertIsNone(self.repo_grid()["3"][5], "the hole must survive the pull")
        self.assertIsNone(self.repo_grid()["0"][0])
        self.assertEqual(len(self.repo_grid()["3"]), 24, "the day must not be dropped")

        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("2 empty", output)

        code, output = self.run_cli("apply", "--yes", "--force-now", "--no-backup")
        self.assertEqual(code, 0, output)
        self.assertIsNone(self.station_grid()["3"][5], "the hole must be re-sent as a hole")
        self.assertIsNone(self.station_grid()["0"][0])

        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)

    def test_an_absent_day_pulls_as_24_empty_cells(self):
        del self.server.station.schedule["schedule"]["6"]

        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        day_six = self.repo_grid()["6"]
        self.assertEqual(len(day_six), 24)
        self.assertEqual([cell for cell in day_six if cell], [], "a blank day is all empty")

        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)

    def test_a_full_grid_still_round_trips(self):
        code, output = self.run_cli("apply", "--yes", "--force-now", "--no-backup")
        self.assertEqual(code, 0, output)
        cells = [cell for day in self.station_grid().values() for cell in day]
        self.assertEqual(len(cells), 168)
        self.assertTrue(all(cells))

        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)


class ValidationTests(EmptyHoursTests):
    def test_validate_accepts_an_empty_hour(self):
        document = json.loads(self.grid_path().read_text())
        document["schedule"]["3"][5] = None
        self.grid_path().write_text(json.dumps(document, indent=2))

        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("1 empty", output)

    def test_validate_still_rejects_an_unknown_show(self):
        document = json.loads(self.grid_path().read_text())
        document["schedule"]["3"][5] = "no-such-show"
        self.grid_path().write_text(json.dumps(document, indent=2))

        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("no-such-show", output)


class ClearTests(EmptyHoursTests):
    def test_clear_empties_the_hours_and_reports_them(self):
        code, output = self.run_cli("character", "add-to-grid", "--clear",
                                    "--days", "mon", "--from", "9", "--to", "11")
        self.assertEqual(code, 0, output)
        self.assertIn("(empty)", output)
        self.assertIn("emptied", output)
        self.assertIsNone(self.repo_grid()["1"][9])
        self.assertIsNone(self.repo_grid()["1"][10])
        self.assertIsNotNone(self.repo_grid()["1"][11], "only the named hours")

        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("2 empty", output)

    def test_cleared_hours_show_in_the_dry_run(self):
        self.assertEqual(self.run_cli("character", "add-to-grid", "--clear",
                                      "--days", "mon", "--from", "9", "--to", "10")[0], 0)
        before = len(self.server.station.requests)
        code, output = self.run_cli("apply")
        self.assertEqual(code, 0, output)
        self.assertIn("dry run", output)
        self.assertEqual(len(self.server.station.requests), before,
                         "a dry run must send nothing")

    def test_clear_does_not_conflict_with_an_occupied_cell(self):
        # mon 09:00 holds a show; emptying it is not a conflict.
        code, output = self.run_cli("character", "add-to-grid", "--clear",
                                    "--days", "mon", "--from", "9", "--to", "10")
        self.assertEqual(code, 0, output)

    def test_clear_rejects_a_slug(self):
        code, output = self.run_cli("character", "add-to-grid", "first-light", "--clear",
                                    "--days", "mon", "--from", "9", "--to", "10")
        self.assertEqual(code, 1)
        self.assertIn("takes no show slug", output)


if __name__ == "__main__":
    unittest.main()
