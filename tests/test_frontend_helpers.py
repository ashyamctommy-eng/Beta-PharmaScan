"""
tests/test_frontend_helpers.py — runs every browser-side check we have.

templates/index.html is one file with no build step, so part of the app only exists in the
browser: the LaTeX delimiters, the device vault's storage rules, the structure-image
rewrite. Those blocks are written so they can be lifted out of the template and driven
under node, and `tests/js/*_check.js` does exactly that — against the shipped text, not a
copy, so a fix there cannot drift from the test.

This file is the single place that runs them, so adding `tests/js/foo_check.js` is enough
to get it run here and in CI. Node is not a runtime dependency of the app: without it these
tests skip.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import glob
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_CHECKS = sorted(glob.glob(str(ROOT / "tests" / "js" / "*_check.js")))


def _node() -> str | None:
    return shutil.which("node")


class TestBrowserSideChecks(unittest.TestCase):
    def test_there_is_something_to_run(self) -> None:
        """A silent glob failure would make this file pass while testing nothing."""
        names = [Path(p).name for p in JS_CHECKS]
        self.assertIn("math_delimiters_check.js", names)
        self.assertIn("vault_check.js", names)

    @unittest.skipIf(_node() is None, "node is not installed")
    def test_every_check_passes(self) -> None:
        for script in JS_CHECKS:
            with self.subTest(script=Path(script).name):
                result = subprocess.run([_node(), script], capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("0 failed", result.stdout)
                self.assertNotIn("FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
