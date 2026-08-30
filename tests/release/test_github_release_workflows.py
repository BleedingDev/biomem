from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GitHubReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.caller = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.core = (ROOT / ".github/workflows/release-core.yml").read_text(encoding="utf-8")
        self.publish = (ROOT / ".github/workflows/release-publish.yml").read_text(encoding="utf-8")

    def test_python_distributions_are_built_once_then_reused_by_every_smoke_lane(self) -> None:
        self.assertEqual(self.core.count("python -m build src --outdir release-assets"), 1)
        self.assertIn("python -m twine check release-assets/*", self.core)
        self.assertIn("needs: [verify, python-distributions, binaries]", self.core)
        self.assertIn("name: release-core-python", self.core)
        self.assertIn("name: release-core-${{ matrix.target }}", self.core)
        self.assertIn("canonical_artifacts.py smoke", self.core)
        self.assertNotIn("pip install -e", self.core.split("exact-artifact-smoke:", 1)[1])

    def test_every_checkout_is_pinned_and_never_persists_credentials(self) -> None:
        for workflow in (self.core, self.publish):
            checkout_count = workflow.count("uses: actions/checkout@")
            self.assertGreater(checkout_count, 0)
            self.assertEqual(workflow.count("ref: ${{ inputs.source_sha }}"), checkout_count)
            self.assertEqual(workflow.count("persist-credentials: false"), checkout_count)

    def test_producer_digests_are_separate_from_release_assets(self) -> None:
        self.assertIn("name: producer-digest-python", self.core)
        self.assertIn("name: producer-digest-${{ matrix.target }}", self.core)
        self.assertIn("Download producer digests without merging", self.publish)
        self.assertIn("--digests-dir incoming/digests", self.publish)
        self.assertNotIn("merge-multiple: true", self.publish)

    def test_write_permissions_are_isolated_to_the_required_jobs(self) -> None:
        self.assertIn("permissions: {}", self.publish)
        attest = self.publish.split("  attest:\n", 1)[1].split("  prepare:\n", 1)[0]
        prepare = self.publish.split("  prepare:\n", 1)[1].split("  publish:\n", 1)[0]
        publication = self.publish.split("  publish:\n", 1)[1].split("  dispatch:\n", 1)[0]
        dispatch = self.publish.split("  dispatch:\n", 1)[1]
        self.assertIn("id-token: write", attest)
        self.assertIn("attestations: write", attest)
        self.assertNotIn("contents: write", attest)
        self.assertIn("contents: read", prepare)
        self.assertNotIn("id-token: write", prepare)
        self.assertIn("contents: write", publication)
        self.assertNotIn("id-token: write", publication)
        self.assertNotIn("attestations: write", publication)
        self.assertIn("contents: write", dispatch)
        self.assertNotIn("id-token: write", dispatch)
        self.assertNotIn("attestations: write", dispatch)

    def test_dry_run_skips_every_external_write_job(self) -> None:
        for job in ("attest", "prepare", "publish", "dispatch"):
            section = self.publish.split(f"  {job}:\n", 1)[1]
            self.assertRegex(section, r"(?m)^    if: \$\{\{ !inputs\.dry_run \}\}$")
        assemble = self.publish.split("  assemble:\n", 1)[1].split("  attest:\n", 1)[0]
        self.assertNotIn("contents: write", assemble)
        self.assertNotIn("gh release", assemble)
        self.assertIn("release-preflight.json", assemble)

    def test_publication_has_no_clobber_and_one_transition_after_exact_verification(self) -> None:
        self.assertNotIn("--clobber", self.publish)
        self.assertNotIn("--clobber", (
            ROOT / "scripts/release/publish_github_release.py"
        ).read_text(encoding="utf-8"))
        publication = self.publish.split("  publish:\n", 1)[1].split("  dispatch:\n", 1)[0]
        self.assertIn("verify-payload", self.publish)
        self.assertEqual(publication.count("publish_github_release.py"), 1)
        self.assertIn("needs: prepare", publication)

    def test_postcondition_only_dispatch_is_retryable_and_carries_exact_identity(self) -> None:
        publication = self.publish.split("  publish:\n", 1)[1].split("  dispatch:\n", 1)[0]
        dispatch = self.publish.split("  dispatch:\n", 1)[1]
        self.assertIn("needs: publish", dispatch)
        self.assertNotIn("--operation dispatch", publication)
        self.assertIn("--operation dispatch", dispatch)
        self.assertIn("Re-verify immutable release", dispatch)
        self.assertEqual(publication.count("publish_github_release.py"), 1)
        self.assertEqual(dispatch.count("publish_github_release.py"), 1)

    def test_publication_requires_authoritative_immutable_release_preflight(self) -> None:
        declaration = self.publish.split("permissions: {}", 1)[0]
        publication = self.publish.split("  publish:\n", 1)[1].split("  dispatch:\n", 1)[0]
        dispatch = self.publish.split("  dispatch:\n", 1)[1]
        self.assertIn("IMMUTABLE_RELEASES_TOKEN:", declaration)
        self.assertIn("required: false", declaration)
        self.assertIn(
            "IMMUTABLE_RELEASES_TOKEN: ${{ secrets.IMMUTABLE_RELEASES_TOKEN }}",
            publication,
        )
        self.assertIn("contents: write", publication)
        self.assertNotIn("administration: read", publication)
        self.assertIn(
            "IMMUTABLE_RELEASES_TOKEN: ${{ secrets.IMMUTABLE_RELEASES_TOKEN }}",
            dispatch,
        )

    def test_attestation_subject_is_the_exact_verified_canonical_set(self) -> None:
        self.assertIn("name: canonical-release-assets", self.publish)
        self.assertIn("subject-path: canonical-assets/*", self.publish)
        self.assertIn("BLOCKED_ENVIRONMENT", self.publish)

    def test_browser_handoffs_are_current_run_bound_and_staged_before_attestation(self) -> None:
        assemble = self.publish.split("  assemble:\n", 1)[1].split("  attest:\n", 1)[0]
        self.assertIn(
            "name: browser-ready-chrome-current-${{ github.run_id }}-${{ github.run_attempt }}",
            assemble,
        )
        self.assertIn(
            "name: browser-firefox-current-${{ github.run_id }}-${{ github.run_attempt }}",
            assemble,
        )
        self.assertIn("assemble-publication-assets", assemble)
        self.assertIn("--run-id \"$RUN_ID\"", assemble)
        self.assertIn("--run-attempt \"$RUN_ATTEMPT\"", assemble)
        self.assertIn("--evidence-dir channel-evidence", assemble)
        self.assertLess(
            self.publish.index("assemble-publication-assets"),
            self.publish.index("actions/attest-build-provenance@"),
        )

    def test_every_publication_stage_uses_the_same_canonical_asset_set(self) -> None:
        self.assertNotIn("canonical-release-core", self.publish)
        self.assertNotIn("subject-path: core-assets/*", self.publish)
        self.assertIn("--artifacts-dir canonical-assets", self.publish)
        self.assertIn("path: canonical-assets/*", self.publish)

    def test_real_release_requires_tag_bound_actions_context(self) -> None:
        trigger = self.caller.split("permissions:\n", 1)[0]
        self.assertIn("\n  workflow_dispatch:\n", trigger)
        self.assertNotIn("\n  push:\n", trigger)
        verify = self.core.split("  verify:\n", 1)[1].split(
            "  python-distributions:\n", 1
        )[0]
        self.assertIn('if [[ "$RELEASE_DRY_RUN" != "true" ]]', verify)
        self.assertIn('[[ "$WORKFLOW_REF" == "refs/tags/$RELEASE_TAG" ]]', verify)
        self.assertIn('[[ "$WORKFLOW_SHA" == "$SOURCE_SHA" ]]', verify)
        attest = self.publish.split("  attest:\n", 1)[1].split("  prepare:\n", 1)[0]
        publication = self.publish.split("  publish:\n", 1)[1].split("  dispatch:\n", 1)[0]
        dispatch = self.publish.split("  dispatch:\n", 1)[1]
        for section in (attest, publication, dispatch):
            self.assertIn('[[ "$WORKFLOW_REF" == "refs/tags/$RELEASE_TAG" ]]', section)
            self.assertIn('[[ "$WORKFLOW_SHA" == "$SOURCE_SHA" ]]', section)
        self.assertIn('if [[ "$RELEASE_DRY_RUN" != "true" ]]', self.caller)
        self.assertIn('[[ "$WORKFLOW_REF" == "refs/tags/$RELEASE_TAG" ]]', self.caller)
        self.assertIn('[[ "$WORKFLOW_SHA" == "$source_sha" ]]', self.caller)
        self.assertIn("permissions:\n  actions: read\n  contents: read", self.caller)

    def test_caller_passes_only_the_immutable_preflight_secret(self) -> None:
        finalize = self.caller.split("  finalize:\n", 1)[1]
        self.assertIn(
            "IMMUTABLE_RELEASES_TOKEN: ${{ secrets.IMMUTABLE_RELEASES_TOKEN }}",
            finalize,
        )
        self.assertNotIn("secrets: inherit", finalize)

    def test_caller_enforces_selected_channels_only_after_canonical_publication(self) -> None:
        self.assertLess(
            self.caller.index("  finalize:\n"),
            self.caller.index("  enforce-selected-channels:\n"),
        )
        enforcement = self.caller.split("  enforce-selected-channels:\n", 1)[1]
        self.assertIn("needs: [resolve, finalize]", enforcement)
        self.assertIn("needs.finalize.result == 'success'", enforcement)
        self.assertIn("inputs.dry_run", enforcement)
        self.assertIn("name: canonical-release-publication", enforcement)
        self.assertIn(
            "--manifest publication/payload/release-manifest.json", enforcement
        )
        self.assertIn("--enforce-selected", enforcement)
        self.assertIn("contents: read", enforcement)
        self.assertNotIn("contents: write", enforcement)

    def test_attestation_action_uses_the_verified_v3_2_commit_not_its_tag_object(self) -> None:
        verified_commit = "96278af6caaf10aea03fd8d33a09a777ca52d62f"
        annotated_tag_object = "62fc1d596301d0ab9914e1fec14dc5c8d93f65cd"
        self.assertIn(f"actions/attest-build-provenance@{verified_commit}", self.publish)
        self.assertNotIn(annotated_tag_object, self.publish)

    def test_reusable_publish_workflow_binds_tag_input_at_every_trust_boundary(self) -> None:
        check = "downloaded release policy tag does not match workflow input"
        self.assertEqual(self.publish.count(check), 4)
        self.assertEqual(self.publish.count("RELEASE_TAG: ${{ inputs.tag }}"), 7)

    def test_macos_intel_uses_python_3_12_in_build_and_exact_smoke_matrices(self) -> None:
        selector = (
            "python-version: ${{ matrix.target == 'macos-x86_64' "
            "&& '3.12' || env.PYTHON_VERSION }}"
        )
        self.assertEqual(self.core.count(selector), 2)
        self.assertIn("runs-on: ${{ matrix.os }}", self.core)

    def test_macos_binary_is_verified_ad_hoc_signed_before_packaging(self) -> None:
        build = self.core.index("- name: Build single-file binary")
        signature = self.core.index("- name: Require an ad-hoc signature", build)
        package = self.core.index("- name: Assemble and inspect canonical standalone archive")
        self.assertLess(build, signature)
        self.assertLess(signature, package)
        section = self.core[signature:package]
        self.assertIn("if: runner.os == 'macOS'", section)
        self.assertIn("codesign --force --sign - --timestamp=none", section)
        self.assertIn("codesign --verify --strict --verbose=2", section)
        self.assertIn("Signature=adhoc", section)

    def test_standalone_builds_pin_cpu_torch_without_dependency_reresolution(self) -> None:
        binaries = self.core.split("  binaries:\n", 1)[1].split(
            "  exact-artifact-smoke:\n", 1
        )[0]
        self.assertIn("torch==2.12.1+cpu", binaries)
        self.assertIn("https://download.pytorch.org/whl/cpu", binaries)
        self.assertIn(
            'torch_version = "2.2.2" if target == "macos-x86_64" else "2.12.1"',
            binaries,
        )
        self.assertIn("release-build-constraints.txt", binaries)
        self.assertIn("project[\"dependencies\"]", binaries)
        self.assertIn("project[\"optional-dependencies\"][\"all\"]", binaries)
        self.assertIn("python -m pip install --no-deps -e src", binaries)
        self.assertIn("torch.version.cuda is not None", binaries)
        self.assertIn('expected = "2.12.1+cpu"', binaries)
        self.assertNotIn('pip install -e "src[gui,all]"', binaries)

    def test_linux_verification_also_uses_the_exact_cpu_torch(self) -> None:
        verify = self.core.split("  verify:\n", 1)[1].split(
            "  python-distributions:\n", 1
        )[0]
        self.assertIn("torch==2.12.1+cpu", verify)
        self.assertIn("https://download.pytorch.org/whl/cpu", verify)
        self.assertIn("release-test-constraints.txt", verify)
        self.assertIn("python -m pip install --no-deps -e src", verify)
        self.assertIn('torch.__version__ != "2.12.1+cpu"', verify)
        self.assertIn("torch.version.cuda is not None", verify)
        self.assertNotIn('pip install -e "src[all]"', verify)

    def test_raw_binary_and_archive_are_bounded_below_github_asset_limit(self) -> None:
        binaries = self.core.split("  binaries:\n", 1)[1].split(
            "  exact-artifact-smoke:\n", 1
        )[0]
        self.assertIn("MAX_RELEASE_ASSET_BYTES: '1073741824'", binaries)
        self.assertIn('Path("dist").iterdir()', binaries)
        self.assertIn('Path("release-assets").iterdir()', binaries)
        self.assertLess(1073741824, 2 * 1024 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
