from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile
import hashlib


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_policy = load_module("release_policy_for_canonical_tests", ROOT / "scripts/release_policy.py")
canonical = load_module(
    "canonical_artifacts_for_tests", ROOT / "scripts/release/canonical_artifacts.py"
)

SOURCE_SHA = "a" * 40


class CanonicalArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = release_policy.resolve_policy("v1.2.3", "none", False)
        self.policy_path = self.root / "policy.json"
        release_policy.write_json(self.policy_path, self.policy)
        self.license = self.root / "LICENSE"
        self.license.write_text("test license\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_core_bundles(self) -> tuple[Path, Path]:
        bundles = self.root / "bundles"
        digests = self.root / "digests"
        bundles.mkdir()
        digests.mkdir()
        by_producer: dict[str, list[Path]] = {"python": []}
        for target in self.policy["targets"]:
            by_producer[target["target"]] = []
        for artifact in self.policy["expected_core_artifacts"]:
            producer = "python" if artifact["kind"].startswith("python_") else artifact["platform"]
            bundle = bundles / f"release-core-{producer}"
            bundle.mkdir(exist_ok=True)
            path = bundle / artifact["name"]
            path.write_bytes((artifact["name"] + "\n").encode("utf-8"))
            by_producer[producer].append(path)
        for producer, paths in by_producer.items():
            directory = digests / f"producer-digest-{producer}"
            directory.mkdir()
            canonical.record_digest(
                producer, SOURCE_SHA, directory / f"producer-digest-{producer}.json", paths
            )
        return bundles, digests

    def make_core(self) -> Path:
        core = self.root / "core"
        core.mkdir()
        for artifact in self.policy["expected_core_artifacts"]:
            (core / artifact["name"]).write_bytes(artifact["name"].encode("utf-8"))
        return core

    def make_canonical(self) -> Path:
        artifacts = self.make_core()
        (artifacts / "chrome-biomem.zip").write_bytes(b"chrome-store-input")
        return artifacts

    def write_optional_evidence(self, directory: Path, policy: dict) -> None:
        contract = release_policy.load_contract()
        selected = set(policy["selected_optional_channels"])
        for channel, definition in contract["channels"].items():
            if definition["selection"] != "explicit":
                continue
            if channel in selected:
                status, reason = "blocked_environment", "missing_credentials"
            else:
                status, reason = "skipped_not_configured", "not_selected"
            release_policy.write_json(
                directory / f"{channel}.json",
                release_policy.make_evidence(policy, channel, status, reason),
            )

    def make_browser_downloads(
        self, *, attach_firefox: bool,
    ) -> tuple[Path, Path, Path]:
        chrome_downloads = self.root / "chrome-downloads"
        firefox_downloads = self.root / "firefox-downloads"
        evidence = self.root / "browser-evidence"
        chrome_downloads.mkdir()
        firefox_downloads.mkdir()
        evidence.mkdir()
        chrome_bytes = b"chrome-store-input"
        chrome_sha = hashlib.sha256(chrome_bytes).hexdigest()
        firefox_input_sha = "b" * 64
        chrome_artifact = chrome_downloads / "browser-ready-chrome-current-123-2"
        chrome_artifact.mkdir()
        (chrome_artifact / "chrome-biomem.zip").write_bytes(chrome_bytes)
        release_policy.write_json(chrome_artifact / "verified-chrome.json", {
            "filename": "chrome-biomem.zip",
            "run_attempt": 2,
            "run_id": 123,
            "sha256": chrome_sha,
            "size": len(chrome_bytes),
            "source_sha": SOURCE_SHA,
            "tag": "v1.2.3",
            "version": "1.2.3",
        })
        policy = (
            release_policy.resolve_policy("v1.2.3", "firefox_amo", False)
            if attach_firefox else self.policy
        )
        if attach_firefox:
            status = "blocked_environment"
            reason = "signed_output_awaiting_release_attachment"
        elif "firefox_amo" in policy["selected_optional_channels"]:
            status = "blocked_environment"
            reason = "missing_credentials"
        else:
            status = "skipped_not_configured"
            reason = "not_selected"
        self.write_optional_evidence(evidence, policy)
        release_policy.write_json(
            evidence / "firefox_amo.json",
            release_policy.make_evidence(policy, "firefox_amo", status, reason),
        )
        handoff = firefox_downloads / "browser-firefox-current-123-2"
        handoff.mkdir()
        envelope = {
            "filename": None,
            "input_sha256": firefox_input_sha,
            "ready": attach_firefox,
            "reason": reason,
            "run_attempt": 2,
            "run_id": 123,
            "sha256": None,
            "size": None,
            "source_sha": SOURCE_SHA,
            "status": status,
            "tag": "v1.2.3",
            "version": "1.2.3",
        }
        if attach_firefox:
            signed = handoff / "firefox-biomem-1.2.3-amo-signed.xpi"
            signed.write_bytes(b"signed-firefox-xpi")
            signed_sha = hashlib.sha256(signed.read_bytes()).hexdigest()
            release_policy.write_json(handoff / "verified-firefox-amo.json", {
                "filename": signed.name,
                "input_sha256": firefox_input_sha,
                "signed_sha256": signed_sha,
                "size": signed.stat().st_size,
                "source_sha": SOURCE_SHA,
                "tag": "v1.2.3",
                "version": "1.2.3",
            })
            envelope.update({
                "filename": signed.name,
                "sha256": signed_sha,
                "size": signed.stat().st_size,
            })
        release_policy.write_json(handoff / "firefox-handoff.json", envelope)
        return chrome_downloads, firefox_downloads, evidence

    def test_windows_zip_and_macos_tar_are_deterministic_and_exact(self) -> None:
        binary = self.root / "biomem"
        binary.write_bytes(b"standalone-binary")
        first = self.root / "first"
        second = self.root / "second"
        windows_a = canonical.package_archive(
            self.policy_path, "windows-x86_64", binary, self.license, first
        )
        windows_b = canonical.package_archive(
            self.policy_path, "windows-x86_64", binary, self.license, second
        )
        self.assertEqual(windows_a.read_bytes(), windows_b.read_bytes())
        canonical.verify_archive(windows_a, "windows-x86_64", "1.2.3")

        mac_a = canonical.package_archive(
            self.policy_path, "macos-arm64", binary, self.license, first
        )
        mac_b = canonical.package_archive(
            self.policy_path, "macos-arm64", binary, self.license, second
        )
        self.assertEqual(mac_a.read_bytes(), mac_b.read_bytes())
        canonical.verify_archive(mac_a, "macos-arm64", "1.2.3")

    def test_archive_symlink_member_is_rejected(self) -> None:
        archive_path = self.root / "biomem-windows-x86_64.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name in ("LICENSE", "VERSION", "VERIFY.txt"):
                archive.writestr(name, "1.2.3\n" if name == "VERSION" else "value")
            link = zipfile.ZipInfo("biomem.exe")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "target")
        with self.assertRaisesRegex(canonical.ArtifactError, "non-regular"):
            canonical.verify_archive(archive_path, "windows-x86_64", "1.2.3")

    def test_assembly_accepts_exact_producer_digests_and_rejects_tampering(self) -> None:
        bundles, digests = self.make_core_bundles()
        output = self.root / "assembled"
        canonical.assemble(self.policy_path, SOURCE_SHA, bundles, digests, output)
        inventory = release_policy.verify_artifacts(self.policy, output)
        self.assertEqual(len(inventory), len(self.policy["expected_core_artifacts"]))

        digest_path = digests / "producer-digest-python/producer-digest-python.json"
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
        digest["artifacts"][0]["sha256"] = "0" * 64
        release_policy.write_json(digest_path, digest)
        with self.assertRaisesRegex(canonical.ArtifactError, "digest mismatch"):
            canonical.assemble(
                self.policy_path, SOURCE_SHA, bundles, digests, self.root / "tampered-output"
            )
        digest["artifacts"].append(dict(digest["artifacts"][0]))
        release_policy.write_json(digest_path, digest)
        with self.assertRaisesRegex(canonical.ArtifactError, "allowlist mismatch"):
            canonical.assemble(
                self.policy_path, SOURCE_SHA, bundles, digests, self.root / "duplicate-output"
            )

    def test_assembly_rejects_extra_collision_and_symlink_inputs(self) -> None:
        bundles, digests = self.make_core_bundles()
        python_bundle = bundles / "release-core-python"
        (python_bundle / "stale.zip").write_bytes(b"stale")
        with self.assertRaisesRegex(canonical.ArtifactError, "allowlist mismatch"):
            canonical.assemble(
                self.policy_path, SOURCE_SHA, bundles, digests, self.root / "extra-output"
            )
        (python_bundle / "stale.zip").unlink()
        artifact = next(python_bundle.iterdir())
        artifact.unlink()
        artifact.symlink_to(self.license)
        with self.assertRaisesRegex(canonical.ArtifactError, "non-regular"):
            canonical.assemble(
                self.policy_path, SOURCE_SHA, bundles, digests, self.root / "link-output"
            )

    def test_payload_manifest_and_checksums_cover_the_exact_core(self) -> None:
        core = self.make_canonical()
        evidence = self.root / "evidence"
        evidence.mkdir()
        self.write_optional_evidence(evidence, self.policy)
        provenance = self.root / "provenance.json"
        release_policy.write_json(provenance, {
            "status": "published",
            "source_sha": SOURCE_SHA,
            "provider": "github_actions_build_provenance",
            "receipt": "https://github.example/attestations/123",
        })
        publication = self.root / "publication"
        canonical.prepare_payload(
            self.policy_path,
            SOURCE_SHA,
            "example/biomem",
            core,
            evidence,
            provenance,
            publication,
        )
        canonical.verify_payload(
            self.policy_path, SOURCE_SHA, "example/biomem", publication / "payload"
        )
        checksum_names = {
            line.split("  ", 1)[1]
            for line in (publication / "payload/SHA256SUMS.txt").read_text().splitlines()
        }
        self.assertEqual(
            checksum_names,
            {item["name"] for item in self.policy["expected_core_artifacts"]}
            | {"chrome-biomem.zip"},
        )
        notes = (publication / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn("gh attestation verify", notes)
        self.assertIn("gh release verify-asset", notes)
        self.assertIn("Alpha release", notes)
        self.assertIn(
            "https://github.com/example/biomem/blob/v1.2.3/"
            "docs/install-from-github-releases.md",
            notes,
        )
        self.assertIn("chrome-biomem.zip", notes)
        self.assertIn("Firefox XPI is absent unless Mozilla signing is verified", notes)
        (publication / "payload/SHA256SUMS.txt").write_text(
            "0" * 64 + "  stale.zip\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(canonical.ArtifactError, "does not exactly match"):
            canonical.verify_payload(
                self.policy_path, SOURCE_SHA, "example/biomem", publication / "payload"
            )

    def test_publication_assembly_always_includes_exact_chrome(self) -> None:
        core = self.make_core()
        chrome, firefox, evidence = self.make_browser_downloads(attach_firefox=False)
        output = self.root / "canonical"
        canonical.assemble_publication_assets(
            self.policy_path,
            SOURCE_SHA,
            "123",
            "2",
            core,
            chrome,
            firefox,
            evidence,
            output,
        )
        self.assertEqual(
            {item["name"] for item in self.policy["expected_core_artifacts"]}
            | {"chrome-biomem.zip"},
            {path.name for path in output.iterdir()},
        )

    def test_exact_firefox_trigger_requires_and_validates_current_run_handoff(self) -> None:
        self.policy = release_policy.resolve_policy("v1.2.3", "firefox_amo", False)
        release_policy.write_json(self.policy_path, self.policy)
        core = self.make_core()
        chrome, firefox, evidence = self.make_browser_downloads(attach_firefox=True)
        output = self.root / "canonical"
        canonical.assemble_publication_assets(
            self.policy_path,
            SOURCE_SHA,
            "123",
            "2",
            core,
            chrome,
            firefox,
            evidence,
            output,
        )
        self.assertTrue((output / "firefox-biomem-1.2.3-amo-signed.xpi").is_file())
        provenance = self.root / "firefox-provenance.json"
        release_policy.write_json(provenance, {
            "status": "published",
            "source_sha": SOURCE_SHA,
            "provider": "github_actions_build_provenance",
            "receipt": "https://github.example/attestations/firefox",
        })
        publication = self.root / "firefox-publication"
        canonical.prepare_payload(
            self.policy_path,
            SOURCE_SHA,
            "example/biomem",
            output,
            evidence,
            provenance,
            publication,
        )
        manifest = json.loads(
            (publication / "payload/release-manifest.json").read_text(encoding="utf-8")
        )
        notes = (publication / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn(
            "Mozilla-signed `firefox-biomem-1.2.3-amo-signed.xpi`",
            notes,
        )
        firefox_channel = manifest["channels"]["firefox_amo"]
        self.assertEqual("published", firefox_channel["status"])
        self.assertEqual(
            release_policy.FIREFOX_ATTACHMENT_REASON,
            firefox_channel["reason_code"],
        )
        self.assertEqual(
            "https://github.com/example/biomem/releases/download/v1.2.3/"
            "firefox-biomem-1.2.3-amo-signed.xpi",
            firefox_channel["receipt"],
        )
        self.assertEqual(
            [item["name"] for item in manifest["artifacts"]],
            manifest["provenance"]["subjects"],
        )

        metadata = (
            firefox
            / "browser-firefox-current-123-2"
            / "verified-firefox-amo.json"
        )
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["source_sha"] = "c" * 40
        release_policy.write_json(metadata, value)
        with self.assertRaisesRegex(canonical.ArtifactError, "metadata"):
            canonical.assemble_publication_assets(
                self.policy_path,
                SOURCE_SHA,
                "123",
                "2",
                core,
                chrome,
                firefox,
                evidence,
                self.root / "tampered-firefox",
            )

    def test_signed_handoff_with_other_evidence_is_rejected(self) -> None:
        chrome, firefox, evidence = self.make_browser_downloads(attach_firefox=True)
        release_policy.write_json(
            evidence / "firefox_amo.json",
            release_policy.make_evidence(
                self.policy, "firefox_amo", "skipped_not_configured", "not_selected",
            ),
        )
        with self.assertRaisesRegex(canonical.ArtifactError, "identity|without the exact"):
            canonical.assemble_publication_assets(
                self.policy_path,
                SOURCE_SHA,
                "123",
                "2",
                self.make_core(),
                chrome,
                firefox,
                evidence,
                self.root / "forbidden-firefox",
            )

    def test_other_blocked_firefox_state_keeps_core_and_chrome_publishable(self) -> None:
        self.policy = release_policy.resolve_policy("v1.2.3", "firefox_amo", False)
        release_policy.write_json(self.policy_path, self.policy)
        chrome, firefox, evidence = self.make_browser_downloads(attach_firefox=False)
        release_policy.write_json(
            evidence / "firefox_amo.json",
            release_policy.make_evidence(
                self.policy,
                "firefox_amo",
                "blocked_environment",
                "missing_credentials",
            ),
        )
        output = self.root / "blocked-firefox-assets"
        canonical.assemble_publication_assets(
            self.policy_path,
            SOURCE_SHA,
            "123",
            "2",
            self.make_core(),
            chrome,
            firefox,
            evidence,
            output,
        )
        self.assertFalse(any(path.suffix == ".xpi" for path in output.iterdir()))
        provenance = self.root / "blocked-firefox-provenance.json"
        release_policy.write_json(provenance, {
            "status": "published",
            "source_sha": SOURCE_SHA,
            "provider": "github_actions_build_provenance",
            "receipt": "https://github.example/attestations/blocked-firefox",
        })
        publication = self.root / "blocked-firefox-publication"
        canonical.prepare_payload(
            self.policy_path,
            SOURCE_SHA,
            "example/biomem",
            output,
            evidence,
            provenance,
            publication,
        )
        manifest = json.loads(
            (publication / "payload/release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "blocked_environment", manifest["channels"]["firefox_amo"]["status"]
        )
        with self.assertRaisesRegex(release_policy.PolicyError, "firefox_amo"):
            release_policy.enforce_selected(manifest)


if __name__ == "__main__":
    unittest.main()
