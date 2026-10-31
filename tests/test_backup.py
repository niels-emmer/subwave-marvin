"""Backup must be complete enough to restore, unlike the station's own export.

The station's API zip has the assets but omits schedule.json; ours has the
schedule, so it must carry the assets too. These tests drive a backup from the
mock's state dir through the real CLI code path.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import mock_station
from test_subwave import load_tool

subwave = load_tool()


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.server = mock_station.start_mock()
        self.addCleanup(self.server.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Drive the real CLI path, but read the mock's state dir instead of ssh.
        self._saved_source = subwave.SshStateSource
        self._saved_tier = subwave.detect_tier
        subwave.SshStateSource = lambda config: subwave.LocalStateSource(self.server.state_dir)
        subwave.detect_tier = lambda config, probe=None: (2, "mock state dir")
        self.addCleanup(self._restore)

    def _restore(self):
        subwave.SshStateSource = self._saved_source
        subwave.detect_tier = self._saved_tier

    def config(self):
        return subwave.Config(dotenv={}, environ={
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
            "SUBWAVE_HOST": "mock-host",
            "SUBWAVE_STATE_DIR": "/var/sub-wave",
        })

    def run_backup(self, *args):
        args = list(args) or []
        destination = self.tmp / "backups"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_backup(
                self.config(),
                type("Args", (), {"dir": str(destination),
                                  "with_library": "--with-library" in args})())
        return code, buffer.getvalue(), destination

    def backup_dir(self, destination):
        return sorted(destination.rglob("*"))


class ContentsTests(BackupTestCase):
    def test_assets_are_included(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        state = stamp_dir / "state"

        for directory in ("skills", "sfx", "jingles", "beds", "themes", "persona-avatars"):
            self.assertTrue((state / directory).is_dir(), "%s was not backed up" % directory)

        self.assertTrue((state / "skills" / "demo-skill" / "SKILL.md").is_file())
        self.assertTrue((state / "skills" / "demo-skill" / "tool.mjs").is_file())
        self.assertTrue((state / "sfx" / "sfx-one.mp3").is_file())
        for manifest in ("sfx.json", "jingles.json", "beds.json"):
            self.assertTrue((state / manifest).is_file(), "%s manifest missing" % manifest)

    def test_the_two_config_documents_are_at_the_top_level(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        self.assertTrue((stamp_dir / "settings.json").is_file())
        self.assertTrue((stamp_dir / "schedule.json").is_file())
        self.assertTrue((stamp_dir / "manifest.json").is_file())
        self.assertTrue((stamp_dir / "RESTORE.md").is_file())

    def test_secrets_are_never_copied(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        self.assertFalse(list(stamp_dir.rglob("secrets.env")))
        self.assertFalse(list(stamp_dir.rglob("icecast-secrets.env")))


class LibraryTests(BackupTestCase):
    def test_library_db_is_excluded_by_default_and_explained(self):
        _, output, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        self.assertFalse((stamp_dir / "state" / "library.db").exists())
        self.assertIn("library.db", output)
        self.assertIn("expensive to rebuild", output)

    def test_with_library_includes_it(self):
        _, output, destination = self.run_backup("--with-library")
        stamp_dir = sorted((destination).glob("*/*"))[0]
        self.assertTrue((stamp_dir / "state" / "library.db").is_file())
        note = (stamp_dir / "RESTORE.md").read_text()
        self.assertIn("library.db` was included", note)

    def test_skipped_runtime_data_is_listed(self):
        _, output, _ = self.run_backup()
        self.assertIn("logs", output)
        self.assertIn("runtime data", output)


class ManifestTests(BackupTestCase):
    def test_manifest_counts_match_what_is_on_disk(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        manifest = json.loads((stamp_dir / "manifest.json").read_text())

        on_disk = [path for path in stamp_dir.rglob("*")
                   if path.is_file() and path.name not in ("manifest.json", "RESTORE.md")]
        self.assertEqual(manifest["files"], len(on_disk))

        total = sum(path.stat().st_size for path in on_disk)
        self.assertEqual(manifest["bytes"], total)

        self.assertEqual(len(manifest["copied"]), len(on_disk))

    def test_manifest_lists_every_skipped_path_with_a_reason(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        manifest = json.loads((stamp_dir / "manifest.json").read_text())
        skipped = {entry["path"]: entry["reason"] for entry in manifest["skipped"]}
        self.assertIn("library.db", skipped)
        self.assertIn("secrets.env", skipped)
        self.assertIn("logs/app.log", skipped)
        for reason in skipped.values():
            self.assertTrue(reason, "every skip needs a reason")


class RestoreNoteTests(BackupTestCase):
    def test_restore_note_names_every_copied_directory(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        note = (stamp_dir / "RESTORE.md").read_text()

        for directory in ("skills", "sfx", "jingles", "beds", "themes", "persona-avatars"):
            self.assertIn("state/%s/" % directory, note)
        self.assertIn("/var/sub-wave/settings.json", note)
        self.assertIn("/var/sub-wave/schedule.json", note)

    def test_restore_note_states_the_order_and_the_restart(self):
        _, _, destination = self.run_backup()
        stamp_dir = sorted((destination).glob("*/*"))[0]
        note = (stamp_dir / "RESTORE.md").read_text()
        self.assertIn("## 1. State files", note)
        self.assertIn("## 2. Assets", note)
        self.assertIn("## 3. Restart", note)
        self.assertIn("listenerAuth", note)


class TierTests(BackupTestCase):
    def test_tier_1_refuses_with_the_reason(self):
        self._restore()
        config = subwave.Config(dotenv={}, environ={"SUBWAVE_BASE_URL": self.server.url})
        with self.assertRaises(subwave.ToolError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                subwave.cmd_backup(config, type("Args", (), {"dir": str(self.tmp),
                                                             "with_library": False})())
        self.assertIn("schedule", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
