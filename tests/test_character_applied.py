"""The character layer must drive the station.

The bug these guard: settings.json also carried the six identity fields, apply
sent that stale copy wholesale, and character.json was only ever read. Editing
the character layer was a silent no-op that returned HTTP 200.
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


class CharacterAppliedTests(unittest.TestCase):
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
        # The mock keeps the station in memory; its state dir is only the seed.
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

    def character_path(self):
        return subwave.INSTANCE_DIR / "character" / "character.json"

    def edit_character(self, **fields):
        document = json.loads(self.character_path().read_text())
        document.update(fields)
        self.character_path().write_text(json.dumps(document, indent=2))

    def apply(self):
        return self.run_cli("apply", "--yes", "--force-now", "--no-backup")

    def station_settings(self):
        return self.server.station.settings


class HouseRulesTests(CharacterAppliedTests):
    def test_three_rules_are_joined_with_newlines_on_apply(self):
        rules = ["Speak plainly.", "Never mention being an AI.", "No politics."]
        self.edit_character(houseRules=rules)

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.station_settings()["djHouseRules"], "\n".join(rules))

    def test_pull_splits_a_multi_line_string_into_rules(self):
        self.server.station.settings["djHouseRules"] = "one\ntwo\nthree"
        code, output = self.run_cli("pull", "--force")
        self.assertEqual(code, 0, output)

        character = json.loads(self.character_path().read_text())
        self.assertEqual(character["houseRules"], ["one", "two", "three"],
                         "a 3-line string is 3 rules, not one blob")

    def test_a_three_rule_list_round_trips_losslessly(self):
        rules = ["Speak plainly.", "Never mention being an AI.", "No politics."]
        self.edit_character(houseRules=rules)
        self.assertEqual(self.apply()[0], 0)

        self.assertEqual(self.run_cli("pull", "--force")[0], 0)
        character = json.loads(self.character_path().read_text())
        self.assertEqual(character["houseRules"], rules)


class IdentityTests(CharacterAppliedTests):
    def test_pull_removes_identity_from_settings_json(self):
        self.assertEqual(self.run_cli("pull", "--force")[0], 0)
        settings = json.loads((subwave.INSTANCE_DIR / "settings.json").read_text())
        for key in subwave.IDENTITY_KEYS:
            self.assertNotIn(key, settings,
                             "%s must live in character.json only" % key)
        character = json.loads(self.character_path().read_text())
        self.assertIn("name", character)

    def test_editing_the_station_name_reaches_the_station(self):
        self.edit_character(name="Harbour After Dark")
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.station_settings()["station"], "Harbour After Dark")

    def test_editing_the_theme_string_reaches_the_station(self):
        self.edit_character(theme="phosphor")
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.station_settings()["theme"], {"active": "phosphor"})


class PostApplyCheckTests(CharacterAppliedTests):
    def test_a_field_that_does_not_land_fails_loudly_and_names_it(self):
        self.edit_character(name="Harbour After Dark")

        # A station that accepts the write and silently keeps the old value: the
        # exact failure mode this check exists for.
        original = subwave.read_state_file

        def stale(config, name):
            if name == "settings.json":
                settings = dict(self.server.station.settings)
                settings["station"] = "Harbour Lights"
                return json.dumps(settings)
            return original(config, name)

        subwave.read_state_file = stale
        code, output = self.apply()
        self.assertNotEqual(code, 0, "a silent no-op must not exit 0")
        self.assertIn("did NOT land", output)
        self.assertIn("station", output)

    def test_a_successful_apply_does_not_report_problems(self):
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertNotIn("did NOT land", output)

    def test_a_failed_verification_is_retried_not_reported_as_no_change(self):
        """The failure that matters: a retry must re-send, not claim success.

        lock["applied"] is recorded before verification runs. If the hash alone
        gated the no-op, the apply that just failed verification would record the
        hash of the payload that did not land, and the next run would short-
        circuit with "no changes" and exit 0, leaving the station wrong.
        """
        self.edit_character(name="Harbour After Dark")
        original = subwave.read_state_file

        def stale(config, name):
            if name == "settings.json":
                settings = dict(self.server.station.settings)
                settings["station"] = "Harbour Lights"  # the write never landed
                return json.dumps(settings)
            return original(config, name)

        subwave.read_state_file = stale
        self.addCleanup(lambda: setattr(subwave, "read_state_file", original))

        code, output = self.apply()
        self.assertNotEqual(code, 0)
        self.assertIn("did NOT land", output)

        before = len(self.server.station.requests)
        code, output = self.apply()
        self.assertIn("did NOT land", output,
                      "the station still does not hold what was sent")
        self.assertNotIn("no changes", output)
        self.assertGreater(len(self.server.station.requests), before,
                           "a retry after a failed verification must re-send")


class SecretVerificationTests(CharacterAppliedTests):
    """A verification step must never be able to print a credential."""

    SECRETS = {
        "privacy.password": "thanks-for-all-the-fish-UNIQUE-MARKER",
        "scrobble.lastfm.apiKey": "ee7d-UNIQUE-MARKER",
        "scrobble.lastfm.apiSecret": "f05a-UNIQUE-MARKER",
        "scrobble.lastfm.sessionKey": "RpBx-UNIQUE-MARKER",
    }

    def seed_secrets(self):
        """Station holds real secrets; the repo holds sentinels, as a pull leaves them."""
        for dotted, value in self.SECRETS.items():
            parts = dotted.split(".")
            node = self.server.station.settings
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

        path = subwave.INSTANCE_DIR / "settings.json"
        document = json.loads(path.read_text())
        for dotted in self.SECRETS:
            parts = dotted.split(".")
            node = document
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = subwave.SENTINEL
        path.write_text(json.dumps(document, indent=2))

    def test_apply_with_secrets_succeeds_and_records_applied(self):
        self.seed_secrets()
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertNotIn("did NOT land", output)

        lock = json.loads((subwave.INSTANCE_DIR / "ids.lock.json").read_text())
        self.assertIn("applied", lock, "a successful write must be recorded")

    def test_stdout_never_contains_a_secret(self):
        self.seed_secrets()
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        for value in self.SECRETS.values():
            self.assertNotIn(value, output, "a verification leaked a credential")

    def test_a_secret_that_differs_is_reported_but_redacted(self):
        self.seed_secrets()
        # The station keeps its own password while we send a different one: the
        # env value wins during rehydration, so the two genuinely disagree.
        frozen = json.loads(json.dumps(self.server.station.settings))
        original = subwave.read_state_file

        def stale(config, name):
            if name == "settings.json":
                return json.dumps(frozen)
            return original(config, name)

        subwave.read_state_file = stale
        os.environ["SUBWAVE_STATION_PASSWORD"] = "a-different-password-UNIQUE-MARKER"
        self.addCleanup(lambda: os.environ.pop("SUBWAVE_STATION_PASSWORD", None))

        code, output = self.apply()
        self.assertNotEqual(code, 0, "a real mismatch must still fail loudly")
        self.assertIn("privacy.password", output)
        self.assertIn("secret", output)
        for value in self.SECRETS.values():
            self.assertNotIn(value, output, "a redacted report leaked the credential")
        self.assertNotIn("a-different-password-UNIQUE-MARKER", output)

    def test_a_non_secret_mismatch_is_still_loud_and_named(self):
        original = subwave.read_state_file

        def stale(config, name):
            if name == "settings.json":
                settings = dict(self.server.station.settings)
                settings["locale"] = "xx-XX"
                return json.dumps(settings)
            return original(config, name)

        subwave.read_state_file = stale
        code, output = self.apply()
        self.assertNotEqual(code, 0)
        self.assertIn("did NOT land", output)
        self.assertIn("locale", output)


class RoundTripTests(CharacterAppliedTests):
    def test_apply_then_pull_then_diff_is_clean(self):
        self.edit_character(houseRules=["Speak plainly.", "No politics."],
                            name="Harbour After Dark")
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.run_cli("pull", "--force")[0], 0)

        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)

    def test_festival_normalisation_does_not_churn(self):
        # The station stores every festival with description and windowDays;
        # sending the short form made diff-after-apply dirty.
        settings = json.loads((subwave.INSTANCE_DIR / "settings.json").read_text())
        settings["festivals"] = [{"month": 1, "day": 1, "name": "New Year", "mood": "celebratory"}]
        (subwave.INSTANCE_DIR / "settings.json").write_text(json.dumps(settings, indent=2))

        self.assertEqual(self.apply()[0], 0)
        stored = self.station_settings()["festivals"][0]
        self.assertEqual(stored["description"], "")
        self.assertEqual(stored["windowDays"], 0)

        self.assertEqual(self.run_cli("pull", "--force")[0], 0)
        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)

    def test_a_festival_key_this_tool_does_not_model_survives_apply(self):
        # The station may know more fields than this copy of the toolkit. A
        # rebuild to exactly six keys deleted them on the next apply.
        settings = json.loads((subwave.INSTANCE_DIR / "settings.json").read_text())
        settings["festivals"] = [{"month": 6, "day": 21, "name": "Midsummer",
                                  "mood": "warm", "art": "sun-wheel"}]
        (subwave.INSTANCE_DIR / "settings.json").write_text(json.dumps(settings, indent=2))

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        stored = self.station_settings()["festivals"][0]
        self.assertEqual(stored["art"], "sun-wheel", "an unknown festival key must survive")
        self.assertEqual(stored["description"], "", "the known keys are still normalised")

    def test_a_theme_object_with_extra_keys_round_trips(self):
        self.server.station.settings["theme"] = {"active": "classic-dark", "palette": "dusk"}
        self.assertEqual(self.run_cli("pull", "--force")[0], 0)

        character = json.loads(self.character_path().read_text())
        self.assertEqual(character["theme"], {"active": "classic-dark", "palette": "dusk"},
                         "the theme object is kept whole, not cut to its id")

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.station_settings()["theme"],
                         {"active": "classic-dark", "palette": "dusk"},
                         "apply must send the extra key back, not delete it")


if __name__ == "__main__":
    unittest.main()
