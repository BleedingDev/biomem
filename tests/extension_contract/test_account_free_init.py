"""Behavioral regressions for account-free browser content initialization."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(__file__).resolve().parent
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src" / "content" / "common.js"
    for name in ("chrome", "firefox", "safari")
}
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for JavaScript behavior tests")
class AccountFreeInitializationTests(unittest.TestCase):
    def run_browser(self, browser: str) -> dict:
        completed = subprocess.run(
            [
                NODE,
                str(TEST_ROOT / "account_free_init_harness.js"),
                str(BROWSERS[browser]),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_initialization_installs_hooks_without_authentication(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                result = self.run_browser(browser)
                self.assertFalse(result["authStatePresent"])
                self.assertTrue(result["hooksSetUp"])
                self.assertIn("attachSendHooks", result["events"])
                self.assertEqual([], result["commands"])


if __name__ == "__main__":
    unittest.main()
