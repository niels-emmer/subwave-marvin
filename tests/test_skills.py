"""Station skills: the DJ's real-world segments, authored as files.

NOT the repo's `skills/` at the root — those are AGENT procedures. These live in
`instance/skills/<slug>/SKILL.md` and are operator content, like personas.
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


class SkillsTestCase(unittest.TestCase):
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
        # the image's built-in list, without needing a container
        subwave.builtin_skill_slugs = lambda config, state: {"curiosity"}
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
        (subwave.INSTANCE_DIR, subwave.SshStateSource, subwave.read_state_file,
         subwave.detect_tier, subwave.builtin_skill_slugs) = self._saved

    # -- helpers -----------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = subwave.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def skill_path(self, slug):
        return subwave.INSTANCE_DIR / "skills" / slug / "SKILL.md"

    def pull(self, *extra):
        return self.run_cli("pull", "--force", *extra)

    def apply(self):
        return self.run_cli("apply", "--yes", "--force-now", "--no-backup")


class PullTests(SkillsTestCase):
    def test_a_custom_skill_pulls_intact(self):
        code, output = self.pull()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.skill_path("demo-skill").is_file())

        data, body = subwave.parse_skill_md(self.skill_path("demo-skill").read_text())
        self.assertEqual(data["name"], "demo-skill")
        self.assertEqual(data["cooldown"], "4h")
        self.assertIn("fictional segment", body)

    def test_a_builtin_is_not_captured(self):
        code, output = self.pull()
        self.assertEqual(code, 0, output)
        self.assertFalse(self.skill_path("curiosity").exists(),
                         "built-ins are the image's content, not the operator's")
        self.assertIn("built-in not captured", output)

    def test_tier_2_pull_needs_no_admin_credentials(self):
        os.environ.pop("SUBWAVE_ADMIN_USER", None)
        os.environ.pop("SUBWAVE_ADMIN_PASS", None)
        code, output = self.pull()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.skill_path("demo-skill").is_file())


class ValidateTests(SkillsTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def write(self, slug, text):
        path = self.skill_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_a_valid_skill_passes(self):
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)

    def test_a_bad_slug_is_rejected(self):
        self.write("Bad Slug", "---\nname: Bad Slug\nlabel: x\ncooldown: 1h\n---\n\nbody\n")
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("not a valid skill slug", output)

    def test_missing_frontmatter_is_an_error(self):
        self.write("no-front", "# just a heading\n\nbody\n")
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("frontmatter", output)

    def test_an_empty_body_is_rejected(self):
        self.write("empty-body", "---\nname: empty-body\nlabel: x\ncooldown: 1h\n---\n\n")
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("body is empty", output)

    def test_a_bad_cooldown_is_rejected(self):
        self.write("bad-cd", "---\nname: bad-cd\nlabel: x\ncooldown: soon\n---\n\nbody\n")
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("cooldown", output)

    def test_a_name_not_matching_the_directory_is_rejected(self):
        self.write("mismatch", "---\nname: other\nlabel: x\ncooldown: 1h\n---\n\nbody\n")
        code, output = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("does not match the directory", output)


class ApplyTests(SkillsTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_a_new_skill_is_created(self):
        code, output = self.run_cli("skills", "new", "weather-watch")
        self.assertEqual(code, 0, output)

        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("weather-watch", self.server.station.skills)

    def test_an_existing_skill_is_updated_not_duplicated(self):
        path = self.skill_path("demo-skill")
        code, output = self.run_cli("skills", "edit", "demo-skill",
                                    "--brief", "A rewritten segment.")
        self.assertEqual(code, 0, output)

        before = len(self.server.station.requests)
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.server.station.skills["demo-skill"]["brief"],
                         "A rewritten segment.")

        puts = [entry for entry in self.server.station.requests[before:]
                if entry["method"] == "PUT" and entry["path"].endswith("/file")]
        self.assertTrue(puts, "an existing skill is a PUT")
        posts = [entry for entry in self.server.station.requests[before:]
                 if entry["method"] == "POST" and entry["path"] == "/api/dj/skills"]
        self.assertEqual(posts, [], "an existing skill must not be POSTed again")

    def test_apply_never_deletes_a_skill(self):
        self.run_cli("skills", "rm", "demo-skill")
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("demo-skill", self.server.station.skills,
                      "apply does not delete station skills")
        deletes = [entry for entry in self.server.station.requests
                   if entry["method"] == "DELETE"]
        self.assertEqual(deletes, [])


class DiffTests(SkillsTestCase):
    def setUp(self):
        super().setUp()
        self.pull()

    def test_a_changed_prompt_is_reported(self):
        self.run_cli("skills", "edit", "demo-skill", "--brief", "Something else entirely.")
        code, output = self.run_cli("diff")
        self.assertEqual(code, 1)
        self.assertIn("skills", output)
        self.assertIn("demo-skill", output)

    def test_an_unchanged_skill_set_is_clean(self):
        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)


class CommandTests(SkillsTestCase):
    def test_new_refuses_a_bad_slug(self):
        code, output = self.run_cli("skills", "new", "Bad Slug")
        self.assertNotEqual(code, 0)
        self.assertIn("not a valid skill slug", output)

    def test_show_prints_the_file(self):
        self.pull()
        code, output = self.run_cli("skills", "show", "demo-skill")
        self.assertEqual(code, 0, output)
        self.assertIn("cooldown: 4h", output)


if __name__ == "__main__":
    unittest.main()


class DiffTierTests(SkillsTestCase):
    """diff must compare at the tier it detected, and never call a built-in removed."""

    def test_untouched_capture_diffs_clean_at_tier_2(self):
        self.assertEqual(self.pull()[0], 0)
        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)
        self.assertIn("skills    1 unchanged, 0 changed, 0 added, 0 removed", output)

    def test_untouched_capture_diffs_clean_at_tier_1(self):
        subwave.detect_tier = lambda config, probe=None: (1, "mock")
        code, output = self.run_cli("pull", "--allow-lossy", "--force")
        self.assertEqual(code, 0, output)
        code, output = self.run_cli("diff")
        self.assertEqual(code, 0, output)
        self.assertIn("in sync", output)

    def test_a_builtin_is_never_reported_as_removed(self):
        self.pull()
        _, output = self.run_cli("diff")
        self.assertNotIn("curiosity", output,
                         "a built-in is not captured, so it cannot be missing")

    def test_a_real_edit_shows_as_changed(self):
        self.pull()
        self.run_cli("skills", "edit", "demo-skill", "--brief", "A different segment.")
        code, output = self.run_cli("diff")
        self.assertEqual(code, 1)
        self.assertIn("changed: demo-skill", output)


class MoodValidationTests(SkillsTestCase):
    def setUp(self):
        super().setUp()
        # the vocabulary the fixture's shows already use, plus room to add one
        self.server.station.settings["moods"] = [
            {"name": "calm"}, {"name": "night"}, {"name": "reflective"},
            {"name": "morning"}, {"name": "sunny"}]
        self.pull()

    def write_show(self, moods, genres=None):
        path = subwave.INSTANCE_DIR / "character" / "shows" / "first-light.json"
        document = json.loads(path.read_text())
        document["moods"] = moods
        if genres is not None:
            document["genres"] = genres
        path.write_text(json.dumps(document, indent=2))

    def test_an_unknown_mood_warns_at_tier_2(self):
        self.write_show(["calm", "nonexistent"])
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, "an unknown mood is a warning, not an error")
        self.assertIn("warning", output)
        self.assertIn("nonexistent", output)
        self.assertIn("first-light.json", output)

    def test_a_known_mood_is_silent(self):
        self.write_show(["calm", "night"])
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("0 warning(s)", output)

    def test_no_mood_check_without_station_access(self):
        subwave.detect_tier = lambda config, probe=None: (1, "mock")
        self.write_show(["nonexistent"])
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertNotIn("nonexistent", output, "tier 1 says nothing rather than guessing")

    def test_a_genre_is_never_checked(self):
        self.write_show(["calm"], genres=["A Made Up Genre That Does Not Exist"])
        code, output = self.run_cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("0 warning(s)", output, "genres are tags, not an enum")
        self.assertNotIn("Made Up Genre", output)
