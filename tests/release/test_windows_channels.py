from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_policy = load_module(
    "release_policy_for_windows_tests", ROOT / "scripts/release_policy.py"
)
canonical = load_module(
    "canonical_artifacts_for_windows_tests", ROOT / "scripts/release/canonical_artifacts.py"
)
windows = load_module(
    "windows_channels_for_tests", ROOT / "scripts/release/generate_windows_channels.py"
)


class WindowsChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = release_policy.resolve_policy("v1.2.3", "winget,scoop", False)
        self.policy_path = self.root / "release-policy.json"
        release_policy.write_json(self.policy_path, self.policy)
        binary = self.root / "standalone.exe"
        binary.write_bytes(b"deterministic test executable")
        license_path = self.root / "LICENSE"
        license_path.write_text("MIT\n", encoding="utf-8")
        archive_dir = self.root / "archive"
        self.archive = canonical.package_archive(
            self.policy_path, "windows-x86_64", binary, license_path, archive_dir
        )
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def generate(self, name: str = "metadata") -> Path:
        output = self.root / name
        windows.generate(
            self.policy_path,
            "BleedingDev/biomem",
            self.archive,
            self.digest,
            output,
        )
        return output

    def test_generates_exact_hash_pinned_winget_and_scoop_contracts(self) -> None:
        output = self.generate()
        winget = output / "winget/1.2.3"
        self.assertEqual(
            {path.name for path in winget.iterdir()},
            {
                "BleedingDev.biomem.yaml",
                "BleedingDev.biomem.installer.yaml",
                "BleedingDev.biomem.locale.en-US.yaml",
            },
        )
        installer = (winget / "BleedingDev.biomem.installer.yaml").read_text()
        immutable_url = (
            "https://github.com/BleedingDev/biomem/releases/download/"
            "v1.2.3/biomem-windows-x86_64.zip"
        )
        for value in (
            "PackageIdentifier: BleedingDev.biomem",
            "InstallerType: zip",
            "NestedInstallerType: portable",
            "Scope: user",
            "RelativeFilePath: biomem.exe",
            "PortableCommandAlias: biomem",
            f"InstallerUrl: {immutable_url}",
            f"InstallerSha256: {self.digest.upper()}",
            "ManifestVersion: 1.12.0",
        ):
            self.assertIn(value, installer)
        self.assertNotIn("SignatureSha256", installer)
        self.assertNotIn("MSIX", installer.upper())

        scoop = json.loads((output / "scoop/biomem.json").read_text())
        self.assertEqual(scoop["version"], "1.2.3")
        self.assertEqual(scoop["architecture"]["64bit"]["url"], immutable_url)
        self.assertEqual(scoop["architecture"]["64bit"]["hash"], self.digest)
        self.assertEqual(scoop["bin"], "biomem.exe")
        self.assertNotIn("installer", scoop)
        self.assertNotIn("uninstaller", scoop)

    def test_generation_is_byte_for_byte_deterministic(self) -> None:
        first = self.generate("first")
        second = self.generate("second")
        first_files = {
            path.relative_to(first): path.read_bytes()
            for path in first.rglob("*")
            if path.is_file()
        }
        second_files = {
            path.relative_to(second): path.read_bytes()
            for path in second.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)

    def test_hash_mismatch_and_malformed_hash_fail_before_writing(self) -> None:
        for index, digest in enumerate(("0" * 64, "abc", "g" * 64)):
            output = self.root / f"bad-hash-{index}"
            with self.assertRaisesRegex(windows.WindowsChannelError, "SHA-256"):
                windows.generate(
                    self.policy_path,
                    "BleedingDev/biomem",
                    self.archive,
                    digest,
                    output,
                )
            self.assertFalse(output.exists())

    def test_caller_cannot_substitute_a_mutable_or_changed_release_url(self) -> None:
        invalid_repositories = (
            "https://github.com/BleedingDev/biomem/releases/latest",
            "BleedingDev/biomem/releases/latest",
            "BleedingDev/biomem.git",
            "BleedingDev/../biomem",
        )
        for index, repository in enumerate(invalid_repositories):
            with self.assertRaisesRegex(windows.WindowsChannelError, "repository"):
                windows.generate(
                    self.policy_path,
                    repository,
                    self.archive,
                    self.digest,
                    self.root / f"bad-repository-{index}",
                )

    def test_rejects_wrong_asset_name_stale_output_and_tampered_policy(self) -> None:
        wrong_name = self.root / "latest.zip"
        wrong_name.write_bytes(self.archive.read_bytes())
        with self.assertRaisesRegex(windows.WindowsChannelError, "canonical asset"):
            windows.generate(
                self.policy_path,
                "BleedingDev/biomem",
                wrong_name,
                self.digest,
                self.root / "wrong-name",
            )

        stale = self.root / "stale"
        stale.mkdir()
        (stale / "old.json").write_text("{}")
        with self.assertRaisesRegex(windows.WindowsChannelError, "must be empty"):
            windows.generate(
                self.policy_path,
                "BleedingDev/biomem",
                self.archive,
                self.digest,
                stale,
            )

        tampered = dict(self.policy)
        tampered["package_identifiers"] = dict(tampered["package_identifiers"])
        tampered["package_identifiers"]["winget"] = "Other.biomem"
        tampered_path = self.root / "tampered-policy.json"
        release_policy.write_json(tampered_path, tampered)
        with self.assertRaisesRegex(windows.release_policy.PolicyError, "canonical"):
            windows.generate(
                tampered_path,
                "BleedingDev/biomem",
                self.archive,
                self.digest,
                self.root / "tampered-policy",
            )

    def test_rejects_an_archive_with_wrong_version_or_extra_member(self) -> None:
        invalid = self.root / windows.WINDOWS_ARCHIVE
        with zipfile.ZipFile(invalid, "w") as archive:
            archive.writestr("biomem.exe", b"binary")
            archive.writestr("LICENSE", b"MIT")
            archive.writestr("VERSION", b"9.9.9\n")
            archive.writestr("VERIFY.txt", b"verify")
            archive.writestr("unexpected.dll", b"extra")
        digest = hashlib.sha256(invalid.read_bytes()).hexdigest()
        with self.assertRaisesRegex(windows.canonical_artifacts.ArtifactError, "allowlist mismatch"):
            windows.generate(
                self.policy_path,
                "BleedingDev/biomem",
                invalid,
                digest,
                self.root / "invalid-archive",
            )

    def test_workflow_is_explicit_non_publishing_and_covers_both_lifecycles(self) -> None:
        workflow = (ROOT / ".github/workflows/test-windows-channels.yml").read_text()
        workflow_lines = [line.strip() for line in workflow.splitlines()]
        for command in (
            "winget validate --manifest",
            "winget install --id",
            "winget upgrade --id",
            "winget uninstall --id",
            "scoop install biomem-test/biomem",
            "scoop update biomem",
            "scoop uninstall biomem",
            "BLOCKED_ENVIRONMENT",
        ):
            self.assertIn(command, workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("wingetcreate submit", workflow)
        self.assertNotIn("gh pr create", workflow)
        self.assertNotIn("RunAsAdmin", workflow)
        self.assertNotIn("LocalManifestFiles", workflow)
        self.assertNotIn("scoop update biomem-test", workflow_lines)
        self.assertIn("scoop update", workflow_lines)
        self.assertIn("scoop update biomem", workflow_lines)
        self.assertLess(
            workflow_lines.index("scoop update"),
            workflow_lines.index("scoop update biomem"),
        )

        uninstall_line = workflow_lines.index(
            "winget uninstall --id $env:PACKAGE_ID --exact --scope user `"
        )
        self.assertEqual(
            workflow_lines[uninstall_line + 1],
            "--source winget --disable-interactivity --purge",
        )

        bootstrap_start = workflow.index("- name: Install Scoop for the current user")
        lifecycle_start = workflow.index(
            "- name: Validate Scoop manifest and exercise its lifecycle"
        )
        bootstrap = workflow[bootstrap_start:lifecycle_start]
        self.assertIn("try {", bootstrap)
        self.assertIn("} catch {", bootstrap)
        self.assertIn("if ($LASTEXITCODE -ne 0)", bootstrap)
        self.assertIn("BLOCKED_ENVIRONMENT: Scoop bootstrap failed", bootstrap)
        self.assertLess(
            bootstrap.index("BLOCKED_ENVIRONMENT: Scoop bootstrap failed"),
            bootstrap.index("exit 78"),
        )

        for scoop_command, failure in (
            ("scoop bucket add biomem-test $Bucket", "Scoop bucket registration failed"),
            ("scoop install biomem-test/biomem", "Scoop installation failed"),
            ("scoop update", "Scoop or bucket refresh failed"),
            ("scoop update biomem", "Scoop package upgrade failed"),
            ("scoop uninstall biomem", "Scoop uninstall failed"),
        ):
            command_index = workflow_lines.index(scoop_command)
            self.assertIn("$LASTEXITCODE -ne 0", workflow_lines[command_index + 1])
            self.assertIn(failure, workflow_lines[command_index + 1])


if __name__ == "__main__":
    unittest.main()
