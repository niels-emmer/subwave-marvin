"""On-air control through the station's MCP server.

The boundary these tests defend: on-air control is EPHEMERAL. It changes what is
happening now, never what the station is. It must not be reachable from apply,
diff, validate or prune, and nothing it does belongs in git.
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


class OnAirTestCase(unittest.TestCase):
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

    def calls(self):
        return [call for call in self.server.station.mcp_calls]


class ReadTests(OnAirTestCase):
    def test_tools_lists_the_catalogue(self):
        code, output = self.run_cli("onair", "tools")
        self.assertEqual(code, 0, output)
        self.assertIn("subwave_now_playing", output)
        self.assertIn("subwave_dj_announce", output)

    def test_now_and_state_are_read_only(self):
        code, output = self.run_cli("onair", "now")
        self.assertEqual(code, 0, output)
        self.assertIn("Mock Track", output)

        code, output = self.run_cli("onair", "state")
        self.assertEqual(code, 0, output)

        for call in self.calls():
            self.assertNotIn(call["tool"],
                             {"subwave_dj_announce", "subwave_skip_track",
                              "subwave_queue_track", "subwave_request_song"})

    def test_list_needs_no_yes(self):
        code, output = self.run_cli("onair", "list", "skills")
        self.assertEqual(code, 0, output)

    def test_read_tools_need_no_credentials(self):
        os.environ.pop("SUBWAVE_ADMIN_USER", None)
        os.environ.pop("SUBWAVE_ADMIN_PASS", None)
        code, output = self.run_cli("onair", "now")
        self.assertEqual(code, 0, output)


class DryRunTests(OnAirTestCase):
    def test_say_without_yes_sends_nothing_and_names_the_call(self):
        code, output = self.run_cli("onair", "say", "Good evening.")
        self.assertEqual(code, 0, output)
        self.assertIn("subwave_dj_announce", output)
        self.assertIn("Good evening.", output)
        self.assertIn("--yes", output)
        self.assertEqual(self.calls(), [], "nothing may go on air without --yes")

    def test_every_air_verb_is_dry_by_default(self):
        verbs = [
            ("say", "hello"),
            ("segment", "hourly"),
            ("skip", "--any-track"),
            ("request", "something like Massive Attack"),
            ("queue", "portishead"),
            ("sfx", "airhorn"),
            ("jingle", "station-id.mp3"),
        ]
        for verb, *rest in verbs:
            with self.subTest(verb=verb):
                code, output = self.run_cli("onair", verb, *rest)
                self.assertEqual(code, 0, output)
                self.assertIn("dry run", output)
        self.assertEqual(self.calls(), [])

    def test_say_with_yes_sends_exactly_one_call(self):
        code, output = self.run_cli("onair", "say", "Good evening.", "--yes")
        self.assertEqual(code, 0, output)
        calls = [call for call in self.calls() if call["tool"] == "subwave_dj_announce"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"], {"message": "Good evening."})

    def test_flags_become_arguments(self):
        self.run_cli("onair", "say", "Over the top.", "--yes", "--raw", "--over-track")
        call = [c for c in self.calls() if c["tool"] == "subwave_dj_announce"][0]
        self.assertEqual(call["arguments"],
                         {"message": "Over the top.", "mode": "raw", "placement": "over"})


class SkipTests(OnAirTestCase):
    def test_skip_without_expect_id_or_any_track_is_refused(self):
        code, output = self.run_cli("onair", "skip", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("--expect-id", output)
        self.assertIn("--any-track", output)
        self.assertEqual(self.calls(), [], "a refused skip must send nothing")

    def test_skip_with_expect_id_sends_it(self):
        code, output = self.run_cli("onair", "skip", "--yes", "--expect-id", "tr-1")
        self.assertEqual(code, 0, output)
        call = [c for c in self.calls() if c["tool"] == "subwave_skip_track"][0]
        self.assertEqual(call["arguments"], {"expectId": "tr-1"})

    def test_skip_with_any_track_sends_no_expectation(self):
        code, output = self.run_cli("onair", "skip", "--yes", "--any-track")
        self.assertEqual(code, 0, output)
        call = [c for c in self.calls() if c["tool"] == "subwave_skip_track"][0]
        self.assertEqual(call["arguments"], {})


class AuthTests(OnAirTestCase):
    def test_a_401_names_the_missing_credentials(self):
        os.environ.pop("SUBWAVE_ADMIN_USER", None)
        os.environ.pop("SUBWAVE_ADMIN_PASS", None)
        code, output = self.run_cli("onair", "say", "hello", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("SUBWAVE_ADMIN_USER", output)
        self.assertIn("SUBWAVE_ADMIN_PASS", output)


class FlowTests(OnAirTestCase):
    def test_queue_searches_then_queues_the_top_result(self):
        code, output = self.run_cli("onair", "queue", "portishead", "--yes")
        self.assertEqual(code, 0, output)
        tools = [call["tool"] for call in self.calls()]
        self.assertIn("subwave_search_library", tools)
        self.assertIn("subwave_queue_track", tools)
        queued = [c for c in self.calls() if c["tool"] == "subwave_queue_track"][0]
        self.assertEqual(queued["arguments"]["id"], "tr-1")

    def test_queue_pick_selects_another_result(self):
        self.run_cli("onair", "queue", "portishead", "--yes", "--pick", "2")
        queued = [c for c in self.calls() if c["tool"] == "subwave_queue_track"][0]
        self.assertEqual(queued["arguments"]["id"], "tr-2")

    def test_request_polls_the_receipt(self):
        code, output = self.run_cli("onair", "request", "anything by Portishead", "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("req-1", output)
        self.assertIn("queued", output)
        tools = [call["tool"] for call in self.calls()]
        self.assertIn("subwave_request_song", tools)
        self.assertIn("subwave_request_status", tools)


class BoundaryTests(OnAirTestCase):
    """On-air control is ephemeral: the declarative commands never touch it."""

    def test_apply_diff_validate_never_call_mcp(self):
        for command in (("validate",), ("diff",), ("apply", "--yes", "--force-now",
                                                  "--no-backup")):
            with self.subTest(command=command):
                self.run_cli(*command)
        self.assertEqual(self.calls(), [], "the declarative surface must not use MCP")

    def test_onair_writes_nothing_into_instance(self):
        before = {path: path.stat().st_mtime
                  for path in subwave.INSTANCE_DIR.rglob("*") if path.is_file()}
        self.run_cli("onair", "say", "hello", "--yes")
        after = {path: path.stat().st_mtime
                 for path in subwave.INSTANCE_DIR.rglob("*") if path.is_file()}
        self.assertEqual(before, after, "on-air control never writes to the repo")


if __name__ == "__main__":
    unittest.main()
