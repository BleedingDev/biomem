from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_policy", ROOT / "scripts/release_policy.py")
assert SPEC and SPEC.loader
release_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_policy)

SOURCE_SHA = "a" * 40
RECEIPT = "https://github.example/releases/v1.2.3"


class ReleasePolicyTests(unittest.TestCase):
    def artifacts(self, directory: Path, policy: dict) -> None:
        for item in policy["expected_core_artifacts"]:
            (directory / item["name"]).write_bytes(item["name"].encode())
        (directory / "chrome-biomem.zip").write_bytes(b"chrome-store-input")

    def evidence(
        self,
        directory: Path,
        policy: dict,
        channel: str,
        status: str,
        reason: str,
        receipt: str | None = None,
    ) -> None:
        release_policy.write_json(
            directory / f"{channel}.json",
            release_policy.make_evidence(policy, channel, status, reason, receipt),
        )

    def provenance(self, directory: Path) -> Path:
        path = directory / "provenance.json"
        release_policy.write_json(path, {
            "status": "published",
            "source_sha": SOURCE_SHA,
            "provider": "github_actions_build_provenance",
            "receipt": "https://github.example/attestations/123",
        })
        return path

    def normal_manifest(self, selected: str = "none", selected_status: str = "published") -> dict:
        policy = release_policy.resolve_policy("v1.2.3", selected, False)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifacts = base / "artifacts"
            evidence = base / "evidence"
            artifacts.mkdir()
            evidence.mkdir()
            self.artifacts(artifacts, policy)
            self.evidence(evidence, policy, "github_release", "published", "verified", RECEIPT)
            self.evidence(evidence, policy, "direct_cli", "published", "verified", RECEIPT)
            for channel in policy["selected_optional_channels"]:
                receipt = RECEIPT if selected_status == "published" else None
                self.evidence(
                    evidence, policy, channel, selected_status,
                    "verified" if receipt else "missing_credentials", receipt,
                )
            return release_policy.build_manifest(
                policy, evidence, artifacts, SOURCE_SHA, self.provenance(base)
            )

    def test_v0_and_v1_use_the_same_core_without_a_signing_gate(self) -> None:
        alpha = release_policy.resolve_policy("v0.9.0", "none", False)
        stable = release_policy.resolve_policy("v1.0.0", "none", False)
        self.assertTrue(alpha["prerelease"])
        self.assertFalse(stable["prerelease"])
        self.assertEqual(
            [item["kind"] for item in alpha["expected_core_artifacts"]],
            [item["kind"] for item in stable["expected_core_artifacts"]],
        )
        self.assertEqual(stable["selected_optional_channels"], [])

    def test_stable_names_versions_targets_and_package_ids_are_canonical(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "winget,scoop", False)
        self.assertEqual(policy["package_identifiers"]["winget"], "BleedingDev.biomem")
        self.assertEqual(policy["package_identifiers"]["pypi"], "biomem-memory")
        self.assertEqual(policy["expected_core_artifacts"][0]["name"],
                         "biomem_memory-1.2.3-py3-none-any.whl")
        self.assertIn(
            "biomem-windows-x86_64.zip",
            [item["name"] for item in policy["expected_core_artifacts"]],
        )
        canonical = release_policy.expected_canonical_artifacts(
            policy["version"], release_policy.load_contract(), include_firefox_xpi=False,
        )
        self.assertEqual(canonical[-1]["name"], "chrome-biomem.zip")

    def test_no_secret_v1_core_manifest_is_valid(self) -> None:
        manifest = self.normal_manifest()
        release_policy.validate_manifest(manifest)
        self.assertEqual(manifest["channels"]["github_release"]["status"], "published")
        self.assertEqual(manifest["channels"]["windows_signed"]["status"],
                         "skipped_not_configured")

    def test_selected_missing_credentials_is_blocked_but_core_is_published(self) -> None:
        manifest = self.normal_manifest("firefox_amo", "blocked_environment")
        release_policy.validate_manifest(manifest)
        self.assertEqual(manifest["channels"]["github_release"]["status"], "published")
        self.assertEqual(manifest["channels"]["firefox_amo"]["reason_code"],
                         "missing_credentials")
        with self.assertRaisesRegex(release_policy.PolicyError, "firefox_amo"):
            release_policy.enforce_selected(manifest)

    def test_exact_firefox_attachment_is_a_versioned_canonical_subject(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "firefox_amo", False)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifacts = base / "artifacts"
            evidence = base / "evidence"
            artifacts.mkdir()
            evidence.mkdir()
            self.artifacts(artifacts, policy)
            filename = "firefox-biomem-1.2.3-amo-signed.xpi"
            (artifacts / filename).write_bytes(b"signed-xpi")
            self.evidence(evidence, policy, "github_release", "published", "verified", RECEIPT)
            self.evidence(evidence, policy, "direct_cli", "published", "verified", RECEIPT)
            self.evidence(
                evidence,
                policy,
                "firefox_amo",
                "published",
                release_policy.FIREFOX_ATTACHMENT_REASON,
                f"https://github.com/example/biomem/releases/download/v1.2.3/{filename}",
            )
            manifest = release_policy.build_manifest(
                policy, evidence, artifacts, SOURCE_SHA, self.provenance(base)
            )
        names = [item["name"] for item in manifest["artifacts"]]
        self.assertEqual(names[-2:], ["chrome-biomem.zip", filename])
        self.assertEqual(manifest["provenance"]["subjects"], names)
        release_policy.validate_manifest(manifest)

    def test_missing_selected_internal_evidence_is_failed_not_success(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "pypi", False)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifacts = base / "artifacts"
            evidence = base / "evidence"
            artifacts.mkdir()
            evidence.mkdir()
            self.artifacts(artifacts, policy)
            self.evidence(evidence, policy, "github_release", "published", "verified", RECEIPT)
            self.evidence(evidence, policy, "direct_cli", "published", "verified", RECEIPT)
            manifest = release_policy.build_manifest(
                policy, evidence, artifacts, SOURCE_SHA, self.provenance(base)
            )
        self.assertEqual(manifest["channels"]["pypi"]["status"], "failed")
        self.assertEqual(manifest["channels"]["pypi"]["reason_code"],
                         "missing_internal_evidence")

    def test_dry_run_is_preflight_only_and_has_no_publication_claims(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "pypi,windows_signed", True)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            artifacts = base / "artifacts"
            evidence = base / "evidence"
            artifacts.mkdir()
            evidence.mkdir()
            self.artifacts(artifacts, policy)
            preflight = release_policy.build_preflight(policy, evidence, artifacts, SOURCE_SHA)
            self.assertEqual(preflight["execution_mode"], "dry_run")
            self.assertNotIn("published", json.dumps(preflight))
            self.assertEqual(preflight["channels"]["pypi"]["reason_code"],
                             "not_attempted_dry_run")
            with self.assertRaisesRegex(release_policy.PolicyError, "preflight"):
                release_policy.build_manifest(policy, evidence, artifacts, SOURCE_SHA)

    def test_artifact_allowlist_rejects_missing_extra_and_symlink_files(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "none", True)
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            self.artifacts(artifacts, policy)
            release_policy.verify_canonical_artifacts(
                policy, artifacts, include_firefox_xpi=False,
            )
            (artifacts / "unsigned-development-only.zip").write_text("unsafe")
            with self.assertRaisesRegex(release_policy.PolicyError, "unexpected"):
                release_policy.verify_canonical_artifacts(
                    policy, artifacts, include_firefox_xpi=False,
                )
            (artifacts / "unsigned-development-only.zip").unlink()
            expected = artifacts / policy["expected_core_artifacts"][0]["name"]
            expected.unlink()
            expected.symlink_to(artifacts / policy["expected_core_artifacts"][1]["name"])
            with self.assertRaisesRegex(release_policy.PolicyError, "non-regular"):
                release_policy.verify_canonical_artifacts(
                    policy, artifacts, include_firefox_xpi=False,
                )

    def test_invalid_tag_source_sha_and_unselected_success_are_rejected(self) -> None:
        with self.assertRaises(release_policy.PolicyError):
            release_policy.resolve_policy("1.2.3", "none", False)
        with self.assertRaises(release_policy.PolicyError):
            release_policy.validate_source_sha("")
        policy = release_policy.resolve_policy("v1.2.3", "none", False)
        with self.assertRaisesRegex(release_policy.PolicyError, "selected"):
            release_policy.make_evidence(policy, "pypi", "published", "fake", RECEIPT)

    def test_published_requires_receipts_and_manifest_validation_does_not_trust_flags(self) -> None:
        policy = release_policy.resolve_policy("v1.2.3", "none", False)
        with self.assertRaisesRegex(release_policy.PolicyError, "receipt"):
            release_policy.make_evidence(policy, "github_release", "published", "fake")
        manifest = self.normal_manifest()
        forged = copy.deepcopy(manifest)
        forged["channels"]["pypi"]["selected"] = True
        forged["channels"]["pypi"]["status"] = "published"
        forged["channels"]["pypi"]["receipt"] = RECEIPT
        with self.assertRaisesRegex(release_policy.PolicyError, "selected flag"):
            release_policy.validate_manifest(forged)

    def test_manifest_requires_exact_channels_states_artifacts_and_provenance(self) -> None:
        manifest = self.normal_manifest()
        for mutation, message in (
            (lambda value: value["channels"].pop("scoop"), "every expected channel"),
            (lambda value: value["channels"]["scoop"].update(status="success"), "status"),
            (lambda value: value["artifacts"][0].update(name="wrong.whl"), "artifact names"),
            (lambda value: value["provenance"].update(receipt=""), "provenance"),
        ):
            changed = copy.deepcopy(manifest)
            mutation(changed)
            with self.assertRaisesRegex(release_policy.PolicyError, message):
                release_policy.validate_manifest(changed)

    def test_core_workflow_has_no_release_secrets_or_protected_environment(self) -> None:
        core = (ROOT / ".github/workflows/release-core.yml").read_text()
        orchestrator = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("secrets.", core)
        self.assertNotIn("environment: release", core)
        self.assertNotIn("strict-signing", orchestrator.lower())
        self.assertIn("source_sha: ${{ needs.resolve.outputs.source_sha }}", orchestrator)
        self.assertIn("python -m pytest -q", core)

    def test_every_safari_xcode_target_uses_the_alpha_marketing_version(self) -> None:
        project = (
            ROOT
            / "extensions/safari-xcode/BDBM Memory Plugin/"
            "BDBM Memory Plugin.xcodeproj/project.pbxproj"
        ).read_text()
        versions = [
            line.split("=", 1)[1].strip(" ;\t")
            for line in project.splitlines()
            if "MARKETING_VERSION =" in line
        ]
        self.assertGreater(len(versions), 0)
        self.assertEqual({"0.0.2"}, set(versions))


if __name__ == "__main__":
    unittest.main()
