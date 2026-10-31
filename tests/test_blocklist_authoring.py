"""Blocklist authoring and apply writes.

The trap: a rule id is a server-minted uuid with no slug. Re-applying without a
lock mints a NEW uuid for the same rule every time, filling RULES_MAX=50 with
near-duplicates that each block a slightly different subset. These tests pin the
slug-to-uuid lock that prevents it.
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


class AuthoringTestCase(unittest.TestCase):
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

    def pull(self):
        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)
        return output

    def repo_blocklist(self):
        return json.loads((subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE).read_text())

    def lock(self):
        path = subwave.INSTANCE_DIR / subwave.BLOCKLIST_LOCK
        return json.loads(path.read_text()) if path.is_file() else {"rules": {}, "ids": {}}

    def station_rules(self):
        return self.server.station.blocklist["rules"]

    def apply(self):
        return self.run_cli("apply", "--yes", "--force-now", "--no-backup")


class RuleIdentityTests(AuthoringTestCase):
    def setUp(self):
        super().setUp()
        self.pull()
        self.baseline = len(self.station_rules())

    def test_a_new_rule_is_posted_once_and_locked(self):
        code, output = self.run_cli("blocklist", "rule", "add", "--label", "No opera",
                                    "--field", "genre", "--value", "Opera")
        self.assertEqual(code, 0, output)

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.station_rules()), self.baseline + 1,
                         "exactly one NEW rule on the station")

        lock = self.lock()
        self.assertIn("no-opera", lock["rules"], "the slug is locked to its uuid")
        uuid = lock["rules"]["no-opera"]
        stored = next(rule for rule in self.station_rules() if rule["label"] == "No opera")
        self.assertEqual(stored["id"], uuid)

    def test_a_second_apply_does_not_mint_a_duplicate(self):
        self.run_cli("blocklist", "rule", "add", "--label", "No opera",
                     "--field", "genre", "--value", "Opera")
        self.assertEqual(self.apply()[0], 0)
        first = self.lock()["rules"]["no-opera"]

        before = len(self.server.station.requests)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(len(self.station_rules()), self.baseline + 1, "no duplicate uuid")
        self.assertEqual(self.lock()["rules"]["no-opera"], first)

        posts = [entry for entry in self.server.station.requests[before:]
                 if entry["path"] == "/api/library/blocklist/rules"]
        self.assertEqual(posts, [], "an existing rule must not be POSTed again")

    def test_editing_a_rule_puts_to_the_same_uuid(self):
        self.run_cli("blocklist", "rule", "add", "--label", "No opera",
                     "--field", "genre", "--value", "Opera")
        self.assertEqual(self.apply()[0], 0)
        uuid = self.lock()["rules"]["no-opera"]

        code, output = self.run_cli("blocklist", "rule", "edit", "no-opera",
                                    "--value", "Opera", "--value", "Operetta")
        self.assertEqual(code, 0, output)

        before = len(self.server.station.requests)
        self.assertEqual(self.apply()[0], 0)
        puts = [entry for entry in self.server.station.requests[before:]
                if entry["path"].startswith("/api/library/blocklist/rules/")]
        mine = [entry for entry in puts if entry["path"].endswith(uuid)]
        self.assertEqual(len(mine), 1, "the edited rule is PUT to its own uuid")
        self.assertEqual(mine[0]["body"]["values"], ["Opera", "Operetta"])

        posts = [entry for entry in self.server.station.requests[before:]
                 if entry["path"] == "/api/library/blocklist/rules"]
        self.assertEqual(posts, [], "an edit is a PUT, never a POST")

    def test_the_slug_is_never_sent_to_the_station(self):
        self.run_cli("blocklist", "rule", "add", "--label", "No opera",
                     "--field", "genre", "--value", "Opera")
        self.assertEqual(self.apply()[0], 0)
        for entry in self.server.station.requests:
            if isinstance(entry.get("body"), dict) and "field" in entry["body"]:
                self.assertNotIn("slug", entry["body"])

    def test_pull_derives_slugs_and_records_uuids(self):
        self.pull()
        rules = self.repo_blocklist()["rules"]
        self.assertTrue(rules)
        for rule in rules:
            self.assertTrue(rule.get("slug"), "every captured rule gets a slug")
        lock = self.lock()
        self.assertEqual(len(lock["rules"]), len(rules))


class EntryTests(AuthoringTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_blocking_an_entry_reaches_the_station(self):
        code, output = self.run_cli("blocklist", "block", "--type", "artist",
                                    "--id", "artist-99", "--name", "Some Band")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.apply()[0], 0)

        ids = [entry["id"] for entry in self.server.station.blocklist["entries"]]
        self.assertIn("artist-99", ids)

    def test_an_already_blocked_entry_is_success(self):
        # pull captured subsonic-1 because the station already holds it, so the
        # re-apply POSTs it and the station answers 409.
        code, output = self.apply()
        self.assertEqual(code, 0, "409 already blocked is success, not an error")
        self.assertIn("already blocked", output)


class RemovalTests(AuthoringTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_a_rule_removed_locally_is_pending_and_not_deleted(self):
        before = len(self.station_rules())
        slug = self.repo_blocklist()["rules"][0]["slug"]

        code, output = self.run_cli("blocklist", "rm", slug)
        self.assertEqual(code, 0, output)

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("pending", output)
        self.assertIn(slug, output)
        self.assertEqual(len(self.station_rules()), before, "apply must not delete")

        deletes = [entry for entry in self.server.station.requests
                   if entry["method"] == "DELETE"]
        self.assertEqual(deletes, [], "apply never deletes")

    def test_validate_reports_the_pending_removal(self):
        slug = self.repo_blocklist()["rules"][0]["slug"]
        self.run_cli("blocklist", "rm", slug)
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("pending removal", output)


class ValidationTests(AuthoringTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_a_bad_field_is_rejected_before_any_request(self):
        before = len(self.server.station.requests)
        code, output = self.run_cli("blocklist", "rule", "add", "--label", "Bad",
                                    "--field", "bpm", "--value", "120")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.server.station.requests), before)

    def test_thirteen_values_are_rejected_before_any_request(self):
        before = len(self.server.station.requests)
        args = ["blocklist", "rule", "add", "--label", "Too many", "--field", "genre"]
        for index in range(13):
            args += ["--value", "v%d" % index]
        code, output = self.run_cli(*args)
        self.assertNotEqual(code, 0)
        self.assertIn("1..12", output)
        self.assertEqual(len(self.server.station.requests), before)

    def test_duplicate_slugs_are_rejected(self):
        document = self.repo_blocklist()
        document["rules"].append(dict(document["rules"][0]))
        (subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE).write_text(json.dumps(document))
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("share the slug", output)

    def test_a_rule_without_a_slug_is_rejected(self):
        document = self.repo_blocklist()
        document["rules"][0].pop("slug", None)
        (subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE).write_text(json.dumps(document))
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("no slug", output)


if __name__ == "__main__":
    unittest.main()
