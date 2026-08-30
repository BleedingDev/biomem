"""Security and lifecycle regressions for the browser-to-background transport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(__file__).resolve().parent
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src"
    for name in ("chrome", "firefox", "safari")
}
NODE = shutil.which("node")


def run_node(harness: str, *arguments: object) -> dict:
    completed = subprocess.run(
        [NODE, str(TEST_ROOT / harness), *(str(argument) for argument in arguments)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode or not completed.stdout:
        raise AssertionError(
            f"Node harness failed ({completed.returncode}): "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    return json.loads(completed.stdout)


@unittest.skipUnless(NODE, "Node.js is required for JavaScript transport harnesses")
class ExactLoopbackEndpointTests(unittest.TestCase):
    def run_background(self, browser: str, url: str, command: str) -> dict:
        return run_node(
            "background_url_harness.js",
            BROWSERS[browser] / "background.js",
            url,
            command,
        )

    def test_exact_api_endpoint_is_used_for_commands_and_health(self) -> None:
        for browser in BROWSERS:
            for command, expected_url, expected_method in (
                ("command", "http://127.0.0.1:8766/api", "POST"),
                ("health", "http://127.0.0.1:8766/api/health", "GET"),
            ):
                with self.subTest(browser=browser, command=command):
                    payload = self.run_background(
                        browser, "http://127.0.0.1:8766/api", command
                    )
                    self.assertTrue(payload["result"].get("ok"), payload)
                    self.assertEqual(
                        [{"url": expected_url, "method": expected_method}],
                        payload["fetchCalls"],
                    )

    def test_userinfo_ports_schemes_hosts_and_paths_are_rejected(self) -> None:
        invalid_urls = (
            "http://user@127.0.0.1:8766/api",
            "http://user:password@127.0.0.1:8766/api",
            "http://127.0.0.1/api",
            "http://127.0.0.1:8765/api",
            "http://127.0.0.1:9876/api",
            "https://127.0.0.1:8766/api",
            "http://localhost:8766/api",
            "http://127.0.0.2:8766/api",
            "http://127.0.0.1:8766/",
            "http://127.0.0.1:8766/api/",
            "http://127.0.0.1:8766/api/health",
            "http://127.0.0.1:8766/other",
            "http://127.0.0.1:8766/api?redirect=/other",
            "http://127.0.0.1:8766/api#other",
            "http://@127.0.0.1:8766/api",
            "http://127.0.0.1:8766/other/../api",
            "http://127.000.000.001:8766/api",
        )
        for browser in BROWSERS:
            for url in invalid_urls:
                with self.subTest(browser=browser, url=url):
                    payload = self.run_background(browser, url, "command")
                    result = payload["result"]
                    self.assertFalse(result.get("ok"), payload)
                    self.assertEqual("INVALID_LOCAL_URL", result.get("code"), payload)
                    self.assertEqual([], payload["fetchCalls"], payload)


@unittest.skipUnless(NODE, "Node.js is required for JavaScript transport harnesses")
class RuntimeDisconnectTests(unittest.TestCase):
    def test_runtime_messaging_failure_disconnects_and_preserves_error(self) -> None:
        expected_message = "The message port closed before a response was received."
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                payload = run_node(
                    "content_runtime_error_harness.js",
                    root / "content" / "bdbm-client.js",
                )
                self.assertTrue(payload["connectedBeforeFailure"], payload)
                self.assertFalse(payload["connectedAfterFailure"], payload)
                self.assertEqual(
                    [{"status": 0, "message": expected_message}],
                    payload["disconnectEvents"],
                )
                self.assertEqual("SERVICE_UNAVAILABLE", payload["error"]["code"])
                self.assertEqual(expected_message, payload["error"]["message"])
                self.assertEqual(
                    "SERVICE_UNAVAILABLE",
                    payload["error"]["response"]["code"],
                )


class CrossCopyParityTests(unittest.TestCase):
    def test_transport_sources_are_byte_identical(self) -> None:
        for relative_path in ("background.js", "content/bdbm-client.js"):
            copies = {
                browser: (root / relative_path).read_bytes()
                for browser, root in BROWSERS.items()
            }
            hashes = {
                browser: hashlib.sha256(data).hexdigest()
                for browser, data in copies.items()
            }
            self.assertEqual(1, len(set(hashes.values())), hashes)


if __name__ == "__main__":
    unittest.main()
