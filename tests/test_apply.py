"""Tests for `apply`, run against tests/mock_station.py only.

No test here contacts a live station: every request goes to a loopback mock.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
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

    def test_the_grid_is_written_after_the_shows_and_references_real_ids(self):
        self.run_apply(yes=True)
        # Not "last": the blocklist and skills are written after the grid, and the
        # read-backs come after those. The invariant is that the grid is written
        # only once every show it names exists on the station.
        writes = [entry["path"] for entry in self.log() if entry["method"] != "GET"]
        self.assertIn("/api/schedule", writes)
        self.assertLess(writes.index("/api/shows"), writes.index("/api/schedule"))

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

    def test_prune_refuses_without_a_backup(self):
        # Deleting on the station has no undo, so a backup is mandatory.
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True, prune=True, no_backup=True)
        self.assertIn("--no-backup", str(caught.exception))
        self.assertIn("backup is mandatory", str(caught.exception))


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


class ErrorEchoTests(ApplyTestCase):
    """A station that echoes the request body must not leak the secret in it."""

    MARKER = "sk-live-UNIQUE-MARKER"

    def test_an_error_echoing_the_body_is_scrubbed(self):
        settings_path = subwave.INSTANCE_DIR / "settings.json"
        document = json.loads(settings_path.read_text())
        document.setdefault("llm", {})["apiKey"] = subwave.SENTINEL
        settings_path.write_text(json.dumps(document, indent=2))

        original = subwave.http_json
        echoed = []

        def echo(config, path, **kwargs):
            body = kwargs.get("body")
            if kwargs.get("method") == "POST" and body and '"llm"' in json.dumps(body):
                echoed.append(True)
                return 400, {"error": json.dumps(body)}
            return original(config, path, **kwargs)

        subwave.http_json = echo
        self.addCleanup(lambda: setattr(subwave, "http_json", original))
        os.environ["ANTHROPIC_API_KEY"] = self.MARKER
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))

        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)

        self.assertTrue(echoed, "the echoing step must have been reached")
        message = str(caught.exception)
        self.assertNotIn(self.MARKER, message,
                         "a station's error body must not print a rehydrated secret")
        self.assertIn(subwave.SENTINEL, message)


class IdempotenceTests(ApplyTestCase):
    def test_apply_twice_reports_no_changes_the_second_time(self):
        self.run_apply(yes=True)
        first = len(self.log())
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("no changes", output)
        self.assertEqual(len(self.log()), first)


class CrossReferenceRefusalTests(ApplyTestCase):
    def test_a_show_whose_host_does_not_exist_never_reaches_the_station(self):
        path = subwave.INSTANCE_DIR / "character" / "shows" / "late-harbour.json"
        document = json.loads(path.read_text())
        document["personaId"] = "p_ghost"
        path.write_text(json.dumps(document, indent=2))

        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        self.assertIn("p_ghost", str(caught.exception))
        self.assertEqual([entry for entry in self.log() if entry["method"] != "GET"], [],
                         "a show with no host must not be written")


class OverrideTests(ApplyTestCase):
    def set_override(self, slug):
        path = subwave.INSTANCE_DIR / "character" / "schedule.json"
        document = json.loads(path.read_text())
        document["override"] = slug
        path.write_text(json.dumps(document, indent=2))

    def show_slug(self):
        lock = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())
        return sorted(lock["shows"])[0]

    def test_a_dangling_override_is_refused_before_any_write(self):
        self.set_override("no-such-show")
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        self.assertIn("no-such-show", str(caught.exception))
        self.assertEqual([entry for entry in self.log() if entry["method"] != "GET"], [])

    def test_the_override_is_written_as_the_show_id(self):
        slug = self.show_slug()
        self.set_override(slug)
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0, output)

        lock = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())
        sent = [entry for entry in self.log() if entry["path"] == "/api/schedule"][0]["body"]
        self.assertEqual(sent["override"], lock["shows"][slug])
        self.assertEqual(self.server.station.schedule["override"], lock["shows"][slug])

    def test_a_station_that_clears_the_override_is_caught(self):
        self.set_override(self.show_slug())
        original = self.server.station.replace_schedule

        def drops_override(body):
            code, payload = original(body)
            if isinstance(body, dict) and "override" in body:
                self.server.station.schedule["override"] = None
            return code, payload

        self.server.station.replace_schedule = drops_override
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        message = str(caught.exception)
        self.assertIn("did NOT land", message)
        self.assertIn("override", message)

    def test_build_grid_raises_rather_than_dropping_an_unknown_override(self):
        model = subwave.load_instance_model()
        model["override"] = "no-such-show"
        lock = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())
        with self.assertRaises(subwave.ToolError) as caught:
            subwave.build_grid(model, lock)
        self.assertIn("no-such-show", str(caught.exception))


class BlocklistVerificationTests(ApplyTestCase):
    """The blocklist is written like any other document, so it is read back."""

    def test_the_blocklist_is_read_back_after_the_writes(self):
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0, output)

        log = self.log()
        writes = [index for index, entry in enumerate(log)
                  if entry["method"] in ("POST", "PUT")
                  and entry["path"].startswith("/api/library/blocklist")]
        reads = [index for index, entry in enumerate(log)
                 if entry["method"] == "GET" and entry["path"] == "/api/library/blocklist"]
        self.assertTrue(writes, "the blocklist must actually be written")
        self.assertTrue(reads, "the blocklist must be read back")
        self.assertGreater(reads[-1], writes[-1],
                           "the read-back must come after the last blocklist write")

    def test_a_station_that_ignores_a_blocklist_write_is_caught(self):
        path = subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE
        document = json.loads(path.read_text())
        document.setdefault("entries", []).append(
            {"type": "track", "id": "tr-999", "name": "Ghost Track"})
        path.write_text(json.dumps(document, indent=2))

        # Accepts the write, stores nothing: the failure this check exists for.
        self.server.station.add_blocklist_entry = lambda body: (200, dict(body or {}))

        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        message = str(caught.exception)
        self.assertIn("did NOT land", message)
        self.assertIn("tr-999", message)


class Tier1VerificationTests(ApplyTestCase):
    """Tier 1 cannot read settings.json, so it says what it did and did not prove."""

    def test_the_visible_persona_fields_are_read_back(self):
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0, output)

        log = self.log()
        settings_writes = [index for index, entry in enumerate(log)
                           if entry["method"] == "POST" and entry["path"] == "/api/settings"]
        reads = [index for index, entry in enumerate(log)
                 if entry["method"] == "GET" and entry["path"] == "/api/personas"]
        self.assertTrue(reads, "tier 1 must read the personas back")
        self.assertGreater(reads[-1], settings_writes[-1])

    def test_it_says_settings_were_not_verified(self):
        code, output = self.run_apply(yes=True)
        self.assertEqual(code, 0, output)
        self.assertIn("NOT verified", output)
        self.assertIn("/api/personas", output)

    def test_a_station_that_ignores_a_persona_rename_is_caught(self):
        path = sorted((subwave.INSTANCE_DIR / "character" / "personas").glob("*.json"))[0]
        document = json.loads(path.read_text())
        document["name"] = "Ghost Host"
        path.write_text(json.dumps(document, indent=2))

        original = self.server.station.merge_settings

        def drops_personas(body):
            return original({key: value for key, value in body.items() if key != "personas"})

        self.server.station.merge_settings = drops_personas
        with self.assertRaises(subwave.ToolError) as caught:
            self.run_apply(yes=True)
        message = str(caught.exception)
        self.assertIn("did NOT land", message)
        self.assertIn("Ghost Host", message)


class DigestTests(ApplyTestCase):
    """The applied digest must not be a hash of a live credential."""

    def mark_password_secret(self):
        path = subwave.INSTANCE_DIR / "settings.json"
        document = json.loads(path.read_text())
        document["privacy"]["password"] = subwave.SENTINEL
        path.write_text(json.dumps(document, indent=2))

    def test_the_digest_is_blind_to_the_secret_value(self):
        steps = [{"method": "POST", "path": "/api/settings",
                  "body": {"llm": {"apiKey": "sk-live-A"}, "station": "Harbour"}}]
        other = [{"method": "POST", "path": "/api/settings",
                  "body": {"llm": {"apiKey": "sk-live-B"}, "station": "Harbour"}}]
        grid = {"schedule": {}, "override": None}
        self.assertEqual(subwave.payload_hash(steps, grid), subwave.payload_hash(other, grid),
                         "two runs differing only in a secret must share a digest")

    def test_the_digest_is_not_the_hash_of_the_rehydrated_body(self):
        body = {"llm": {"apiKey": "sk-live-A"}, "station": "Harbour"}
        steps = [{"method": "POST", "path": "/api/settings", "body": body}]
        grid = {"schedule": {}, "override": None}
        rehydrated = hashlib.sha256(json.dumps(
            {"steps": [{"method": "POST", "path": "/api/settings", "body": body}],
             "grid": grid, "blocklist": {}, "skills": {}},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertNotEqual(subwave.payload_hash(steps, grid), rehydrated,
                            "ids.lock.json must not hold a sha256 of the real credential")

    def test_a_real_apply_records_a_digest_a_rotated_secret_does_not_change(self):
        self.mark_password_secret()
        code, output = self.run_apply(
            config=self.config(SUBWAVE_STATION_PASSWORD="first-secret"), yes=True)
        self.assertEqual(code, 0, output)

        sent = [entry for entry in self.log()
                if entry["path"] == "/api/settings" and "privacy" in entry["body"]]
        self.assertEqual(sent[0]["body"]["privacy"]["password"], "first-secret",
                         "the secret really was in the payload that got hashed")

        before = len(self.log())
        code, output = self.run_apply(
            config=self.config(SUBWAVE_STATION_PASSWORD="rotated-secret"), yes=True)
        self.assertEqual(code, 0, output)
        self.assertIn("no changes since the last apply", output)
        self.assertEqual([entry for entry in self.log()[before:] if entry["method"] != "GET"], [])


class NoOpGuardTests(BackupBeforeWriteTests):
    """A no-op apply must not spend a backup or refuse a live station.

    The digest check used to sit after both guards, so "nothing to do" still cost
    a full backup, and a settled station mid-show was refused for a write that
    would never happen.
    """

    def apply_yes(self, force_now=True, no_backup=False):
        args = argparse.Namespace(yes=True, force_now=force_now, no_backup=no_backup,
                                  prune=False, backup_dir=str(self.backup_root))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_apply(self.config(), args)
        return code, buffer.getvalue()

    def settle(self):
        code, output = self.apply_yes()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(sorted(self.backup_root.glob("*/*"))), 1)

    def test_a_no_op_apply_takes_no_backup(self):
        self.settle()
        before = len(self.log())
        code, output = self.apply_yes()
        self.assertEqual(code, 0, output)
        self.assertIn("no changes since the last apply", output)
        self.assertEqual(len(sorted(self.backup_root.glob("*/*"))), 1,
                         "a run with nothing to write must not take a second backup")
        self.assertEqual([entry for entry in self.log()[before:] if entry["method"] != "GET"], [])

    def test_a_no_op_apply_does_not_refuse_a_live_station(self):
        self.settle()
        before = len(self.log())
        code, output = self.apply_yes(force_now=False)
        self.assertEqual(code, 0, output)
        self.assertIn("no changes since the last apply", output)
        self.assertEqual([entry for entry in self.log()[before:] if entry["method"] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
