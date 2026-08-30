from __future__ import annotations

from pathlib import Path
import ast
import json
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PyPIChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = (
            ROOT / ".github/workflows/publish-pypi.yml"
        ).read_text(encoding="utf-8")
        self.metadata = tomllib.loads(
            (ROOT / "src/pyproject.toml").read_text(encoding="utf-8")
        )["project"]

    def job(self, name: str, next_name: str | None = None) -> str:
        section = self.workflow.split(f"  {name}:\n", 1)[1]
        if next_name:
            section = section.split(f"  {next_name}:\n", 1)[0]
        return section

    def python_block_after(self, marker: str) -> str:
        section = self.workflow.split(marker, 1)[1]
        block = section.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        return textwrap.dedent(block)

    def test_published_release_repository_dispatch_and_manual_dry_run_are_supported(self) -> None:
        self.assertIn("release:\n    types: [published]", self.workflow)
        self.assertIn(
            "repository_dispatch:\n    types: [canonical_release_published]",
            self.workflow,
        )
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("default: true\n        type: boolean", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)

    def test_repository_dispatch_payload_validation_fails_closed(self) -> None:
        script = self.python_block_after("Validate repository dispatch payload exactly")
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            event_path = Path(temporary) / "event.json"

            def run(payload: object, tag: str = "v0.0.2", sha: str = source_sha):
                event_path.write_text(json.dumps({"client_payload": payload}), encoding="utf-8")
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(event_path),
                        "repository_dispatch",
                        tag,
                        sha,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(run({"tag": "v0.0.2", "source_sha": source_sha}).returncode, 0)
            for payload, tag, sha in (
                ({"tag": "v0.0.2"}, "v0.0.2", source_sha),
                ({"tag": "v0.0.2", "source_sha": source_sha, "extra": True}, "v0.0.2", source_sha),
                ({"tag": "v0.0.2", "source_sha": "b" * 40}, "v0.0.2", source_sha),
                ({"tag": "latest", "source_sha": source_sha}, "latest", source_sha),
                ({"tag": "v0.0.2", "source_sha": "short"}, "v0.0.2", "short"),
            ):
                self.assertNotEqual(run(payload, tag, sha).returncode, 0)

    def test_every_external_action_is_immutable(self) -> None:
        uses = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", self.workflow)
        self.assertGreaterEqual(len(uses), 7)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_exact_released_distributions_are_verified_and_never_rebuilt(self) -> None:
        prepare = self.job("prepare", "publish")
        self.assertIn("gh release download", prepare)
        self.assertIn("release-manifest.json", prepare)
        self.assertIn("SHA256SUMS.txt", prepare)
        self.assertIn('item["kind"] in {"python_wheel", "python_sdist"}', prepare)
        self.assertIn("checksums != manifest_digests", prepare)
        self.assertIn("canonical distribution digest mismatch", prepare)
        self.assertIn('for artifact in dist/*', prepare)
        self.assertIn('gh attestation verify "$artifact"', prepare)
        self.assertNotIn("gh attestation verify dist/*", prepare)
        self.assertIn(
            '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release-publish.yml"',
            prepare,
        )
        self.assertIn('--source-digest "$source_sha"', prepare)
        self.assertIn("BLOCKED_ENVIRONMENT: GitHub attestation permission", prepare)
        self.assertIn("FAILED: GitHub provenance verification rejected", prepare)
        self.assertIn("python -m twine check dist/*", prepare)
        self.assertNotIn("python -m build", self.workflow)
        self.assertNotIn("twine upload", self.workflow)

    def test_oidc_permission_is_isolated_and_no_long_lived_token_exists(self) -> None:
        prepare = self.job("prepare", "publish")
        publish = self.job("publish", "post-publish")
        post_publish = self.job("post-publish")
        self.assertIn("permissions: {}", self.workflow)
        self.assertNotIn("id-token: write", prepare)
        self.assertEqual(publish.count("id-token: write"), 1)
        self.assertNotIn("contents: write", publish)
        self.assertIn("permissions: {}", post_publish)
        self.assertNotIn("PYPI_API_TOKEN", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^\s+(?:user|password):")
        self.assertIn("pypa/gh-action-pypi-publish@", publish)

    def test_dry_run_and_unselected_channel_cannot_reach_publication(self) -> None:
        publish = self.job("publish", "post-publish")
        self.assertIn("needs.prepare.outputs.selected == 'true'", publish)
        self.assertIn("needs.prepare.outputs.dry_run != 'true'", publish)
        self.assertIn("needs.prepare.outputs.remote_exact != 'true'", publish)
        self.assertIn("Dry-run preflight passed; no PyPI publication was attempted.", self.workflow)
        self.assertIn("SKIPPED_NOT_CONFIGURED", self.workflow)

    def test_missing_trusted_publisher_is_never_reported_as_success(self) -> None:
        publish = self.job("publish", "post-publish")
        self.assertIn("continue-on-error: true", publish)
        self.assertIn("if: steps.pypi.outcome == 'failure'", publish)
        self.assertIn("BLOCKED_ENVIRONMENT:", publish)
        self.assertIn("raise SystemExit(3)", publish)
        self.assertIn('"invalid-publisher"', publish)
        rejection = publish.split("Classify upload or provider rejection", 1)[1]
        self.assertIn("FAILED:", rejection)
        self.assertIn("exit 2", rejection)
        self.assertNotIn("BLOCKED_ENVIRONMENT:", rejection)
        self.assertNotIn("skip-existing", publish)

    def test_github_environment_and_release_failures_have_distinct_classification(self) -> None:
        prepare = self.job("prepare", "install-smoke")
        self.assertIn("HTTP 401|HTTP 403|authentication|permission", prepare)
        self.assertIn("BLOCKED_ENVIRONMENT: cannot read the canonical GitHub Release", prepare)
        self.assertIn("FAILED: canonical published GitHub Release does not exist", prepare)
        self.assertIn("BLOCKED_ENVIRONMENT: GitHub Release immutability capability", prepare)
        self.assertIn("FAILED: canonical GitHub Release is not immutable", prepare)
        self.assertIn("FAILED: canonical GitHub Release assets are missing or invalid", prepare)

    def test_retry_accepts_only_exact_remote_files_with_attestations(self) -> None:
        self.assertIn("if remote_files != local", self.workflow)
        self.assertIn("attestation_bundles", self.workflow)
        self.assertGreaterEqual(self.workflow.count("pypi-attestations verify pypi"), 2)
        self.assertGreaterEqual(self.workflow.count("--provenance-file"), 2)
        self.assertIn('"repository": "BleedingDev/biomem"', self.workflow)
        self.assertIn('"workflow": "publish-pypi.yml"', self.workflow)
        self.assertIn('"environment": "pypi"', self.workflow)
        self.assertIn("remote_exact=true", self.workflow)
        remote_check = self.workflow.split("Check whether the exact PyPI release already exists", 1)[1]
        self.assertLess(
            remote_check.index("pypi-attestations verify pypi"),
            remote_check.index("remote_exact=true"),
        )
        post_publish = self.job("post-publish")
        self.assertIn("needs.prepare.outputs.remote_exact == 'true'", post_publish)

    def test_actual_workflow_publisher_validator_rejects_wrong_identity(self) -> None:
        script = self.python_block_after("Check whether the exact PyPI release already exists")
        tree = ast.parse(script)
        validator_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "verify_publisher_identity"
        )
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[validator_node], type_ignores=[]), "<workflow>", "exec"), namespace)
        validator = namespace["verify_publisher_identity"]
        valid_publisher = {
            "environment": "pypi",
            "kind": "GitHub",
            "repository": "BleedingDev/biomem",
            "workflow": "publish-pypi.yml",
        }
        valid = {"attestation_bundles": [{"publisher": valid_publisher}]}
        validator(valid, "artifact.whl")  # type: ignore[operator]
        invalid_values = (
            {},
            {"attestation_bundles": []},
            {"attestation_bundles": [{"publisher": {**valid_publisher, "repository": "attacker/repo"}}]},
            {"attestation_bundles": [{"publisher": {**valid_publisher, "workflow": "other.yml"}}]},
            {"attestation_bundles": [{"publisher": {**valid_publisher, "environment": "other"}}]},
        )
        for invalid in invalid_values:
            with self.assertRaises(SystemExit):
                validator(invalid, "artifact.whl")  # type: ignore[operator]

    def test_post_publish_checks_pypi_and_smokes_uv_entrypoint(self) -> None:
        post_publish = self.job("post-publish")
        self.assertIn("https://pypi.org/pypi/", post_publish)
        self.assertIn("https://pypi.org/integrity/", post_publish)
        self.assertIn("uv tool install", post_publish)
        self.assertIn('"$(uv tool dir --bin)/biomem" --help', post_publish)
        self.assertIn("uv tool uninstall biomem-memory", post_publish)

    def test_uv_and_pipx_smoke_exact_wheel_and_sdist_on_three_operating_systems(self) -> None:
        smoke = self.job("install-smoke", "publish")
        self.assertIn("os: [ubuntu-24.04, windows-2025, macos-15]", smoke)
        self.assertEqual(smoke.count("for artifact in dist/*"), 2)
        for command in ("biomem", "biomem-server", "biomem-mcp"):
            self.assertIn(command, smoke)
        self.assertIn("uv tool install", smoke)
        self.assertIn("uv tool upgrade biomem-memory", smoke)
        self.assertIn("uv tool uninstall biomem-memory", smoke)
        self.assertIn("uvx pipx install", smoke)
        self.assertIn("uvx pipx upgrade biomem-memory", smoke)
        self.assertNotIn("uvx pipx reinstall biomem-memory", smoke)
        self.assertIn("uvx pipx uninstall biomem-memory", smoke)
        uv_step = smoke.split("Install, upgrade, and uninstall exact wheel and sdist with uv", 1)[1]
        uv_before, uv_after = uv_step.split("uv tool upgrade biomem-memory", 1)
        pipx_step = uv_after.split("Install, upgrade, and uninstall exact wheel and sdist with pipx", 1)[1]
        pipx_before, pipx_after = pipx_step.split("uvx pipx upgrade biomem-memory", 1)
        for executable in ("biomem$suffix", "biomem-server$suffix", "biomem-mcp$suffix"):
            self.assertIn(executable, uv_before)
            self.assertIn(executable, uv_after)
            self.assertIn(executable, pipx_before)
            self.assertIn(executable, pipx_after)
        publish = self.job("publish", "post-publish")
        self.assertIn("needs: [prepare, install-smoke]", publish)

    def test_distribution_name_version_and_console_scripts_are_consistent(self) -> None:
        self.assertEqual(self.metadata["name"], "biomem-memory")
        self.assertEqual(self.metadata["version"], "0.0.2")
        self.assertEqual(
            self.metadata["scripts"],
            {
                "biomem-server": "memory_module.main:main",
                "biomem": "memory_module.cli:main",
                "biomem-mcp": "memory_module.mcp_server:main",
            },
        )
        self.assertIn("mcp==2.1.1", self.metadata["dependencies"])
        self.assertEqual(self.metadata["optional-dependencies"]["mcp"], [])
        self.assertTrue((ROOT / "src" / self.metadata["readme"]).is_file())


if __name__ == "__main__":
    unittest.main()
