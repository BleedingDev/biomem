from __future__ import annotations

import hashlib
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_policy = load_module("release_policy_for_macos_tests", ROOT / "scripts/release_policy.py")
formula = load_module(
    "generate_homebrew_formula_for_tests",
    ROOT / "scripts/release/generate_homebrew_formula.py",
)
canonical = load_module(
    "canonical_artifacts_for_macos_tests",
    ROOT / "scripts/release/canonical_artifacts.py",
)


class MacOSChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = release_policy.resolve_policy("v0.0.2", "none", False)
        self.policy_path = self.root / "release-policy.json"
        release_policy.write_json(self.policy_path, self.policy)
        self.digests = {
            item["name"]: hashlib.sha256(item["name"].encode("utf-8")).hexdigest()
            for item in self.policy["expected_core_artifacts"]
        }
        self.checksums_path = self.root / "SHA256SUMS.txt"
        self.write_checksums(self.digests)
        self.output = self.root / "Formula/biomem.rb"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_checksums(self, digests: dict[str, str]) -> None:
        self.checksums_path.write_text(
            "".join(f"{digest}  {name}\n" for name, digest in digests.items()),
            encoding="utf-8",
        )

    def generate(self) -> str:
        formula.generate(
            self.policy_path,
            self.checksums_path,
            "BleedingDev/biomem",
            self.output,
        )
        return self.output.read_text(encoding="utf-8")

    def test_formula_is_deterministic_versioned_and_architecture_pinned(self) -> None:
        first = self.generate()
        second = self.generate()
        self.assertEqual(first, second)
        self.assertIn(
            "https://github.com/BleedingDev/biomem/releases/download/v0.0.2/"
            "biomem-macos-arm64.tar.gz",
            first,
        )
        self.assertIn(
            "https://github.com/BleedingDev/biomem/releases/download/v0.0.2/"
            "biomem-macos-x86_64.tar.gz",
            first,
        )
        for target in ("macos-arm64", "macos-x86_64"):
            name = f"biomem-{target}.tar.gz"
            self.assertIn(f'sha256 "{self.digests[name]}"', first)
        self.assertNotIn("/latest/", first)
        self.assertNotIn("version :latest", first)
        self.assertNotRegex(first, r"@[A-Z0-9_]+@")

    def test_formula_installs_only_owned_archive_files_and_has_no_uninstall_hook(self) -> None:
        rendered = self.generate()
        self.assertIn('bin.install "biomem"', rendered)
        self.assertIn('prefix.install "LICENSE", "VERSION", "VERIFY.txt"', rendered)
        self.assertNotIn("def uninstall", rendered)
        for forbidden in (
            "Application Support",
            ".biomem",
            "xattr",
            "quarantine",
            "pkg",
            "cask",
            "notar",
            "Developer ID",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_rejects_missing_extra_duplicate_or_malformed_checksums(self) -> None:
        mutations = []
        missing = dict(self.digests)
        missing.pop("biomem-macos-arm64.tar.gz")
        mutations.append("".join(f"{digest}  {name}\n" for name, digest in missing.items()))
        mutations.append(self.checksums_path.read_text() + f"{'0' * 64}  stale.tar.gz\n")
        first_name, first_digest = next(iter(self.digests.items()))
        mutations.append(
            self.checksums_path.read_text() + f"{first_digest}  {first_name}\n"
        )
        mutations.append(self.checksums_path.read_text().replace(first_digest, first_digest.upper(), 1))
        for value in mutations:
            with self.subTest(value=value[-90:]):
                self.checksums_path.write_text(value, encoding="utf-8")
                with self.assertRaises(formula.FormulaError):
                    formula.generate(
                        self.policy_path,
                        self.checksums_path,
                        "BleedingDev/biomem",
                        self.output,
                    )
                self.write_checksums(self.digests)

    def test_rejects_repository_injection_and_noncanonical_policy(self) -> None:
        for repository in ("BleedingDev", "../biomem", 'BleedingDev/biomem"; system("id")'):
            with self.subTest(repository=repository):
                with self.assertRaises(formula.FormulaError):
                    formula.generate(
                        self.policy_path,
                        self.checksums_path,
                        repository,
                        self.output,
                    )
        value = json.loads(self.policy_path.read_text(encoding="utf-8"))
        value["tag"] = "latest"
        release_policy.write_json(self.policy_path, value)
        with self.assertRaises(ValueError):
            formula.generate(
                self.policy_path,
                self.checksums_path,
                "BleedingDev/biomem",
                self.output,
            )

    def test_rejects_incomplete_template_and_symlink_output(self) -> None:
        incomplete = self.root / "incomplete.rb.in"
        incomplete.write_text('class Biomem < Formula\n  homepage "@REPOSITORY@"\nend\n')
        with self.assertRaisesRegex(formula.FormulaError, "exactly once"):
            formula.generate(
                self.policy_path,
                self.checksums_path,
                "BleedingDev/biomem",
                self.output,
                incomplete,
            )
        target = self.root / "outside.rb"
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.symlink_to(target)
        with self.assertRaisesRegex(formula.FormulaError, "regular file path"):
            formula.generate(
                self.policy_path,
                self.checksums_path,
                "BleedingDev/biomem",
                self.output,
            )
        self.assertFalse(target.exists())

    def test_generated_formula_has_valid_ruby_syntax_when_ruby_is_available(self) -> None:
        self.generate()
        try:
            completed = subprocess.run(
                ["ruby", "-c", str(self.output)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError:
            self.skipTest("Ruby is not installed")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("Syntax OK", completed.stdout)

    def test_canonical_extractor_rejects_unsafe_tar_member_before_writing(self) -> None:
        archive = self.root / "biomem-macos-arm64.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            value = b"escape"
            member = tarfile.TarInfo("../escape")
            member.size = len(value)
            bundle.addfile(member, io.BytesIO(value))
        destination = self.root / "extracted"
        with self.assertRaisesRegex(canonical.ArtifactError, "unsafe"):
            canonical.verify_archive(archive, "macos-arm64", "0.0.2", destination)
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(list(destination.iterdir()), [])

    def test_workflow_requires_both_macos_architectures_and_real_channel_smokes(self) -> None:
        workflow = (ROOT / ".github/workflows/test-macos-channels.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "macos-15",
            "macos-15-intel",
            "macos-arm64",
            "macos-x86_64",
            "codesign --verify --strict",
            "Signature=adhoc",
            "brew install",
            "brew fetch --force",
            "brew test",
            "brew upgrade",
            "brew uninstall",
            "canonical_artifacts.verify_archive",
            "biomem v0.0.0",
            '[[ "$old_keg" != "$new_keg" ]]',
            '[[ "$old_target" != "$new_target" ]]',
            '[[ "$old_digest" != "$new_digest" ]]',
            "BLOCKED_ENVIRONMENT",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("repository_dispatch", workflow)
        self.assertNotIn("xattr -d", workflow)
        self.assertNotIn("tar -xzf", workflow)


if __name__ == "__main__":
    unittest.main()
