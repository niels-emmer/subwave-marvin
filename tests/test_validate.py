"""Tests for `validate` and its wiring into `apply`.

Seeds a valid instance/ from the fixture, then breaks one thing at a time.
"""

import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Both invocation styles must work: `discover -s tests` puts this directory on
# sys.path, a dotted `unittest tests.test_validate` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_subwave import load_tool

subwave = load_tool()
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "station.json"


class ValidateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = subwave.INSTANCE_DIR
        subwave.INSTANCE_DIR = self.tmp / "instance"
        self.addCleanup(lambda: setattr(subwave, "INSTANCE_DIR", self._saved))

        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        model = subwave.build_live_model(document["settings"], document["schedule"])
        subwave.write_instance(model, {}, capture={
            "tier": 2, "at": "2026-10-31T00:00:00Z", "lossy": False, "lost": []})
        self.root = subwave.INSTANCE_DIR

    # -- helpers -----------------------------------------------------------
    def path(self, *parts):
        return self.root.joinpath(*parts)

    def edit(self, *parts, mutate=None):
        path = self.path(*parts)
        document = json.loads(path.read_text())
        mutate(document)
        path.write_text(json.dumps(document, indent=2))

    def run_validate(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = subwave.cmd_validate(None, argparse.Namespace())
        return code, buffer.getvalue()

    def show_file(self):
        return sorted((self.root / "character" / "shows").glob("*.json"))[0]

    def persona_file(self):
        return sorted((self.root / "character" / "personas").glob("*.json"))[0]


class CleanTests(ValidateTestCase):
    def test_a_valid_instance_passes(self):
        code, output = self.run_validate()
        self.assertEqual(code, 0)
        self.assertIn("ok", output)
        self.assertIn("168/168", output)


class FieldTests(ValidateTestCase):
    def test_missing_persona_field_is_an_error_naming_file_and_field(self):
        path = self.persona_file()
        self.edit("character", "personas", path.name, mutate=lambda d: d.pop("soul"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn(path.name, output)
        self.assertIn("'soul'", output)

    def test_missing_show_field_is_an_error(self):
        path = self.show_file()
        self.edit("character", "shows", path.name, mutate=lambda d: d.pop("genres"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("'genres'", output)

    def test_unknown_field_is_only_a_warning(self):
        path = self.show_file()
        self.edit("character", "shows", path.name,
                  mutate=lambda d: d.update({"someFutureField": True}))
        code, output = self.run_validate()
        self.assertEqual(code, 0, "unknown fields must not fail validation")
        self.assertIn("warning", output)
        self.assertIn("someFutureField", output)

    def test_unknown_character_field_is_a_warning(self):
        self.edit("character", "character.json", mutate=lambda d: d.update({"mascot": "a gull"}))
        code, output = self.run_validate()
        self.assertEqual(code, 0)
        self.assertIn("mascot", output)

    def test_character_requires_identity_keys(self):
        self.edit("character", "character.json", mutate=lambda d: d.pop("locale"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("'locale'", output)

    def test_character_rejects_a_wrong_type(self):
        self.edit("character", "character.json", mutate=lambda d: d.update({"houseRules": "not a list"}))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("houseRules", output)


class GridTests(ValidateTestCase):
    def test_grid_must_have_seven_days(self):
        self.edit("character", "schedule.json",
                  mutate=lambda d: d["schedule"].pop("6"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("0..6", output)

    def test_day_must_have_24_cells(self):
        self.edit("character", "schedule.json",
                  mutate=lambda d: d["schedule"].__setitem__("2", d["schedule"]["2"][:23]))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("24 cells", output)

    def test_cell_must_name_a_show_file(self):
        def mutate(document):
            document["schedule"]["3"][5] = "no-such-show"
        self.edit("character", "schedule.json", mutate=mutate)
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("no-such-show", output)

    def test_empty_cell_is_an_error(self):
        def mutate(document):
            document["schedule"]["3"][5] = None
        self.edit("character", "schedule.json", mutate=mutate)
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("empty", output)


class LockTests(ValidateTestCase):
    def test_slug_without_a_lock_entry_is_an_error(self):
        self.edit("ids.lock.json", mutate=lambda d: d["shows"].pop("late-harbour"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("late-harbour", output)
        self.assertIn("no lock entry", output)

    def test_malformed_id_is_an_error(self):
        self.edit("ids.lock.json",
                  mutate=lambda d: d["shows"].__setitem__("late-harbour", "banana"))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("malformed", output)

    def test_duplicate_ids_are_an_error(self):
        def mutate(document):
            first = document["shows"]["late-harbour"]
            document["shows"]["first-light"] = first
        self.edit("ids.lock.json", mutate=mutate)
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("used by both", output)

    def test_lock_and_file_disagreeing_is_an_error(self):
        def mutate(document):
            document["shows"]["late-harbour"] = "s_different1"
        self.edit("ids.lock.json", mutate=mutate)
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("its file says", output)


class NewEntityTests(ValidateTestCase):
    def test_a_show_file_with_no_id_is_a_new_entity_not_an_error(self):
        path = self.path("character", "shows", "brand-new.json")
        document = json.loads(self.show_file().read_text())
        document.pop("id", None)
        document["name"] = "Brand New"
        path.write_text(json.dumps(document, indent=2))
        code, output = self.run_validate()
        self.assertEqual(code, 0, output)
        self.assertNotIn("error", output)


class CaptureTests(ValidateTestCase):
    def test_lossy_capture_is_a_hard_failure(self):
        self.edit("ids.lock.json", mutate=lambda d: d.__setitem__(
            "capture", {"tier": 1, "at": "2026-10-31T00:00:00Z",
                        "lossy": True, "lost": ["19 of 26 show fields"]}))
        code, output = self.run_validate()
        self.assertEqual(code, 1)
        self.assertIn("lossy", output)

    def test_unreadable_input_exits_2(self):
        self.path("character", "schedule.json").write_text("{not json")
        code, output = self.run_validate()
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", output)

    def test_missing_instance_exits_2(self):
        shutil.rmtree(self.root)
        code, output = self.run_validate()
        self.assertEqual(code, 2)


class ApplyRefusalTests(ValidateTestCase):
    def test_apply_refuses_when_validate_finds_errors(self):
        path = self.show_file()
        self.edit("character", "shows", path.name, mutate=lambda d: d.pop("genres"))
        args = argparse.Namespace(yes=True, force_now=True, no_backup=True,
                                  prune=False, backup_dir=None)
        config = subwave.Config(dotenv={}, environ={"SUBWAVE_BASE_URL": "http://127.0.0.1:1"})
        with self.assertRaises(subwave.ToolError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                subwave.cmd_apply(config, args)
        self.assertIn("validate", str(caught.exception))
        self.assertIn("'genres'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
