"""The quickstart contract: a stranger clones the repo and chats at a station.

This is the one test that copies only TRACKED files into a temp directory, so
`instance/` and `.env` are absent exactly as they would be after a clone. It
then drives the documented sequence as subprocesses against the in-process mock,
with nothing but a base URL and credentials in the environment: no ssh, no host
access, no HOME.

If this test fails, the README is lying.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sys
from pathlib import Path

# Both invocation styles must work: `discover -s tests` puts this directory on
# sys.path, a dotted `unittest tests.test_smoke` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_station

ROOT = Path(__file__).resolve().parents[1]


def tracked_files():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            capture_output=True, check=True)
    return [name for name in result.stdout.decode().split("\0") if name]


def fresh_clone(destination):
    """Copy exactly the tracked files: no instance/, no .env, no untracked work."""
    for name in tracked_files():
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.server = mock_station.start_mock()
        self.addCleanup(self.server.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fresh_clone(self.tmp)

    # -- helpers -----------------------------------------------------------
    def env(self):
        """Only a base URL and credentials: no ssh target, no state dir, no HOME."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SUBWAVE_BASE_URL": self.server.url,
            "SUBWAVE_ADMIN_USER": "mock-admin",
            "SUBWAVE_ADMIN_PASS": "mock-pass",
        }

    def run_tool(self, *args):
        result = subprocess.run(
            [sys.executable, str(self.tmp / "scripts" / "subwave.py")] + list(args),
            cwd=self.tmp, env=self.env(), capture_output=True, text=True, timeout=120)
        return result

    def requests(self):
        return len(self.server.station.requests)

    # -- the clone is clean -------------------------------------------------
    def test_the_clone_has_no_instance_and_no_env(self):
        self.assertFalse((self.tmp / "instance").exists())
        self.assertFalse((self.tmp / ".env").exists())
        self.assertTrue((self.tmp / "AGENTS.md").exists())
        self.assertTrue((self.tmp / "scripts" / "subwave.py").exists())

    # -- scenario A: the read path at tier 1 -------------------------------
    def test_documented_read_sequence_at_tier_1(self):
        status = self.run_tool("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("tier      1", status.stdout)

        pull = self.run_tool("pull", "--allow-lossy")
        self.assertEqual(pull.returncode, 0, pull.stderr)
        self.assertIn("lost", pull.stdout)
        self.assertTrue((self.tmp / "instance" / "ids.lock.json").is_file())

        # A lossy capture is a hard failure by design: it must never be applied.
        validate = self.run_tool("validate")
        self.assertEqual(validate.returncode, 1, validate.stdout)
        self.assertIn("lossy", validate.stdout)

        diff = self.run_tool("diff")
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertIn("readable fields", diff.stdout)

        before = self.requests()
        apply_run = self.run_tool("apply")
        self.assertEqual(apply_run.returncode, 1, "apply must refuse a lossy capture")
        self.assertIn("lossy", apply_run.stdout + apply_run.stderr)
        self.assertEqual(self.requests(), before, "a refused apply must send nothing")

    # -- scenario B: the write path from the committed demo -----------------
    def test_documented_write_path_from_the_committed_demo(self):
        # A stranger can try the write path with the committed fictional station.
        shutil.copytree(self.tmp / "examples" / "demo", self.tmp / "instance")

        validate = self.run_tool("validate")
        self.assertEqual(validate.returncode, 0, validate.stdout)
        self.assertIn("ok", validate.stdout)

        before = self.requests()
        dry_run = self.run_tool("apply")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("dry run", dry_run.stdout)
        self.assertIn("0 requests sent", dry_run.stdout)
        self.assertEqual(self.requests(), before, "a dry run must send nothing")

    # -- the whole thing never needed host access --------------------------
    def test_no_ssh_variables_are_needed(self):
        env = self.env()
        self.assertNotIn("SUBWAVE_HOST", env)
        self.assertNotIn("SUBWAVE_STATE_DIR", env)
        status = self.run_tool("status")
        self.assertEqual(status.returncode, 0)
        self.assertIn("missing SUBWAVE_HOST, SUBWAVE_STATE_DIR", status.stdout)


if __name__ == "__main__":
    unittest.main()
