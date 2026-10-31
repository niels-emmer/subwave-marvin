"""`apply --prune` — the only destructive operation in the toolkit.

The station's delete routes have no undo: a re-added rule gets a new uuid, and a
deleted persona's shows lose their host. So a cascade is never silent: refuse by
default when something depends on the entity, and require --force-prune.
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


class PruneTestCase(unittest.TestCase):
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
                       subwave.read_state_file, subwave.detect_tier)
        subwave.INSTANCE_DIR = self.tmp / "instance"
        subwave.SshStateSource = lambda config: subwave.LocalStateSource(
            self.server.state_dir)
        subwave.read_state_file = lambda config, name: json.dumps(
            self.server.station.settings if name == "settings.json"
            else self.server.station.schedule)
        subwave.detect_tier = lambda config, probe=None: (2, "mock")
        self.addCleanup(self._restore)

        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        model = subwave.build_live_model(document["settings"], document["schedule"])
        subwave.write_instance(model, {}, capture={
            "tier": 2, "at": "2026-10-31T00:00:00Z", "lossy": False, "lost": []})
        blocklist = json.loads(json.dumps(self.server.station.blocklist))
        subwave.assign_rule_slugs(blocklist["rules"], {})
        subwave.write_json(subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE, blocklist)
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
         subwave.read_state_file, subwave.detect_tier) = self._saved

    # -- helpers -----------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = subwave.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def apply(self, *extra):
        return self.run_cli("apply", "--yes", "--force-now", "--prune", *extra)

    def show_ids(self):
        return [show["id"] for show in self.server.station.schedule["shows"]]

    def persona_ids(self):
        return [persona["id"] for persona in self.server.station.settings["personas"]]

    def deletes(self):
        return [entry for entry in self.server.station.requests
                if entry["method"] == "DELETE"]

    def add_station_show(self, show_id="s_orphan1", name="Orphan Show", in_grid=False):
        show = {"id": show_id, "name": name, "personaId": self.persona_ids()[0],
                "topic": "", "guestPersonaIds": [], "moods": [], "themeId": ""}
        self.server.station.schedule["shows"].append(show)
        if in_grid:
            self.server.station.schedule["schedule"]["2"][4] = show_id
        return show

    def add_station_persona(self, persona_id="p_orphan1", name="Orphan Host"):
        persona = {"id": persona_id, "name": name, "tagline": ""}
        self.server.station.settings["personas"].append(persona)
        return persona


class DryRunTests(PruneTestCase):
    def test_dry_run_deletes_nothing(self):
        self.add_station_show()
        before = self.show_ids()
        code, output = self.run_cli("apply", "--prune")
        self.assertEqual(code, 0, output)
        self.assertIn("delete", output)
        self.assertIn("Orphan Show", output)
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.show_ids(), before)


class ShowTests(PruneTestCase):
    def test_a_show_in_no_grid_cell_is_deleted(self):
        self.add_station_show()
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("deleted   show 'Orphan Show'", output)
        self.assertNotIn("s_orphan1", self.show_ids())

    def test_a_show_occupying_cells_is_refused_naming_day_and_hour(self):
        self.add_station_show(in_grid=True)
        code, output = self.apply()
        self.assertNotEqual(code, 0)
        self.assertIn("occupies 1 grid cell(s)", output)
        self.assertIn("tue 04:00", output)
        self.assertEqual(self.deletes(), [], "a refusal must delete nothing")
        self.assertIn("s_orphan1", self.show_ids())

    def test_force_prune_deletes_it_and_clears_the_cells(self):
        self.add_station_show(in_grid=True)
        code, output = self.apply("--force-prune")
        self.assertEqual(code, 0, output)
        self.assertIn("deleted   show 'Orphan Show'", output)
        self.assertNotIn("s_orphan1", self.show_ids())

    def test_clearing_cells_empties_a_repo_grid_that_pointed_at_it(self):
        # Unit-level: the repo grid can still name a show that was pruned.
        path = subwave.INSTANCE_DIR / "character" / "schedule.json"
        document = json.loads(path.read_text())
        document["schedule"]["2"][4] = "orphan-show"
        path.write_text(json.dumps(document))

        cleared = subwave.clear_show_cells(["s_orphan1"],
                                           {"shows": {"orphan-show": "s_orphan1"}})
        self.assertEqual(cleared, 1)
        after = json.loads(path.read_text())
        self.assertIsNone(after["schedule"]["2"][4])

    def test_the_last_show_is_refused_with_no_override(self):
        for show_id in self.show_ids():
            self.server.station.schedule["shows"] = [
                show for show in self.server.station.schedule["shows"]
                if show["id"] != show_id]
        self.add_station_show("s_only1", "Only Show")
        self.server.station.schedule["schedule"] = {
            str(day): ["s_only1"] * 24 for day in range(7)}

        code, output = self.apply("--force-prune")
        self.assertNotEqual(code, 0, "--force-prune must not override this")
        self.assertIn("last show", output)
        self.assertEqual(self.deletes(), [])


class PersonaTests(PruneTestCase):
    def test_a_persona_hosting_a_show_is_refused_naming_it(self):
        self.add_station_persona()
        self.server.station.schedule["shows"][0]["personaId"] = "p_orphan1"

        code, output = self.apply()
        self.assertNotEqual(code, 0)
        self.assertIn("hosts 1 show(s)", output)
        self.assertEqual(self.deletes(), [])

    def test_the_active_persona_is_refused(self):
        self.add_station_persona("p_orphan1", "Active Orphan")
        self.server.station.settings["activePersonaId"] = "p_orphan1"

        code, output = self.apply("--force-prune")
        self.assertNotEqual(code, 0)
        self.assertIn("active persona", output)
        self.assertEqual(self.deletes(), [])

    def test_the_last_persona_is_refused(self):
        # The station holds exactly one persona and it is not in the repo.
        self.server.station.settings["personas"] = [
            {"id": "p_orphan1", "name": "Only Host", "tagline": ""}]
        self.server.station.settings["activePersonaId"] = None
        self.server.station.schedule["shows"] = []

        code, output = self.apply("--force-prune")
        self.assertNotEqual(code, 0)
        self.assertIn("every persona", output)
        self.assertEqual(self.deletes(), [])


class BackupTests(PruneTestCase):
    def test_prune_with_no_backup_is_rejected(self):
        code, output = self.run_cli("apply", "--yes", "--force-now", "--prune",
                                    "--no-backup")
        self.assertNotEqual(code, 0)
        self.assertIn("backup is mandatory", output)
        self.assertEqual(self.deletes(), [])


class BlocklistTests(PruneTestCase):
    def test_a_pending_rule_is_deleted_only_under_prune(self):
        # Remove the rule from the local file; the lock still knows its uuid.
        path = subwave.INSTANCE_DIR / subwave.BLOCKLIST_FILE
        document = json.loads(path.read_text())
        slug = document["rules"][0]["slug"]
        document["rules"] = []
        path.write_text(json.dumps(document))

        code, output = self.run_cli("apply", "--yes", "--force-now", "--no-backup")
        self.assertEqual(code, 0, output)
        self.assertIn("pending", output)
        self.assertEqual(self.deletes(), [], "a plain apply never deletes")

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("blocklist rule", output)
        deletes = self.deletes()
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0]["path"].startswith("/api/library/blocklist/rules/"))
        self.assertNotIn(slug, json.dumps(self.server.station.blocklist))


class OrderingTests(PruneTestCase):
    def test_a_failed_verification_leaves_the_station_untouched(self):
        self.add_station_show()
        original = subwave.read_state_file

        def stale(config, name):
            if name == "settings.json":
                settings = dict(self.server.station.settings)
                settings["locale"] = "xx-XX"  # never lands
                return json.dumps(settings)
            return original(config, name)

        subwave.read_state_file = stale
        code, output = self.apply()
        self.assertNotEqual(code, 0)
        self.assertIn("did NOT land", output)
        self.assertEqual(self.deletes(), [], "verification failing must stop prune")
        self.assertIn("s_orphan1", self.show_ids())


if __name__ == "__main__":
    unittest.main()
