"""Cross-browser conformance tests for the biomem extension transport contract.

These tests intentionally describe the target contract, not the currently broken
implementation. They are expected to fail against E2E-002/E2E-003 until the
extension transport fixes land.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(__file__).resolve().parent
CONTRACT = json.loads((TEST_ROOT / "contract.json").read_text(encoding="utf-8"))
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src"
    for name in ("chrome", "firefox", "safari")
}
NODE = shutil.which("node")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_node(harness: str, source: Path, scenario: str) -> dict:
    completed = subprocess.run(
        [NODE, str(TEST_ROOT / harness), str(source), scenario],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not completed.stdout:
        raise AssertionError(
            f"Node harness produced no JSON (exit {completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"Node harness emitted invalid JSON: {completed.stdout!r}; "
            f"stderr={completed.stderr!r}"
        ) from error
    if completed.returncode:
        raise AssertionError(
            f"Node harness failed with exit {completed.returncode}: {payload!r}; "
            f"stderr={completed.stderr!r}"
        )
    return payload


def normalized_manifest(data: dict) -> dict:
    """Remove only documented browser-specific manifest differences."""
    normalized = dict(data)
    normalized.pop("browser_specific_settings", None)
    background = normalized.get("background", {})
    entries = set(background.get("scripts", []))
    if background.get("service_worker"):
        entries.add(background["service_worker"])
    normalized["background"] = {"entries": sorted(entries)}
    return normalized


class ManifestContractTests(unittest.TestCase):
    def test_common_manifest_contract_does_not_drift(self) -> None:
        manifests = {
            browser: json.loads(read(root / "manifest.json"))
            for browser, root in BROWSERS.items()
        }
        canonical = normalized_manifest(manifests["chrome"])
        for browser, manifest in manifests.items():
            with self.subTest(browser=browser):
                self.assertEqual(canonical, normalized_manifest(manifest))

    def test_manifests_use_only_the_required_loopback_http_origin(self) -> None:
        expected_hosts = {f"{CONTRACT['loopback_origin']}/*"}
        expected_optional_hosts = set(CONTRACT["provider_origins"])
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                manifest = json.loads(read(root / "manifest.json"))
                self.assertEqual(3, manifest.get("manifest_version"))
                self.assertEqual(expected_hosts, set(manifest.get("host_permissions", [])))
                self.assertEqual(
                    expected_optional_hosts,
                    set(manifest.get("optional_host_permissions", [])),
                )
                self.assertFalse(
                    any(
                        origin.startswith("ws://")
                        for origin in (
                            manifest.get("host_permissions", [])
                            + manifest.get("optional_host_permissions", [])
                        )
                    ),
                    "content scripts no longer own a direct WebSocket transport",
                )

    def test_declared_background_and_dynamic_content_assets_exist(self) -> None:
        dynamic_assets = {
            "content/bdbm-client.js",
            "content/prompt-builder.js",
            "content/common.js",
            "content/inject.css",
            "content/site-gemini.js",
            "content/site-chatgpt.js",
            "content/site-claude.js",
            "content/site-perplexity.js",
        }
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                manifest = json.loads(read(root / "manifest.json"))
                background = manifest.get("background", {})
                entries = set(background.get("scripts", []))
                if background.get("service_worker"):
                    entries.add(background["service_worker"])
                self.assertEqual({"background.js"}, entries)
                for relative_path in sorted(dynamic_assets | entries):
                    self.assertTrue((root / relative_path).is_file(), relative_path)


class SourceContractTests(unittest.TestCase):
    def test_transport_critical_copies_do_not_drift(self) -> None:
        for relative_path in (
            "background.js",
            "content/bdbm-client.js",
            "options.js",
        ):
            canonical = read(BROWSERS["chrome"] / relative_path).replace("\r\n", "\n")
            for browser in ("firefox", "safari"):
                with self.subTest(browser=browser, path=relative_path):
                    candidate = read(BROWSERS[browser] / relative_path).replace("\r\n", "\n")
                    self.assertTrue(
                        canonical == candidate,
                        f"{relative_path} differs from the Chromium canonical copy",
                    )

    def test_content_clients_route_all_loopback_io_through_background(self) -> None:
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                source = read(root / "content" / "bdbm-client.js")
                self.assertRegex(source, r"localCommand")
                self.assertFalse(
                    re.search(r"\bnew\s+WebSocket\s*\(", source),
                    "content scripts must not construct loopback WebSockets",
                )
                self.assertFalse(
                    re.search(r"\bfetch\s*\(", source),
                    "content scripts must not fetch loopback endpoints directly",
                )

    def test_extension_pages_do_not_use_opaque_health_probes(self) -> None:
        for browser, root in BROWSERS.items():
            for filename in ("options.js", "popup.js"):
                with self.subTest(browser=browser, path=filename):
                    source = read(root / filename)
                    self.assertFalse(
                        "no-cors" in source,
                        f"{filename} still accepts opaque health responses",
                    )
                    self.assertFalse(
                        re.search(r"\bfetch\s*\(", source),
                        f"{filename} must delegate health to the background",
                    )
                    self.assertRegex(source, r"localCommand")
                    self.assertRegex(source, r"command\s*:\s*[\"']health[\"']")

    def test_background_declares_local_command_routing(self) -> None:
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                source = read(root / "background.js")
                self.assertRegex(source, r"msg\.type\s*===\s*[\"']localCommand[\"']")


@unittest.skipUnless(NODE, "Node.js is required for JavaScript contract harnesses")
class BackgroundBehaviorTests(unittest.TestCase):
    def run_scenario(self, browser: str, scenario: str) -> dict:
        return run_node(
            "background_harness.js",
            BROWSERS[browser] / "background.js",
            scenario,
        )

    def test_config_is_whitelisted_and_unknown_stored_fields_are_purged(self) -> None:
        expected_keys = {
            "bdbmWsUrl",
            "bdbmHttpUrl",
            "memoryEnabled",
            "sites",
        }
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "config_whitelist")
                config = payload["result"]["config"]
                self.assertEqual(expected_keys, set(config))
                self.assertIs(False, config["memoryEnabled"])
                self.assertEqual([{"config": config}], payload["storageWrites"])

    def test_valid_health_requires_readable_product_status_and_version(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "health_valid")
                result = payload["result"]
                self.assertTrue(result.get("ok"), result)
                self.assertEqual(200, result.get("status"))
                self.assertEqual(CONTRACT["product"], result.get("data", {}).get("product"))
                self.assertEqual(CONTRACT["healthy_status"], result.get("data", {}).get("status"))
                self.assertEqual(
                    CONTRACT["protocol_version"],
                    result.get("data", {}).get("protocol_version"),
                )
                self.assertIs(True, result.get("data", {}).get("ready"))
                self.assertEqual(
                    CONTRACT["health_transport"],
                    result.get("data", {}).get("transport"),
                )
                self.assertTrue(result.get("data", {}).get("version"))
                self.assertEqual(1, len(payload["fetchCalls"]))
                call = payload["fetchCalls"][0]
                self.assertEqual(CONTRACT["health_method"], call["method"])
                self.assertTrue(call["url"].endswith(CONTRACT["health_path"]), call)
                self.assertIsNone(call["mode"], "opaque no-cors probes are forbidden")

    def test_false_positive_health_responses_are_rejected(self) -> None:
        scenarios = (
            "health_invalid_product",
            "health_invalid_status",
            "health_invalid_protocol",
            "health_not_ready",
            "health_wrong_transport",
            "health_missing_version",
            "health_non_json",
            "health_opaque",
        )
        for browser in BROWSERS:
            for scenario in scenarios:
                with self.subTest(browser=browser, scenario=scenario):
                    payload = self.run_scenario(browser, scenario)
                    result = payload["result"]
                    self.assertFalse(result.get("ok"), result)
                    self.assertTrue(result.get("error"), result)
                    self.assertEqual(
                        1,
                        len(payload["fetchCalls"]),
                        "the background must reject the actual HTTP response, not the message type",
                    )
                    self.assertTrue(
                        payload["fetchCalls"][0]["url"].endswith(CONTRACT["health_path"]),
                        payload,
                    )

    def test_service_unavailable_and_network_errors_are_returned(self) -> None:
        for browser in BROWSERS:
            for scenario in ("health_503", "command_503", "network_error"):
                with self.subTest(browser=browser, scenario=scenario):
                    payload = self.run_scenario(browser, scenario)
                    result = payload["result"]
                    self.assertFalse(result.get("ok"), result)
                    self.assertTrue(result.get("error"), result)
                    self.assertEqual(
                        1,
                        len(payload["fetchCalls"]),
                        "the background must translate the actual transport failure",
                    )
                    if scenario.endswith("503"):
                        self.assertEqual(503, result.get("status"), result)
                    self.assertEqual(
                        CONTRACT["service_unavailable_code"],
                        result.get("data", {}).get("code"),
                        result,
                    )

    def test_commands_are_posted_by_the_background_and_return_parsed_json(self) -> None:
        expected_command = {"command": "status"}
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "command_success")
                result = payload["result"]
                self.assertTrue(result.get("ok"), result)
                self.assertEqual("ok", result.get("data", {}).get("status"))
                self.assertEqual(1, len(payload["fetchCalls"]))
                call = payload["fetchCalls"][0]
                self.assertEqual(CONTRACT["command_method"], call["method"])
                self.assertTrue(call["url"].endswith(CONTRACT["command_path"]), call)
                self.assertEqual(expected_command, json.loads(call["body"]))
                self.assertIsNone(call["mode"])


@unittest.skipUnless(NODE, "Node.js is required for JavaScript contract harnesses")
class ContentBehaviorTests(unittest.TestCase):
    def run_scenario(self, browser: str, scenario: str) -> dict:
        return run_node(
            "content_harness.js",
            BROWSERS[browser] / "content" / "bdbm-client.js",
            scenario,
        )

    def test_available_server_connects_and_sends_command_via_background(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "available")
                self.assertIsNone(payload["error"], payload)
                self.assertTrue(payload["connected"], payload)
                self.assertEqual(0, payload["webSocketAttempts"], payload)
                self.assertEqual(0, payload["directFetchAttempts"], payload)
                local_commands = [
                    message for message in payload["messages"]
                    if message.get("type") == "localCommand"
                ]
                self.assertGreaterEqual(len(local_commands), 2, payload)
                self.assertEqual(CONTRACT["health_command"], local_commands[0]["command"])
                self.assertEqual("status", local_commands[1]["command"]["command"])

    def test_unavailable_server_is_not_reported_as_connected(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "unavailable")
                self.assertFalse(payload["connected"], payload)
                self.assertEqual(0, payload["webSocketAttempts"], payload)
                self.assertEqual(0, payload["directFetchAttempts"], payload)
                self.assertIsNotNone(payload["error"], payload)
                self.assertEqual(
                    CONTRACT["service_unavailable_code"],
                    payload["error"].get("code"),
                    payload,
                )

    def test_store_forwards_exact_clean_response_with_summaries(self) -> None:
        for browser in BROWSERS:
            with self.subTest(browser=browser):
                payload = self.run_scenario(browser, "exact_store")
                self.assertIsNone(payload["error"], payload)
                store_commands = [
                    message["command"]
                    for message in payload["messages"]
                    if message.get("type") == "localCommand"
                    and isinstance(message.get("command"), dict)
                    and message["command"].get("command") == "store"
                ]
                self.assertEqual(1, len(store_commands), payload)
                self.assertEqual(
                    "BIOMEM_LOSSLESS_4107 means the silver compass points east.",
                    store_commands[0].get("response_text"),
                    payload,
                )


if __name__ == "__main__":
    unittest.main()
