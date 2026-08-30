from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_signing_evidence", ROOT / "scripts/release/verify_signing_evidence.py"
)
assert SPEC and SPEC.loader
signing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signing)


class SigningReadinessTests(unittest.TestCase):
    def native_windows(self, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "platform": "windows",
            "provider": "signpath_foundation",
            "signature_valid": True,
            "signer": "CN=SignPath Foundation, O=SignPath Foundation",
            "timestamp": {"status": "verified", "value": "2026-08-29T10:15:00Z"},
            "provider_receipt": "https://app.signpath.io/Web/SigningRequests/123",
        }
        value.update(changes)
        return value

    def verify_windows(self, base: Path, evidence: dict[str, object] | None = None) -> dict:
        unsigned = base / "biomem-windows-x86_64.zip"
        signed = base / "biomem-windows-x86_64-signed.zip"
        unsigned.write_bytes(b"tested unsigned canonical artifact")
        signed.write_bytes(b"signed artifact with Authenticode mutation")
        return signing.verify_signing_evidence(
            channel="windows_signed",
            provider="signpath_foundation",
            input_artifact=unsigned,
            expected_input_sha256=signing.sha256(unsigned),
            signed_artifact=signed,
            native_evidence=evidence or self.native_windows(),
            expected_signer="CN=SignPath Foundation, O=SignPath Foundation",
        )

    def test_no_provider_is_skipped_only_when_unselected(self) -> None:
        result = signing.provider_readiness(
            channel="windows_signed",
            selected=False,
            dry_run=False,
            required_environment=["SIGNPATH_API_TOKEN"],
            manually_enabled=False,
            adapter_enabled=False,
            environment={},
        )
        self.assertEqual(result["status"], "skipped_not_configured")
        self.assertFalse(result["publication_claimed"])

    def test_configured_provider_is_ready_but_never_claims_publication(self) -> None:
        result = signing.provider_readiness(
            channel="windows_signed",
            selected=True,
            dry_run=False,
            required_environment=["SIGNPATH_API_TOKEN", "SIGNPATH_ORGANIZATION_ID"],
            manually_enabled=True,
            adapter_enabled=True,
            environment={"SIGNPATH_API_TOKEN": "secret", "SIGNPATH_ORGANIZATION_ID": "org"},
        )
        self.assertEqual(result["status"], "ready_for_adapter")
        self.assertEqual(result["reason_code"], "provider_configured")
        self.assertFalse(result["publication_claimed"])

    def test_selected_missing_provider_is_blocked_environment(self) -> None:
        result = signing.provider_readiness(
            channel="windows_signed",
            selected=True,
            dry_run=False,
            required_environment=["SIGNPATH_API_TOKEN", "SIGNPATH_PROJECT_SLUG"],
            manually_enabled=True,
            adapter_enabled=False,
            environment={"SIGNPATH_PROJECT_SLUG": "biomem"},
        )
        self.assertEqual(result["status"], "blocked_environment")
        self.assertEqual(result["reason_code"], "missing_provider_configuration")
        self.assertEqual(result["missing_configuration"], ["SIGNPATH_API_TOKEN"])

    def test_boolean_presence_signal_never_claims_a_missing_adapter(self) -> None:
        missing = signing.provider_readiness(
            channel="windows_signed",
            selected=True,
            dry_run=False,
            required_environment=["PROVIDER_CREDENTIALS_CONFIGURED"],
            manually_enabled=True,
            adapter_enabled=False,
            environment={"PROVIDER_CREDENTIALS_CONFIGURED": ""},
        )
        self.assertEqual(missing["status"], "blocked_environment")
        self.assertEqual(missing["reason_code"], "missing_provider_configuration")

        configured = signing.provider_readiness(
            channel="windows_signed",
            selected=True,
            dry_run=False,
            required_environment=["PROVIDER_CREDENTIALS_CONFIGURED"],
            manually_enabled=True,
            adapter_enabled=False,
            environment={"PROVIDER_CREDENTIALS_CONFIGURED": "true"},
        )
        self.assertEqual(configured["status"], "blocked_environment")
        self.assertEqual(configured["reason_code"], "provider_adapter_not_enabled")
        self.assertFalse(configured["publication_claimed"])

    def test_valid_windows_evidence_emits_digest_identity_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.verify_windows(Path(temporary))
        self.assertEqual(result["channel_status"], "ready_for_assembly")
        self.assertNotEqual(result["input"]["sha256"], result["output"]["sha256"])
        self.assertEqual(
            result["verified_identity"]["signer"],
            "CN=SignPath Foundation, O=SignPath Foundation",
        )
        self.assertFalse(result["publication_claimed"])

    def test_invalid_signature_and_wrong_signer_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(signing.SigningEvidenceError, "did not pass"):
                self.verify_windows(Path(temporary), self.native_windows(signature_valid=False))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(signing.SigningEvidenceError, "exactly match"):
                self.verify_windows(Path(temporary), self.native_windows(signer="CN=Wrong Signer"))

    def test_stale_digest_is_rejected_before_evidence_is_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            unsigned = base / "input.zip"
            signed = base / "signed.zip"
            unsigned.write_bytes(b"current")
            signed.write_bytes(b"signed")
            with self.assertRaisesRegex(signing.SigningEvidenceError, "stale input digest"):
                signing.verify_signing_evidence(
                    channel="windows_signed",
                    provider="signpath_foundation",
                    input_artifact=unsigned,
                    expected_input_sha256="0" * 64,
                    signed_artifact=signed,
                    native_evidence=self.native_windows(),
                    expected_signer="CN=SignPath Foundation, O=SignPath Foundation",
                )

    def test_apple_requires_exact_team_bundles_notary_staple_and_gatekeeper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            unsigned = base / "safari-input.zip"
            signed = base / "safari-signed.zip"
            unsigned.write_bytes(b"unsigned safari")
            signed.write_bytes(b"developer id signed and notarized safari")
            evidence = {
                "schema_version": 1,
                "platform": "apple",
                "provider": "apple_developer_id",
                "signature_valid": True,
                "signer": "Developer ID Application: Biomem Maintainer (ABCDE12345)",
                "team_id": "ABCDE12345",
                "bundle_ids": [
                    "com.bleedingdev.biomem.safari",
                    "com.bleedingdev.biomem.safari.Extension",
                ],
                "timestamp": {"status": "verified", "value": "2026-08-29T10:15:00+00:00"},
                "provider_receipt": "https://appstoreconnect.apple.com/notary/123",
                "notarization": {
                    "status": "Accepted",
                    "id": "notary-request-123",
                    "staple_valid": True,
                    "gatekeeper_status": "accepted",
                },
            }
            result = signing.verify_signing_evidence(
                channel="safari_public",
                provider="apple_developer_id",
                input_artifact=unsigned,
                expected_input_sha256=signing.sha256(unsigned),
                signed_artifact=signed,
                native_evidence=evidence,
                expected_signer="Developer ID Application: Biomem Maintainer (ABCDE12345)",
                expected_team_id="ABCDE12345",
                expected_bundle_ids=[
                    "com.bleedingdev.biomem.safari",
                    "com.bleedingdev.biomem.safari.Extension",
                ],
            )
            self.assertEqual(result["notarization"]["status"], "Accepted")
            wrong = json.loads(json.dumps(evidence))
            wrong["bundle_ids"] = ["com.example.wrong"]
            with self.assertRaisesRegex(signing.SigningEvidenceError, "bundle IDs"):
                signing.verify_signing_evidence(
                    channel="safari_public",
                    provider="apple_developer_id",
                    input_artifact=unsigned,
                    expected_input_sha256=signing.sha256(unsigned),
                    signed_artifact=signed,
                    native_evidence=wrong,
                    expected_signer="Developer ID Application: Biomem Maintainer (ABCDE12345)",
                    expected_team_id="ABCDE12345",
                    expected_bundle_ids=[
                        "com.bleedingdev.biomem.safari",
                        "com.bleedingdev.biomem.safari.Extension",
                    ],
                )

    def test_readiness_chain_receives_only_nonsecret_presence_signals(self) -> None:
        workflow_paths = [
            ROOT / ".github/workflows/release.yml",
            ROOT / ".github/workflows/release-optional.yml",
            ROOT / ".github/workflows/publish-browser-channels.yml",
            ROOT / ".github/workflows/release-sign-windows.yml",
            ROOT / ".github/workflows/release-sign-apple.yml",
        ]
        workflows = {
            path.name: path.read_text(encoding="utf-8") for path in workflow_paths
        }
        readiness = (
            workflows["release-sign-windows.yml"]
            + workflows["release-sign-apple.yml"]
        )
        chain = "\n".join(workflows.values())
        raw_signing_secrets = (
            "SIGNPATH_API_TOKEN",
            "APPLE_TEAM_ID",
            "APPLE_DEVELOPER_ID_P12_BASE64",
            "APPLE_DEVELOPER_ID_P12_PASSWORD",
            "APPLE_DEVELOPER_ID_INSTALLER_P12_BASE64",
            "APPLE_DEVELOPER_ID_INSTALLER_P12_PASSWORD",
            "APPLE_SAFARI_HOST_PROFILE_BASE64",
            "APPLE_SAFARI_EXTENSION_PROFILE_BASE64",
            "APPLE_NOTARY_KEY_BASE64",
            "APPLE_NOTARY_KEY_ID",
            "APPLE_NOTARY_ISSUER_ID",
        )
        for secret in raw_signing_secrets:
            self.assertNotIn(secret, chain)
        self.assertNotIn("${{ secrets.", readiness)
        self.assertNotIn("    secrets:\n", workflows["release-optional.yml"])
        self.assertIn(
            "vars.SIGNPATH_CREDENTIALS_CONFIGURED == 'true'",
            workflows["release.yml"],
        )
        self.assertIn(
            "vars.APPLE_COMMON_CREDENTIALS_CONFIGURED == 'true'",
            workflows["release.yml"],
        )
        self.assertIn("PROVIDER_CREDENTIALS_CONFIGURED", readiness)
        self.assertIn("CHANNEL_CREDENTIALS_CONFIGURED", readiness)
        self.assertNotIn("WINDOWS_CODE_SIGNING_PFX", chain)
        self.assertNotIn("environment: release", chain)
        self.assertNotIn("secrets: inherit", chain)

    def test_readiness_workflows_make_no_signing_provider_call(self) -> None:
        for filename in ("release-sign-windows.yml", "release-sign-apple.yml"):
            text = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
            lowered = text.lower()
            for provider_call in (
                "invoke-restmethod",
                "invoke-webrequest",
                "curl ",
                "api.signpath",
                "notarytool submit",
                "xcrun notarytool",
            ):
                self.assertNotIn(provider_call, lowered)
            uses = [
                line.strip().removeprefix("uses: ")
                for line in text.splitlines()
                if line.strip().startswith("uses: ")
            ]
            self.assertTrue(uses)
            self.assertTrue(all(action.startswith("actions/") for action in uses))
            self.assertIn("--adapter-enabled false", text)


if __name__ == "__main__":
    unittest.main()
