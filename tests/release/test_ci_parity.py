from __future__ import annotations

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github/workflows/ci.yml"


class CIParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ci = CI.read_text(encoding="utf-8")
        cls.smoke = (ROOT / "tests/release/ci_platform_smoke.py").read_text(
            encoding="utf-8"
        )

    def test_appveyor_and_slack_surface_are_retired(self) -> None:
        self.assertFalse((ROOT / "appveyor.yml").exists())
        self.assertNotIn("AppVeyor", (ROOT / "Makefile").read_text(encoding="utf-8"))
        self.assertNotIn("Slack", self.ci)

    def test_make_build_targets_use_the_src_project_and_repository_dist(self) -> None:
        expected = "python3 -m build src --wheel --outdir dist"
        for target in ("wheel", "build-windows"):
            with self.subTest(target=target):
                completed = subprocess.run(
                    ["make", "-n", target],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(expected, completed.stdout.strip())

        linux = subprocess.run(
            ["make", "-n", "build-linux"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, linux.returncode, linux.stderr)
        self.assertIn("python -m build src --wheel --outdir dist", linux.stdout)

    def test_normal_ci_builds_and_checks_exact_python_distributions(self) -> None:
        package = self.ci.split("  package:\n", 1)[1].split(
            "  python-compatibility:\n", 1
        )[0]
        self.assertIn("python -m build src --sdist --wheel --outdir dist", package)
        self.assertIn("assert len(artifacts) == 2", package)
        self.assertIn("assert len(wheels) == 1", package)
        self.assertIn("assert len(sdists) == 1", package)
        self.assertIn("python -m twine check dist/*", package)

    def test_declared_python_versions_have_a_cheap_compatibility_matrix(self) -> None:
        compatibility = self.ci.split("  python-compatibility:\n", 1)[1].split(
            "  platform-smoke:\n", 1
        )[0]
        self.assertIn("python: ['3.10', '3.11', '3.12', '3.13', '3.14']", compatibility)
        self.assertIn("python -m build src --wheel --outdir dist", compatibility)
        self.assertIn('"torch==2.12.1+cpu"', compatibility)
        self.assertIn("https://download.pytorch.org/whl/cpu", compatibility)
        self.assertIn("python -m pip install dist/*.whl", compatibility)
        self.assertNotIn("--no-deps", compatibility)
        self.assertIn("python -m pip check", compatibility)
        self.assertIn("python -m compileall -q src", compatibility)
        self.assertIn("import importlib.metadata as m, memory_module, torch", compatibility)
        self.assertIn("Path('src').resolve() not in Path(memory_module.__file__).resolve().parents", compatibility)
        self.assertIn("create_parser().parse_args", compatibility)
        self.assertIn("torch.version.cuda is None", compatibility)

    def test_current_five_runner_matrix_executes_the_product_smoke(self) -> None:
        platform = self.ci.split("  platform-smoke:\n", 1)[1].split(
            "  extensions:\n", 1
        )[0]
        for target in (
            "linux-x86_64",
            "linux-arm64",
            "windows-x86_64",
            "macos-x86_64",
            "macos-arm64",
        ):
            self.assertEqual(1, platform.count(f"target: {target}"))
        self.assertIn("python -m pip check", platform)
        self.assertIn("python tests/test_smoke.py", platform)
        self.assertIn("python tests/release/ci_platform_smoke.py", platform)

    def test_extension_ci_validates_the_actual_unsigned_firefox_output(self) -> None:
        extensions = self.ci.split("  extensions:\n", 1)[1].split(
            "  safari-development-build:\n", 1
        )[0]
        self.assertIn("unzip -t dist/firefox-ci-unsigned.xpi", extensions)
        self.assertNotIn("unzip -t dist/firefox-ci.xpi", extensions)

    def test_smoke_covers_cpu_cli_session_settings_container_and_security(self) -> None:
        for contract in (
            "torch.version.cuda is None",
            "not torch.cuda.is_available()",
            "create_parser().parse_args",
            "SessionCache(ttl_seconds=60)",
            "SettingsManager(data_dir",
            "SecurityManager(data_dir",
            "BDBMContainer()",
            "save_bdbm(",
            "load_bdbm(",
        ):
            self.assertIn(contract, self.smoke)

    def test_every_external_action_is_pinned_to_an_immutable_commit(self) -> None:
        action_lines = [
            line.strip() for line in self.ci.splitlines() if line.strip().startswith("uses:")
        ]
        self.assertGreater(len(action_lines), 0)
        for line in action_lines:
            with self.subTest(line=line):
                self.assertRegex(
                    line,
                    re.compile(r"^uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}(?:\s+#.*)?$"),
                )


if __name__ == "__main__":
    unittest.main()
