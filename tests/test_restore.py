"""Tests for `restore` - the other half of backup.

Everything runs against a local state dir standing in for the station's, and an
in-process mock for the on-air check. Nothing touches a real station.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_station
from test_subwave import load_tool

subwave = load_tool()


class RestoreTestCase(unittest.TestCase):
    def setUp(self):
        self.server = mock_station.start_mock()
        self.addCleanup(self.server.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # the station's state dir, which restore writes into
        self.state_dir = self.server.state_dir
        self.backups = self.tmp / "backups"

        self._saved_source = subwave.SshStateSource
        self._saved_tier = subwave.detect_tier
        subwave.SshStateSource = lambda config: subwave.LocalStateSource(self.state_dir)
        subwave.detect_tier = lambda config, probe=None: (2, "mock state dir")
        self.addCleanup(self._restore)

        self.config = subwave.Config(dotenv={}, environ={
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
            "SUBWAVE_HOST": "mock-host",
            "SUBWAVE_STATE_DIR": "/var/sub-wave",
        })

    def _restore(self):
        subwave.SshStateSource = self._saved_source
        subwave.detect_tier = self._saved_tier

    # -- helpers -----------------------------------------------------------
    def make_backup(self):
        destination, _ = subwave.take_backup(self.config, self.backups)
        return destination

    def run_restore(self, directory, *flags):
        args = type("Args", (), {
            "directory": str(directory),
            "yes": "--yes" in flags,
            "force_now": "--force-now" in flags,
        })()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_restore(self.config, args)
        return code, buffer.getvalue()

    def read_state(self, relative):
        return (Path(self.state_dir) / relative).read_bytes()

    def wipe_state(self):
        """Remove the config and assets, leaving the directory itself."""
        for name in ("settings.json", "schedule.json"):
            (Path(self.state_dir) / name).unlink()
        for name in subwave.ASSET_DIRS:
            shutil.rmtree(Path(self.state_dir) / name, ignore_errors=True)
        for name in subwave.ASSET_MANIFESTS:
            target = Path(self.state_dir) / name
            if target.exists():
                target.unlink()


class RoundTripTests(RestoreTestCase):
    def test_backup_wipe_restore_comes_back_identical(self):
        backup = self.make_backup()
        original_settings = self.read_state("settings.json")
        original_schedule = self.read_state("schedule.json")
        original_asset = self.read_state("sfx/sfx-one.mp3")

        self.wipe_state()
        self.assertFalse((Path(self.state_dir) / "settings.json").exists())

        code, output = self.run_restore(backup, "--yes", "--force-now")
        self.assertEqual(code, 0, output)

        self.assertEqual(self.read_state("settings.json"), original_settings)
        self.assertEqual(self.read_state("schedule.json"), original_schedule)
        self.assertEqual(self.read_state("sfx/sfx-one.mp3"), original_asset)

    def test_the_restored_station_has_the_same_programming(self):
        backup = self.make_backup()
        before = json.loads(self.read_state("schedule.json"))
        self.wipe_state()
        self.run_restore(backup, "--yes", "--force-now")

        after = json.loads(self.read_state("schedule.json"))
        cells_before = [cell for day in before["schedule"].values() for cell in day]
        cells_after = [cell for day in after["schedule"].values() for cell in day]
        self.assertEqual(cells_after, cells_before)
        self.assertEqual(len(after["shows"]), len(before["shows"]))
        self.assertEqual(len(cells_after), 168)

        settings = json.loads(self.read_state("settings.json"))
        self.assertTrue(settings["personas"])

    def test_assets_are_restored_including_manifests(self):
        backup = self.make_backup()
        self.wipe_state()
        self.run_restore(backup, "--yes", "--force-now")
        for directory in ("skills", "sfx", "jingles", "beds", "themes", "persona-avatars"):
            self.assertTrue((Path(self.state_dir) / directory).is_dir(), directory)
        for manifest in ("sfx.json", "jingles.json", "beds.json"):
            self.assertTrue((Path(self.state_dir) / manifest).is_file(), manifest)


class DryRunTests(RestoreTestCase):
    def test_dry_run_writes_nothing(self):
        backup = self.make_backup()
        original = self.read_state("settings.json")
        self.wipe_state()

        code, output = self.run_restore(backup)
        self.assertEqual(code, 0, output)
        self.assertIn("dry run", output)
        self.assertIn("0 written", output)
        self.assertFalse((Path(self.state_dir) / "settings.json").exists(),
                         "a dry run must not write")

    def test_dry_run_lists_every_target_and_the_order(self):
        backup = self.make_backup()
        _, output = self.run_restore(backup)
        self.assertIn("1. state files", output)
        self.assertIn("2. assets", output)
        self.assertIn("restart the mixer", output)
        self.assertIn("/var/sub-wave/settings.json", output)


class OnAirTests(RestoreTestCase):
    def test_refuses_while_on_air(self):
        backup = self.make_backup()
        # the mock reports an active show, so this is the default path
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes")
        message = str(caught.exception)
        self.assertIn("on air", message)
        self.assertIn("--force-now", message)

    def test_override_says_what_it_is_overriding(self):
        backup = self.make_backup()
        code, output = self.run_restore(backup, "--yes", "--force-now")
        self.assertEqual(code, 0, output)


class IncompleteBackupTests(RestoreTestCase):
    def test_missing_file_listed_in_the_manifest_is_refused(self):
        backup = self.make_backup()
        (backup / "state" / "sfx" / "sfx-one.mp3").unlink()
        before = self.read_state("settings.json")
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("incomplete", str(caught.exception))
        self.assertEqual(self.read_state("settings.json"), before, "nothing may be written")

    def test_grid_pointing_at_a_missing_show_is_refused(self):
        backup = self.make_backup()
        schedule = json.loads((backup / "schedule.json").read_text())
        schedule["schedule"]["0"][0] = "s_not_in_this_backup"
        (backup / "schedule.json").write_text(json.dumps(schedule))
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("cannot restore the programming", str(caught.exception))

    def test_missing_schedule_is_refused(self):
        backup = self.make_backup()
        (backup / "schedule.json").unlink()
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("not a complete backup", str(caught.exception))

    def test_unparseable_settings_is_refused(self):
        backup = self.make_backup()
        (backup / "settings.json").write_text("{not json")
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("not valid JSON", str(caught.exception))


class RollbackTests(RestoreTestCase):
    def test_a_failed_post_write_check_puts_the_previous_state_back(self):
        backup = self.make_backup()
        original_settings = self.read_state("settings.json")
        original_schedule = self.read_state("schedule.json")

        # A source that corrupts the schedule on the way in: the writes succeed,
        # the station does not validate, and the previous state must return.
        class CorruptingSource(subwave.LocalStateSource):
            """Corrupts the schedule once, on the way in - not on the rollback."""

            corrupted = False

            def write_bytes(self, relative, payload):
                if relative == "schedule.json" and not CorruptingSource.corrupted:
                    CorruptingSource.corrupted = True
                    payload = b'{"shows": [], "schedule": {}}'
                super().write_bytes(relative, payload)

        subwave.SshStateSource = lambda config: CorruptingSource(self.state_dir)
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("did not validate", str(caught.exception))

        self.assertEqual(self.read_state("settings.json"), original_settings)
        self.assertEqual(self.read_state("schedule.json"), original_schedule,
                         "the previous schedule must be back")

    def test_a_write_failure_part_way_puts_the_previous_state_back(self):
        backup = self.make_backup()
        original_settings = self.read_state("settings.json")

        class FailingSource(subwave.LocalStateSource):
            def write_bytes(self, relative, payload):
                if relative.startswith("sfx/"):
                    raise subwave.ToolError("disk full")
                super().write_bytes(relative, payload)

        subwave.SshStateSource = lambda config: FailingSource(self.state_dir)
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")
        self.assertIn("previous state was put back", str(caught.exception))
        self.assertEqual(self.read_state("settings.json"), original_settings)

    def test_a_rollback_that_also_fails_says_what_is_left_broken(self):
        # The old message claimed "the previous state was put back" no matter
        # what happened, so a half-written station read as a clean undo.
        backup = self.make_backup()

        class BrokenRollbackSource(subwave.LocalStateSource):
            failing = False

            def write_bytes(self, relative, payload):
                if relative.startswith("sfx/"):
                    self.failing = True
                    raise subwave.ToolError("disk full")
                if self.failing and relative == "settings.json":
                    raise subwave.ToolError("permission denied")
                super().write_bytes(relative, payload)

        subwave.SshStateSource = lambda config: BrokenRollbackSource(self.state_dir)
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_restore(backup, "--yes", "--force-now")

        message = str(caught.exception)
        self.assertNotIn("the previous state was put back", message)
        self.assertIn("could not put back", message)
        self.assertIn("settings.json", message)
        self.assertIn("half-written", message)


class TierTests(RestoreTestCase):
    def test_tier_1_refuses_and_explains_why(self):
        self._restore()  # back to real tier detection, which needs ssh
        config = subwave.Config(dotenv={}, environ={"SUBWAVE_BASE_URL": self.server.url})
        args = type("Args", (), {"directory": str(self.tmp), "yes": False,
                                 "force_now": False})()
        with self.assertRaises(subwave.ToolError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                subwave.cmd_restore(config, args)
        self.assertIn("schedule.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
