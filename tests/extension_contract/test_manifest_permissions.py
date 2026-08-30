"""Least-privilege manifest regressions for browser provider access."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(__file__).resolve().parent
CONTRACT = json.loads((TEST_ROOT / "contract.json").read_text(encoding="utf-8"))
BROWSERS = {
    name: REPO_ROOT / "extensions" / f"{name}-src"
    for name in ("chrome", "firefox", "safari")
}
SITE_PERMISSIONS = re.compile(
    r"const\s+SITE_PERMISSIONS\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)
ORIGIN = re.compile(r"[\"'](https?://[^\"']+/\*)[\"']")


def options_permission_origins(source: str) -> set[str]:
    """Return the origins named by the options-page permission controls."""
    match = SITE_PERMISSIONS.search(source)
    if match is None:
        raise AssertionError("options.js does not declare SITE_PERMISSIONS")
    return set(ORIGIN.findall(match.group("body")))


class ManifestPermissionModelTests(unittest.TestCase):
    def test_loopback_is_required_and_provider_origins_are_optional(self) -> None:
        expected_required = {f"{CONTRACT['loopback_origin']}/*"}
        expected_optional = set(CONTRACT["provider_origins"])

        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                manifest = json.loads(
                    (root / "manifest.json").read_text(encoding="utf-8")
                )
                required = set(manifest.get("host_permissions", []))
                optional = set(manifest.get("optional_host_permissions", []))

                self.assertEqual(expected_required, required)
                self.assertEqual(expected_optional, optional)
                self.assertTrue(required.isdisjoint(optional))

    def test_options_flow_names_only_declared_optional_origins(self) -> None:
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                manifest = json.loads(
                    (root / "manifest.json").read_text(encoding="utf-8")
                )
                source = (root / "options.js").read_text(encoding="utf-8")

                self.assertEqual(
                    set(manifest["optional_host_permissions"]),
                    options_permission_origins(source),
                )
                self.assertRegex(source, r"chrome\.permissions\.contains\(\{\s*origins\s*\}")
                self.assertRegex(source, r"chrome\.permissions\.request\(\{\s*origins\s*\}")

    def test_websocket_permissions_do_not_return(self) -> None:
        for browser, root in BROWSERS.items():
            with self.subTest(browser=browser):
                manifest = json.loads(
                    (root / "manifest.json").read_text(encoding="utf-8")
                )
                declared_origins = (
                    manifest.get("host_permissions", [])
                    + manifest.get("optional_host_permissions", [])
                )
                self.assertFalse(
                    any(origin.startswith(("ws://", "wss://")) for origin in declared_origins)
                )


if __name__ == "__main__":
    unittest.main()
