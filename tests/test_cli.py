"""The CLI surface itself.

A loop variable once shadowed the root parser in build_parser, so `--help`
printed a leaf command and every invocation was broken - and the whole suite
stayed green, because nothing asserted the CLI's own shape. These tests run the
real script, the way a user does.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "subwave.py"

COMMANDS = ("status", "pull", "diff", "character", "validate", "apply", "restore",
            "backup", "blocklist")


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


class RootParserTests(unittest.TestCase):
    def test_help_exits_zero(self):
        result = run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_lists_every_top_level_command(self):
        output = run("--help").stdout
        for command in COMMANDS:
            self.assertIn(command, output, "%r is missing from --help" % command)

    def test_the_root_parser_is_actually_the_root(self):
        # The regression: build_parser returned a leaf, so --help printed
        # "usage: subwave.py blocklist rule edit ..." instead of the program.
        # argparse wraps the usage line, so take the whole block up to the blank line.
        usage = run("--help").stdout.split("\n\n")[0]
        self.assertTrue(usage.startswith("usage: subwave.py"),
                        "unexpected usage line: %r" % usage)
        for command in COMMANDS:
            self.assertIn(command, usage,
                          "the usage line is not the whole program: %r" % usage)
        # A leaf's usage names a subcommand path, not the brace set.
        self.assertNotIn("rule edit", usage)

    def test_no_subcommand_is_an_error_not_a_crash(self):
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)


class CommandHelpTests(unittest.TestCase):
    def test_every_command_has_working_help(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                result = run(command, "--help")
                self.assertEqual(result.returncode, 0,
                                 "%s --help failed: %s" % (command, result.stderr))
                self.assertIn(command, result.stdout)

    def test_nested_groups_have_working_help(self):
        for path in (("character",), ("blocklist",), ("blocklist", "rule")):
            with self.subTest(path=path):
                result = run(*path, "--help")
                self.assertEqual(result.returncode, 0,
                                 "%s --help failed: %s" % (" ".join(path), result.stderr))


if __name__ == "__main__":
    unittest.main()
