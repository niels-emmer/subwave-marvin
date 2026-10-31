"""Tests for `character edit-persona` and `character edit-show`.

The promise of an edit is narrower than authoring a new entity: only the fields
named change, every other field keeps its value *and* its position, the id and
the slug are untouched, and the result validates and produces a coherent apply
plan. Everything runs in-process against the mock; nothing touches a station.
"""

import json
import os
import sys
import unittest
from pathlib import Path

# Both invocation styles must work: `discover -s tests` puts this directory on
# sys.path, a dotted `unittest tests.test_edit` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_character import CharacterTestCase
from test_subwave import load_tool

subwave = load_tool()


class EditTestCase(CharacterTestCase):
    def setUp(self):
        super().setUp()
        code, output = self.run_cli("character", "init", "--from", "demo")
        self.assertEqual(code, 0, output)

    # -- helpers -----------------------------------------------------------
    def read(self, kind, slug):
        return json.loads((self.root() / "character" / kind / ("%s.json" % slug)).read_text())

    def persona_path(self, slug="ivo-marsh"):
        return self.root() / "character" / "personas" / ("%s.json" % slug)

    def show_path(self, slug="first-light"):
        return self.root() / "character" / "shows" / ("%s.json" % slug)

    def lock(self):
        return json.loads((self.root() / "ids.lock.json").read_text())

    def assert_untouched(self, before, after, named):
        """Every field not named keeps its exact value, and no key moved."""
        self.assertEqual(set(after), set(before), "no key may be added or dropped")
        self.assertEqual(list(after), list(before), "key order must be preserved")
        for field in set(before) - set(named):
            self.assertEqual(after[field], before[field], "unnamed field %r changed" % field)


class PersonaEditTests(EditTestCase):
    def test_edit_persona_changes_only_the_named_field(self):
        before = self.read("personas", "ivo-marsh")
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "warmth=8")
        self.assertEqual(code, 0, output)
        self.assertIn("warmth", output)

        after = self.read("personas", "ivo-marsh")
        self.assertEqual(after["warmth"], 8)
        self.assertIsInstance(after["warmth"], int)
        self.assert_untouched(before, after, {"warmth"})
        self.assert_valid()

    def test_renaming_keeps_the_id_the_slug_and_the_lockfile(self):
        before = self.read("personas", "ivo-marsh")
        lock_before = self.lock()

        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "name=Ivo Marsh Jr")
        self.assertEqual(code, 0, output)

        after = self.read("personas", "ivo-marsh")
        self.assertEqual(after["name"], "Ivo Marsh Jr")
        self.assertEqual(after["id"], before["id"], "a rename must not change the id")
        self.assertTrue(self.persona_path("ivo-marsh").is_file(),
                        "a rename must not rename the slug file")
        self.assertEqual(self.lock(), lock_before, "a rename must not touch ids.lock.json")
        self.assertEqual(len(self.lock()["personas"]), len(lock_before["personas"]),
                         "a rename must not add a lockfile entry")
        self.assert_valid()

    def test_edit_persona_produces_a_coherent_dry_run_naming_the_persona(self):
        self.run_cli("character", "edit-persona", "ivo-marsh", "--set", "warmth=8")
        sent_before = len(self.server.station.requests)

        code, output = self.run_cli("apply")
        self.assertEqual(code, 0, output)
        self.assertIn("dry run", output)
        self.assertIn("0 requests sent", output)
        self.assertIn("Ivo Marsh", output, "the plan must name the edited persona")
        self.assertEqual(len(self.server.station.requests), sent_before)

    def test_editing_needs_no_station(self):
        # Pure local file editing: no base URL, no credentials, no requests.
        for key in ("SUBWAVE_BASE_URL", "SUBWAVE_ADMIN_USER", "SUBWAVE_ADMIN_PASS"):
            os.environ.pop(key, None)
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "warmth=9")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read("personas", "ivo-marsh")["warmth"], 9)
        self.assertEqual(len(self.server.station.requests), 0)


class ShowEditTests(EditTestCase):
    def test_edit_show_flips_only_the_named_booleans(self):
        before = self.read("shows", "first-light")
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "banter=true", "--set", "pauseTalk=false")
        self.assertEqual(code, 0, output)

        after = self.read("shows", "first-light")
        self.assertIs(after["banter"], True)
        self.assertIs(after["pauseTalk"], False)
        self.assert_untouched(before, after, {"banter", "pauseTalk"})
        self.assert_valid()

    def test_edit_show_produces_a_coherent_dry_run_naming_the_show(self):
        self.run_cli("character", "edit-show", "first-light", "--set", "banter=true")
        code, output = self.run_cli("apply")
        self.assertEqual(code, 0, output)
        self.assertIn("dry run", output)
        self.assertIn("show first-light", output)
        self.assertIn("26 fields", output)
        self.assertIn("0 requests sent", output)

    def test_list_fields_take_comma_separated_values(self):
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "genres=soul,jazz")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read("shows", "first-light")["genres"], ["soul", "jazz"])


class TypeTests(EditTestCase):
    def test_every_editable_field_has_a_type(self):
        self.assertEqual(set(subwave.PERSONA_TYPES), set(subwave.PERSONA_FIELDS) - {"id"})
        self.assertEqual(set(subwave.SHOW_TYPES), set(subwave.SHOW_FIELDS) - {"id"})

    def test_integer_fields_reject_non_integers(self):
        for bad in ("warm", "6.5", ""):
            before = self.persona_path().read_text()
            code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                        "--set", "warmth=%s" % bad)
            self.assertEqual(code, 1, output)
            self.assertIn("integer", output)
            self.assertEqual(self.persona_path().read_text(), before,
                             "a refusal must not write")

    def test_enum_fields_reject_values_outside_the_vocabulary(self):
        before = self.persona_path().read_text()
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "frequency=loud")
        self.assertEqual(code, 1, output)
        self.assertIn("must be one of", output)
        self.assertIn("chatty", output)
        self.assertIn("moderate", output)
        self.assertEqual(self.persona_path().read_text(), before)

        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "frequency=moderate")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read("personas", "ivo-marsh")["frequency"], "moderate")

    def test_full_persona_vocabularies_are_accepted(self):
        for field, value in (("frequency", "aggressive"), ("scriptLength", "extended"),
                             ("linkStyle", "announce")):
            code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                        "--set", "%s=%s" % (field, value))
            self.assertEqual(code, 0, "%s=%s: %s" % (field, value, output))
            self.assertEqual(self.read("personas", "ivo-marsh")[field], value)

    def test_the_ladder_order_is_preserved(self):
        # Sourced from upstream controller/src/schemas/persona.ts and show.ts.
        # Order matters: settings/persona.ts bumps one rung with indexOf().
        self.assertEqual(subwave.PERSONA_ENUMS["frequency"],
                         ("silent", "quiet", "moderate", "chatty", "aggressive"))
        self.assertEqual(subwave.PERSONA_ENUMS["scriptLength"],
                         ("one-liner", "concise", "extended", "storyteller"))
        self.assertEqual(subwave.PERSONA_ENUMS["linkStyle"], ("natural", "announce"))
        self.assertEqual(subwave.SHOW_ENUMS["energies"], ("low", "medium", "high"))
        self.assertEqual(subwave.SHOW_ENUMS["vocals"], ("", "instrumental", "vocal"))
        self.assertNotIn("moods", subwave.SHOW_ENUMS,
                         "moods is station-supplied, not a fixed enum")

    def test_moods_are_free_form_because_the_station_supplies_them(self):
        # Upstream validates moods against the station's installed mood names
        # (showStringList values: ctx.moodNames), which this toolkit cannot read,
        # so an unknown mood must be accepted rather than blocked.
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "moods=night,sleepy")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read("shows", "first-light")["moods"], ["night", "sleepy"])

    def test_vocals_accepts_only_the_station_set(self):
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "vocals=shouty")
        self.assertEqual(code, 1, output)
        self.assertIn("must be one of", output)
        self.assertIn("instrumental", output)

        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "vocals=instrumental")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read("shows", "first-light")["vocals"], "instrumental")

    def test_energy_vocabulary_is_enforced(self):
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "energies=frantic")
        self.assertEqual(code, 1, output)
        self.assertIn("must be one of", output)
        self.assertIn("medium", output)

    def test_boolean_fields_accept_only_true_or_false(self):
        for bad in ("yes", "1", "on"):
            before = self.show_path().read_text()
            code, output = self.run_cli("character", "edit-show", "first-light",
                                        "--set", "banter=%s" % bad)
            self.assertEqual(code, 1, output)
            self.assertIn("boolean", output)
            self.assertEqual(self.show_path().read_text(), before)


class RefusalTests(EditTestCase):
    def test_a_typo_is_still_rejected_with_the_allowed_set(self):
        before = self.persona_path().read_text()
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "frequency=shouty")
        self.assertEqual(code, 1, output)
        self.assertIn("must be one of", output)
        for value in ("silent", "quiet", "moderate", "chatty", "aggressive"):
            self.assertIn(value, output)
        self.assertEqual(self.persona_path().read_text(), before)

    def test_force_value_allows_an_unknown_value_with_a_warning(self):
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "frequency=shouty", "--force-value")
        self.assertEqual(code, 0, output)
        self.assertIn("warning", output)
        self.assertIn("outside the known vocabulary", output)
        self.assertIn("shouty", output)
        self.assertEqual(self.read("personas", "ivo-marsh")["frequency"], "shouty")
        self.assert_valid()

    def test_force_value_also_covers_enum_lists(self):
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "energies=frantic", "--force-value")
        self.assertEqual(code, 0, output)
        self.assertIn("warning", output)
        self.assertEqual(self.read("shows", "first-light")["energies"], ["frantic"])

    def test_force_value_does_not_relax_booleans_or_integers(self):
        code, output = self.run_cli("character", "edit-show", "first-light",
                                    "--set", "banter=yes", "--force-value")
        self.assertEqual(code, 1, output)
        self.assertIn("boolean", output)

        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "warmth=warm", "--force-value")
        self.assertEqual(code, 1, output)
        self.assertIn("integer", output)

    def test_unknown_field_is_rejected_with_the_fields_that_exist(self):
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "warmthh=8")
        self.assertEqual(code, 1, output)
        self.assertIn("unknown persona field", output)
        self.assertIn("warmthh", output)
        self.assertIn("warmth", output, "the refusal must list the fields that do exist")
        self.assertIn("soul", output)

    def test_id_is_immutable(self):
        before = self.persona_path().read_text()
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "id=p_somebody_else")
        self.assertEqual(code, 1, output)
        self.assertIn("immutable", output)
        self.assertEqual(self.persona_path().read_text(), before)

    def test_unknown_slug_lists_the_known_ones(self):
        code, output = self.run_cli("character", "edit-persona", "nobody")
        self.assertEqual(code, 1, output)
        self.assertIn("ivo-marsh", output)

    def test_at_least_one_set_is_required(self):
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh")
        self.assertEqual(code, 1, output)
        self.assertIn("nothing to change", output)

    def test_a_malformed_assignment_is_rejected(self):
        code, output = self.run_cli("character", "edit-persona", "ivo-marsh",
                                    "--set", "warmth")
        self.assertEqual(code, 1, output)
        self.assertIn("FIELD=VALUE", output)


if __name__ == "__main__":
    unittest.main()
