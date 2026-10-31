"""The tier-2 onboarding path: a bare ssh host is enough to get started.

A newcomer knows their machine's ssh address and nothing else. The container
name and the state dir's path *inside* the container are the two facts that
turn a dead end into tier 2, and both are discoverable from the host. These
tests pin the discovery contract without touching ssh: `ssh_run` is stubbed,
so what is asserted is the parsing, the ordering and what `status` prints.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import mock_station
from test_subwave import load_tool

subwave = load_tool()


class FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class OnboardingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self._saved_instance = subwave.INSTANCE_DIR
        subwave.INSTANCE_DIR = self.tmp / "instance"
        self.addCleanup(lambda: setattr(subwave, "INSTANCE_DIR", self._saved_instance))

        self._saved_ssh_run = subwave.ssh_run
        self.addCleanup(lambda: setattr(subwave, "ssh_run", self._saved_ssh_run))

        self._saved_http_json = subwave.http_json
        self.addCleanup(lambda: setattr(subwave, "http_json", self._saved_http_json))

        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        for key in ("SUBWAVE_HOST", "SUBWAVE_STATE_DIR", "SUBWAVE_CONTAINER"):
            os.environ.pop(key, None)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = subwave.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def offline_api(self):
        """status must not reach the network in these tests."""
        subwave.http_json = lambda *a, **kw: (200, {
            "activeShow": None, "nowPlaying": {}, "listeners": {}, "streamOnline": True})

    def stub_ssh(self, *results):
        """Queue ssh results; the last repeats. A plain string means stdout, rc 0."""
        queue = [r if isinstance(r, FakeResult) else FakeResult(stdout=r) for r in results]
        calls = []

        def fake(config, command, **kwargs):
            calls.append(command)
            return queue[min(len(calls) - 1, len(queue) - 1)]

        subwave.ssh_run = fake
        return calls


class DiscoverStateDirsTests(OnboardingTestCase):
    def config(self, **values):
        return subwave.Config(dotenv={}, environ=values)

    def test_found_lines_are_parsed(self):
        self.stub_ssh("FOUND sub-wave-controller /var/sub-wave\n")
        config = self.config(SUBWAVE_HOST="me@host", SUBWAVE_STATE_DIR="/var/sub-wave")
        self.assertEqual(subwave.discover_state_dirs(config),
                         ([("sub-wave-controller", "/var/sub-wave")], ""))

    def test_the_image_default_container_leads(self):
        # A renamed container must not win over the image default just by
        # appearing first: the default is the likeliest answer for a stranger.
        self.stub_ssh("FOUND subwave-old /srv/state\n"
                      "FOUND sub-wave-controller /var/sub-wave\n")
        config = self.config(SUBWAVE_HOST="me@host", SUBWAVE_STATE_DIR="/var/sub-wave")
        found, note = subwave.discover_state_dirs(config)
        self.assertEqual(found[0], ("sub-wave-controller", "/var/sub-wave"))
        self.assertEqual(len(found), 2)

    def test_noise_from_the_remote_shell_is_ignored(self):
        self.stub_ssh("Pseudo-terminal will not be allocated\n"
                      "FOUND sub-wave-controller /var/sub-wave\n"
                      "docker: command not found\n")
        config = self.config(SUBWAVE_HOST="me@host", SUBWAVE_STATE_DIR="/var/sub-wave")
        self.assertEqual(subwave.discover_state_dirs(config),
                         ([("sub-wave-controller", "/var/sub-wave")], ""))

    def test_an_unreachable_host_yields_nothing_rather_than_raising(self):
        self.stub_ssh(FakeResult("", "ssh: connect to host port 22: refused", 255))
        config = self.config(SUBWAVE_HOST="me@host", SUBWAVE_STATE_DIR="/var/sub-wave")
        found, note = subwave.discover_state_dirs(config)
        self.assertEqual(found, [])
        self.assertIn("connect to host", note, "an unreachable host must say why")


class StatusOnboardingTests(OnboardingTestCase):
    def test_a_bare_host_probes_and_prints_the_two_lines_to_add(self):
        self.offline_api()
        os.environ.update({"SUBWAVE_BASE_URL": "http://127.0.0.1:7700",
                           "SUBWAVE_HOST": "me@host"})
        calls = self.stub_ssh("FOUND sub-wave-controller /var/sub-wave\n")

        code, output = self.run_cli("status")

        self.assertIn("tier      1", output)
        self.assertIn("missing SUBWAVE_STATE_DIR", output)
        self.assertIn("found container sub-wave-controller, data directory /var/sub-wave", output)
        self.assertIn("SUBWAVE_CONTAINER=sub-wave-controller", output)
        self.assertIn("SUBWAVE_STATE_DIR=/var/sub-wave", output)
        self.assertEqual(len(calls), 1, "discovery is one ssh round trip, not one per container")

    def test_no_host_prints_guidance_and_never_calls_ssh(self):
        self.offline_api()
        os.environ.update({"SUBWAVE_BASE_URL": "http://127.0.0.1:7700"})

        def refuse(*a, **kw):
            raise AssertionError("tier 1 with no SUBWAVE_HOST must not touch ssh")

        subwave.ssh_run = refuse
        code, output = self.run_cli("status")

        self.assertIn("set SUBWAVE_HOST=", output)
        self.assertIn("finds the", output)

    def test_a_wrong_container_on_the_right_host_is_recovered(self):
        # The state dir is right and the container name is wrong: the probe
        # fails, so discovery is the only thing that can name the real one.
        self.offline_api()
        os.environ.update({"SUBWAVE_BASE_URL": "http://127.0.0.1:7700",
                           "SUBWAVE_HOST": "me@host",
                           "SUBWAVE_STATE_DIR": "/var/sub-wave",
                           "SUBWAVE_CONTAINER": "wrong-name"})
        self.stub_ssh(FakeResult("", "Error: No such container: wrong-name", 1),
                      "FOUND sub-wave-controller /var/sub-wave\n")

        code, output = self.run_cli("status")

        self.assertIn("tier      1", output)
        self.assertIn("SUBWAVE_CONTAINER=sub-wave-controller", output)

    def test_a_host_with_no_station_says_so_without_a_traceback(self):
        self.offline_api()
        os.environ.update({"SUBWAVE_BASE_URL": "http://127.0.0.1:7700",
                           "SUBWAVE_HOST": "me@host"})
        self.stub_ssh(FakeResult("", "sh: 1: docker: not found", 127))

        code, output = self.run_cli("status")

        self.assertNotIn("Traceback", output)
        self.assertIn("no container on me@host has a mounted settings.json", output)
        self.assertIn("the host said: sh: 1: docker: not found", output)

    def test_status_does_not_claim_to_be_read_only(self):
        self.offline_api()
        os.environ.update({"SUBWAVE_BASE_URL": "http://127.0.0.1:7700"})
        self.stub_ssh("")
        code, output = self.run_cli("status")
        self.assertNotIn("read-only", output)


if __name__ == "__main__":
    unittest.main()
