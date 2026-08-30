from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
CALLER = ROOT / ".github/workflows/release.yml"
OPTIONAL = ROOT / ".github/workflows/release-optional.yml"
BROWSER = ROOT / ".github/workflows/publish-browser-channels.yml"
COLLECTOR = ROOT / ".github/workflows/release-collect-evidence.yml"
POLICY_SPEC = importlib.util.spec_from_file_location(
    "release_policy", ROOT / "scripts/release_policy.py"
)
assert POLICY_SPEC and POLICY_SPEC.loader
release_policy = importlib.util.module_from_spec(POLICY_SPEC)
POLICY_SPEC.loader.exec_module(release_policy)


class ReleaseChannelOrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.caller = CALLER.read_text(encoding="utf-8")
        cls.optional = OPTIONAL.read_text(encoding="utf-8")
        cls.browser = BROWSER.read_text(encoding="utf-8")
        cls.collector = COLLECTOR.read_text(encoding="utf-8")

    def collector_program(self) -> str:
        lines = self.collector.splitlines()
        start = next(index + 1 for index, line in enumerate(lines) if "<<'PY'" in line)
        end = next(
            index for index in range(start, len(lines)) if lines[index].strip() == "PY"
        )
        return textwrap.dedent("\n".join(lines[start:end]))

    def populate_fragments(
        self, base: Path, *, channels: str, dry_run: bool, duplicate_chrome: bool = False
    ) -> dict:
        policy = release_policy.resolve_policy("v0.0.2", channels, dry_run)
        (base / "policy").mkdir(parents=True)
        release_policy.write_json(base / "policy/release-policy.json", policy)
        (base / "release").mkdir()
        shutil.copy2(ROOT / "release/release-policy.json", base / "release/release-policy.json")
        (base / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/release_policy.py", base / "scripts/release_policy.py")

        contract = release_policy.load_contract()
        selected = set(policy["selected_optional_channels"])
        browser_sources = {
            "chrome_web_store": "release-channel-evidence-fragment-chrome-web-store",
            "firefox_amo": "release-channel-evidence-fragment-firefox-amo",
            "safari_public": (
                "release-channel-evidence-fragment-safari_public"
                if "safari_public" in selected and not dry_run
                else "release-channel-evidence-fragment-safari-public"
            ),
        }
        for channel, definition in contract["channels"].items():
            if definition["selection"] != "explicit":
                continue
            if channel in selected:
                status = "blocked_environment"
                reason = "not_attempted_dry_run" if dry_run else "manual_enablement_required"
            else:
                status = "skipped_not_configured"
                reason = "not_selected"
            source = browser_sources.get(channel, "release-channel-evidence-fragment-base")
            release_policy.write_json(
                base / "fragments" / source / f"{channel}.json",
                release_policy.make_evidence(policy, channel, status, reason),
            )
        if duplicate_chrome:
            shutil.copy2(
                base
                / "fragments/release-channel-evidence-fragment-chrome-web-store/chrome_web_store.json",
                base / "fragments/release-channel-evidence-fragment-base/chrome_web_store.json",
            )
        return policy

    def test_browser_workflow_is_the_only_browser_evidence_owner(self) -> None:
        for channel in ("chrome_web_store", "firefox_amo", "safari_public"):
            # The optional producer may name each channel once only to exclude
            # it from its generic base fragment; it must not emit its evidence.
            self.assertEqual(1, self.optional.count(channel))
            self.assertNotIn(f'--channel {channel}', self.optional)
            self.assertIn(channel, self.browser)
            self.assertIn(channel, self.collector)

        self.assertEqual(
            1,
            self.browser.count(
                "name: release-channel-evidence-fragment-chrome-web-store"
            ),
        )
        self.assertEqual(
            1,
            self.browser.count("name: release-channel-evidence-fragment-firefox-amo"),
        )
        self.assertEqual(
            1,
            self.browser.count("name: release-channel-evidence-fragment-safari-public"),
        )
        self.assertEqual(
            1,
            self.browser.count("uses: ./.github/workflows/release-sign-apple.yml"),
        )

    def test_safari_selected_unselected_and_dry_run_paths_are_exclusive(self) -> None:
        unavailable = self.browser.split(
            "  safari-not-selected-or-dry-run:\n", 1
        )[1].split("  safari-public:\n", 1)[0]
        selected = self.browser.split("  safari-public:\n", 1)[1]
        self.assertIn(
            "if: needs.preflight.outputs.safari != 'true' || inputs.dry_run",
            unavailable,
        )
        self.assertIn(
            "if: needs.preflight.outputs.safari == 'true' && !inputs.dry_run",
            selected,
        )
        self.assertIn("status=skipped_not_configured", unavailable)
        self.assertIn("status=blocked_environment", unavailable)
        self.assertIn("channel: safari_public", selected)

    def test_chrome_and_firefox_classify_unselected_and_dry_run_without_writes(self) -> None:
        chrome = self.browser.split("  chrome-", 1)[1]
        self.assertIn(
            'status, reason, attempt = "skipped_not_configured", "not_selected", False',
            chrome,
        )
        self.assertIn(
            'status, reason, attempt = "blocked_environment", "not_attempted_dry_run", False',
            chrome,
        )
        self.assertIn("if: steps.readiness.outputs.attempt == 'true'", chrome)

        firefox = self.browser.split("  firefox-", 1)[1]
        self.assertIn(
            'status, reason, lookup = "skipped_not_configured", "not_selected", False',
            firefox,
        )
        self.assertIn(
            'status, reason, lookup = "blocked_environment", "not_attempted_dry_run", False',
            firefox,
        )
        self.assertIn("if: steps.readiness.outputs.lookup == 'true'", firefox)
        provider = firefox.index("Query the exact AMO version before creating it")
        signing = firefox.index("Sign the exact tested input through AMO")
        self.assertLess(provider, signing)

    def test_caller_waits_for_both_producers_before_exactly_one_fan_in(self) -> None:
        browser_job = self.caller.split("  browser-channels:\n", 1)[1].split(
            "  collect-evidence:\n", 1
        )[0]
        collector_job = self.caller.split("  collect-evidence:\n", 1)[1].split(
            "  finalize:\n", 1
        )[0]
        finalize = self.caller.split("  finalize:\n", 1)[1].split(
            "  enforce-selected-channels:\n", 1
        )[0]
        self.assertIn("uses: ./.github/workflows/publish-browser-channels.yml", browser_job)
        self.assertIn("reuse_release_policy_artifact: true", browser_job)
        self.assertIn(
            "needs: [resolve, core, optional-channels, browser-channels]",
            collector_job,
        )
        self.assertIn("always() && needs.core.result == 'success'", collector_job)
        self.assertIn("uses: ./.github/workflows/release-collect-evidence.yml", collector_job)
        self.assertIn("needs: [resolve, core, collect-evidence]", finalize)

        all_workflows = "\n".join(
            (self.caller, self.optional, self.browser, self.collector)
        )
        self.assertEqual(1, all_workflows.count("name: release-channel-evidence\n"))
        self.assertNotIn("merge-multiple: true", self.collector)
        self.assertIn("expected exactly one evidence fragment", self.collector)

    def test_caller_isolates_signing_readiness_from_raw_secrets(self) -> None:
        optional_job = self.caller.split("  optional-channels:\n", 1)[1].split(
            "  browser-channels:\n", 1
        )[0]
        browser_job = self.caller.split("  browser-channels:\n", 1)[1].split(
            "  collect-evidence:\n", 1
        )[0]
        self.assertNotIn("    secrets:\n", optional_job)
        self.assertIn("    secrets:\n", browser_job)
        self.assertNotIn("secrets: inherit", optional_job + browser_job)
        self.assertIn(
            "vars.SIGNPATH_CREDENTIALS_CONFIGURED == 'true'", optional_job
        )
        self.assertIn(
            "vars.APPLE_COMMON_CREDENTIALS_CONFIGURED == 'true'", optional_job
        )
        self.assertNotIn("AMO_JWT_SECRET", optional_job)
        for secret in (
            "CWS_CLIENT_ID",
            "CWS_CLIENT_SECRET",
            "CWS_REFRESH_TOKEN",
            "AMO_JWT_ISSUER",
            "AMO_JWT_SECRET",
        ):
            self.assertIn(f"{secret}: ${{{{ secrets.{secret} }}}}", browser_job)
        for raw_signing_secret in (
            "SIGNPATH_API_TOKEN",
            "APPLE_TEAM_ID",
            "APPLE_DEVELOPER_ID_P12_BASE64",
            "APPLE_SAFARI_HOST_PROFILE_BASE64",
            "APPLE_NOTARY_KEY_BASE64",
        ):
            self.assertNotIn(raw_signing_secret, optional_job + browser_job)

        safari_call = self.browser.split("  safari-public:\n", 1)[1]
        self.assertNotIn("    secrets:\n", safari_call)
        self.assertIn(
            "inputs.apple_common_credentials_configured ||",
            safari_call,
        )
        self.assertIn(
            "inputs.apple_safari_credentials_configured ||",
            safari_call,
        )
        self.assertIn("vars.APPLE_COMMON_CREDENTIALS_CONFIGURED == 'true'", safari_call)
        self.assertIn("vars.APPLE_SAFARI_CREDENTIALS_CONFIGURED == 'true'", safari_call)

    def test_optional_workflow_can_call_read_only_signing_boundaries(self) -> None:
        permission_header = self.optional.split("jobs:\n", 1)[0]
        self.assertIn("actions: read", permission_header)
        self.assertIn("contents: read", permission_header)
        self.assertNotIn("write", permission_header)

    def test_collector_embedded_python_is_syntactically_valid(self) -> None:
        compile(self.collector_program(), "release-collect-evidence-heredoc", "exec")

    def test_collector_preserves_selected_unselected_and_dry_run_evidence(self) -> None:
        cases = (
            ("none", False),
            ("chrome_web_store,firefox_amo,safari_public", False),
            ("chrome_web_store,firefox_amo,safari_public", True),
        )
        for channels, dry_run in cases:
            with self.subTest(channels=channels, dry_run=dry_run), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                policy = self.populate_fragments(base, channels=channels, dry_run=dry_run)
                expected_safari_source = (
                    "release-channel-evidence-fragment-safari_public"
                    if "safari_public" in policy["selected_optional_channels"]
                    and not dry_run
                    else "release-channel-evidence-fragment-safari-public"
                )
                self.assertTrue((base / "fragments" / expected_safari_source).is_dir())
                completed = subprocess.run(
                    [sys.executable, "-c", self.collector_program()],
                    cwd=base,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                evidence = release_policy.read_evidence(base / "channel-evidence")
                optional = {
                    name
                    for name, definition in release_policy.load_contract()["channels"].items()
                    if definition["selection"] == "explicit"
                }
                self.assertEqual(optional, set(evidence))
                selected = set(policy["selected_optional_channels"])
                for channel, item in evidence.items():
                    expected = "blocked_environment" if channel in selected else "skipped_not_configured"
                    self.assertEqual(expected, item["status"], channel)

    def test_collector_rejects_duplicate_browser_evidence_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self.populate_fragments(
                base,
                channels="chrome_web_store",
                dry_run=False,
                duplicate_chrome=True,
            )
            completed = subprocess.run(
                [sys.executable, "-c", self.collector_program()],
                cwd=base,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn(
                "expected exactly one evidence fragment for chrome_web_store, found 2",
                completed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
