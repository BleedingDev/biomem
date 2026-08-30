from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

from scripts.release import amo_replay


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/publish-browser-channels.yml"
BUILD_SCRIPT = ROOT / "scripts/build_extensions.sh"
CRX_SIGNER = ROOT / "scripts/crx3_sign.py"
TOOLING_PACKAGE = ROOT / "release/browser-tooling/package.json"
TOOLING_LOCK = ROOT / "release/browser-tooling/package-lock.json"
AMO_TRUST_ROOT = ROOT / "release/browser-tooling/mozilla-amo-production-root.pem"


class FakeHttpResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, declared_length: int | None = None) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload) if declared_length is None else declared_length)}

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def signed_xpi_bytes(version: str = "0.0.2") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as xpi:
        xpi.writestr("manifest.json", json.dumps({"version": version}, separators=(",", ":")))
    return output.getvalue()


def amo_replay_zip(
    *,
    version: str,
    tag: str,
    source_sha: str,
    input_sha256: str,
    signed: bytes | None = None,
    metadata_changes: dict[str, object] | None = None,
    extra_name: str | None = None,
) -> bytes:
    if signed is None:
        signed = signed_xpi_bytes(version)
    filename = f"firefox-biomem-{version}-amo-signed.xpi"
    metadata: dict[str, object] = {
        "filename": filename,
        "input_sha256": input_sha256,
        "signed_sha256": hashlib.sha256(signed).hexdigest(),
        "size": len(signed),
        "source_sha": source_sha,
        "tag": tag,
        "version": version,
    }
    metadata.update(metadata_changes or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, signed)
        archive.writestr("verified-firefox-amo.json", json.dumps(metadata))
        if extra_name is not None:
            archive.writestr(extra_name, b"unexpected")
    return output.getvalue()


def embedded_python_programs(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    programs: list[str] = []
    index = 0
    while index < len(lines):
        if "<<'PY'" not in lines[index]:
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(lines) and lines[index].strip() != "PY":
            index += 1
        if index == len(lines):
            raise AssertionError(f"unterminated Python heredoc at line {start}")
        programs.append(textwrap.dedent("\n".join(lines[start:index])))
        index += 1
    return programs


def workflow_run_block(workflow: str, step_name: str) -> str:
    marker = f"      - name: {step_name}\n"
    start = workflow.index(marker) + len(marker)
    run_marker = "        run: |\n"
    start = workflow.index(run_marker, start) + len(run_marker)
    end = workflow.find("\n      - name:", start)
    if end == -1:
        end = len(workflow)
    return textwrap.dedent(workflow[start:end])


class BrowserPackagingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("zip"), "zip is required by the packaging contract")
    def test_build_separates_public_inspection_from_ci_only_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / "scripts").mkdir(parents=True)
            shutil.copy2(BUILD_SCRIPT, checkout / "scripts/build_extensions.sh")
            shutil.copy2(CRX_SIGNER, checkout / "scripts/crx3_sign.py")
            for name in ("chrome-src", "firefox-src", "safari-src", "safari-xcode"):
                shutil.copytree(ROOT / "extensions" / name, checkout / "extensions" / name)
            # Exercise the cp fallback with files that must never reach a package.
            for browser in ("chrome-src", "firefox-src"):
                source = checkout / "extensions" / browser
                (source / "leaked.pem").write_text("private key", encoding="utf-8")
                (source / "stale.crx").write_bytes(b"stale crx")
                (source / "stale.xpi").write_bytes(b"stale xpi")

            environment = dict(os.environ)
            environment["BIOMEM_FORCE_CP_FALLBACK"] = "1"
            completed = subprocess.run(
                [
                    "bash",
                    "scripts/build_extensions.sh",
                    "--no-crx",
                    "--no-safari",
                    "--prefix=contract",
                ],
                cwd=checkout,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            dist = checkout / "dist"
            classification = json.loads((dist / "browser-artifacts.json").read_text())
            by_name = {item["name"]: item for item in classification["artifacts"]}
            self.assertEqual(
                {"chrome-contract.zip", "firefox-contract-unsigned.xpi"},
                set(by_name),
            )
            self.assertEqual("public_inspection", by_name["chrome-contract.zip"]["release_visibility"])
            firefox = by_name["firefox-contract-unsigned.xpi"]
            self.assertEqual("ci_only", firefox["release_visibility"])
            self.assertFalse(firefox["distributable"])
            self.assertFalse((dist / "firefox-contract.xpi").exists())
            for packaged in ("chrome-contract.zip", "firefox-contract-unsigned.xpi"):
                with zipfile.ZipFile(dist / packaged) as archive:
                    self.assertFalse(
                        any(
                            name.endswith((".pem", ".crx", ".xpi"))
                            for name in archive.namelist()
                        ),
                        packaged,
                    )
            with zipfile.ZipFile(dist / "firefox-contract-unsigned.xpi") as archive:
                self.assertFalse(
                    any(name.upper().startswith("META-INF/") for name in archive.namelist())
                )
                packaged_manifest = archive.read("manifest.json")
            source_manifest = (
                checkout / "extensions/firefox-src/manifest.json"
            ).read_bytes()
            self.assertTrue(source_manifest.endswith(b"\n"))
            self.assertEqual(source_manifest[:-1], packaged_manifest)
            self.assertFalse(packaged_manifest.endswith(b"\n"))
            self.assertEqual(
                json.loads(source_manifest),
                json.loads(packaged_manifest),
            )

    def test_source_symlinks_fail_closed_with_each_copy_backend(self) -> None:
        backends = [("cp", "1")]
        if shutil.which("rsync"):
            backends.append(("rsync", "0"))
        for backend, force_fallback in backends:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                checkout = Path(temporary) / "checkout"
                (checkout / "scripts").mkdir(parents=True)
                shutil.copy2(BUILD_SCRIPT, checkout / "scripts/build_extensions.sh")
                shutil.copy2(CRX_SIGNER, checkout / "scripts/crx3_sign.py")
                for name in ("chrome-src", "firefox-src", "safari-src", "safari-xcode"):
                    shutil.copytree(ROOT / "extensions" / name, checkout / "extensions" / name)
                outside = Path(temporary) / "outside-secret.txt"
                outside.write_text("must not be packaged", encoding="utf-8")
                (checkout / "extensions/chrome-src/escape.txt").symlink_to(outside)
                environment = dict(os.environ)
                environment["BIOMEM_FORCE_CP_FALLBACK"] = force_fallback
                completed = subprocess.run(
                    ["bash", "scripts/build_extensions.sh", "--no-crx", "--no-safari"],
                    cwd=checkout,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertIn("symlink", completed.stderr.lower())
                self.assertFalse((checkout / "dist/chrome-biomem.zip").exists())

    def test_staging_symlinks_fail_closed_after_each_copy_backend(self) -> None:
        backends = [("cp", "1", shutil.which("cp"))]
        if shutil.which("rsync"):
            backends.append(("rsync", "0", shutil.which("rsync")))
        for backend, force_fallback, real_copy in backends:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                checkout = Path(temporary) / "checkout"
                (checkout / "scripts").mkdir(parents=True)
                shutil.copy2(BUILD_SCRIPT, checkout / "scripts/build_extensions.sh")
                shutil.copy2(CRX_SIGNER, checkout / "scripts/crx3_sign.py")
                for name in ("chrome-src", "firefox-src", "safari-src", "safari-xcode"):
                    shutil.copytree(ROOT / "extensions" / name, checkout / "extensions" / name)
                outside = Path(temporary) / "outside-secret.txt"
                outside.write_text("must not be packaged", encoding="utf-8")
                fake_bin = Path(temporary) / "bin"
                fake_bin.mkdir()
                wrapper = fake_bin / backend
                wrapper.write_text("""#!/bin/sh
"$BIOMEM_REAL_COPY" "$@"
code=$?
last=""
for argument in "$@"; do last="$argument"; done
case "$last" in
  */dist/.stage/chrome|*/dist/.stage/chrome/)
    ln -s "$BIOMEM_SYMLINK_TARGET" "${last%/}/injected-link"
    ;;
esac
exit "$code"
""", encoding="utf-8")
                wrapper.chmod(0o755)
                environment = dict(os.environ)
                environment.update({
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "BIOMEM_FORCE_CP_FALLBACK": force_fallback,
                    "BIOMEM_REAL_COPY": str(real_copy),
                    "BIOMEM_SYMLINK_TARGET": str(outside),
                })
                completed = subprocess.run(
                    ["bash", "scripts/build_extensions.sh", "--no-crx", "--no-safari"],
                    cwd=checkout,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertIn("staging tree contains a forbidden symlink", completed.stderr.lower())
                self.assertFalse((checkout / "dist/chrome-biomem.zip").exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required by the CRX contract")
    def test_crx_verification_enforces_the_exact_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package = base / "extension.zip"
            key = base / "extension.pem"
            crx = base / "extension.crx"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("manifest.json", '{"manifest_version":3,"version":"0.0.2"}')
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key],
                check=True,
                capture_output=True,
            )
            packed = subprocess.run(
                ["python3", CRX_SIGNER, "pack", package, key, crx],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, packed.returncode, packed.stderr)
            extension_id = next(
                line.rsplit(" ", 1)[-1].rstrip(")")
                for line in packed.stdout.splitlines()
                if "crx_id" in line
            )
            exact = subprocess.run(
                ["python3", CRX_SIGNER, "verify", crx, "--expected-id", extension_id],
                check=False,
                capture_output=True,
                text=True,
            )
            wrong = subprocess.run(
                ["python3", CRX_SIGNER, "verify", crx, "--expected-id", "a" * 32],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, exact.returncode, exact.stdout + exact.stderr)
            self.assertNotEqual(0, wrong.returncode)
            self.assertIn("identity: FAILED", wrong.stdout)


class AmoReplayTests(unittest.TestCase):
    VERSION = "0.0.2"
    TAG = "v0.0.2"
    SOURCE_SHA = "a" * 40
    INPUT_SHA = "b" * 64
    REPOSITORY = "BleedingDev/biomem"
    API_URL = "https://api.github.test"

    def artifact_name(self) -> str:
        return amo_replay.durable_artifact_name(self.TAG, self.SOURCE_SHA, self.INPUT_SHA)

    def artifact_index(self, artifacts: list[dict[str, object]]) -> bytes:
        return json.dumps({"total_count": len(artifacts), "artifacts": artifacts}).encode()

    def artifact(self, archive: bytes, *, artifact_id: int = 17, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "archive_download_url": (
                f"{self.API_URL}/repos/{self.REPOSITORY}/actions/artifacts/{artifact_id}/zip"
            ),
            "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}",
            "expired": False,
            "id": artifact_id,
            "name": self.artifact_name(),
            "size_in_bytes": len(archive),
            "workflow_run": {"head_sha": self.SOURCE_SHA},
        }
        value.update(changes)
        return value

    def resolve(
        self,
        responses: list[bytes | Exception],
        output_dir: Path,
    ) -> tuple[amo_replay.ReplayResult, list[str]]:
        requested: list[str] = []

        def opener(request: object, timeout: int) -> FakeHttpResponse:
            self.assertEqual(30, timeout)
            requested.append(request.full_url)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return FakeHttpResponse(response)

        result = amo_replay.resolve_replay(
            api_url=self.API_URL,
            repository=self.REPOSITORY,
            token="step-scoped-token",
            artifact_name=self.artifact_name(),
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
            output_dir=output_dir,
            opener=opener,
        )
        self.assertEqual([], responses)
        return result, requested

    def test_zero_exact_artifacts_allows_one_signing_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, requested = self.resolve(
                [self.artifact_index([])], Path(temporary) / "amo-output",
            )
        self.assertEqual(
            amo_replay.ReplayResult("provider_lookup", "ready", "no_durable_amo_replay_found"),
            result,
        )
        self.assertEqual(1, len(requested))

    def test_one_exact_artifact_is_safely_materialized_for_shared_verification(self) -> None:
        archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        artifact = self.artifact(archive)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "amo-output"
            result, requested = self.resolve(
                [self.artifact_index([artifact]), archive], output,
            )
            self.assertEqual([f"firefox-biomem-{self.VERSION}-amo-signed.xpi"], [p.name for p in output.iterdir()])
            with zipfile.ZipFile(next(output.iterdir())) as signed:
                self.assertEqual('{"version":"0.0.2"}', signed.read("manifest.json").decode())
        self.assertEqual(
            amo_replay.ReplayResult("reuse", "ready", "durable_amo_replay_verified"),
            result,
        )
        self.assertEqual(2, len(requested))
        self.assertTrue(requested[1].endswith("/actions/artifacts/17/zip"))

    def test_conflict_and_tamper_fail_before_any_provider_contact(self) -> None:
        valid_archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        artifact = self.artifact(valid_archive)
        scenarios = (
            ("multiple", self.artifact_index([artifact, self.artifact(valid_archive, artifact_id=18)]), None),
            (
                "metadata-drift",
                self.artifact_index([artifact]),
                amo_replay_zip(
                    version=self.VERSION,
                    tag=self.TAG,
                    source_sha=self.SOURCE_SHA,
                    input_sha256=self.INPUT_SHA,
                    metadata_changes={"source_sha": "c" * 40},
                ),
            ),
            (
                "unexpected-file",
                self.artifact_index([artifact]),
                amo_replay_zip(
                    version=self.VERSION,
                    tag=self.TAG,
                    source_sha=self.SOURCE_SHA,
                    input_sha256=self.INPUT_SHA,
                    extra_name="../escape",
                ),
            ),
        )
        for name, index, archive in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                responses = [index] + ([] if archive is None else [archive])
                result, requested = self.resolve(responses, Path(temporary) / "amo-output")
                self.assertEqual("stop", result.action)
                self.assertEqual("failed", result.status)
                self.assertEqual("durable_amo_replay_invalid_or_conflicting", result.reason)
                self.assertEqual(1 if archive is None else 2, len(requested))

    def test_rest_artifact_identity_is_bound_before_download_or_reuse(self) -> None:
        archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        mutations = (
            {"workflow_run": {"head_sha": "c" * 40}},
            {"size_in_bytes": len(archive) + 1},
            {"digest": "sha256:" + "0" * 64},
            {"archive_download_url": "https://api.github.test/repos/other/repo/actions/artifacts/17/zip"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                artifact = self.artifact(archive, **mutation)
                downloads = [archive] if set(mutation) <= {"size_in_bytes", "digest"} else []
                result, requested = self.resolve(
                    [self.artifact_index([artifact]), *downloads],
                    Path(temporary) / "amo-output",
                )
                self.assertEqual("stop", result.action)
                self.assertEqual("failed", result.status)
                self.assertEqual("durable_amo_replay_invalid_or_conflicting", result.reason)
                expected_requests = 2 if set(mutation) <= {"size_in_bytes", "digest"} else 1
                self.assertEqual(expected_requests, len(requested))

    def test_replay_archive_and_xpi_size_bounds_are_executable(self) -> None:
        def xpi_with(entries: list[tuple[str, bytes]]) -> bytes:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, payload in entries:
                    archive.writestr(name, payload)
            return output.getvalue()

        too_many = xpi_with([(f"entry-{index}.txt", b"") for index in range(513)])
        oversized_member = xpi_with([("large.bin", b"\0" * (20 * 1024 * 1024 + 1))])
        oversized_total = xpi_with([
            (f"large-{index}.bin", b"\0" * (17 * 1024 * 1024))
            for index in range(3)
        ])
        for name, payload, reason in (
            ("member-count", too_many, "member count"),
            ("single-member", oversized_member, "member exceeds"),
            ("expanded-total", oversized_total, "expands beyond"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                amo_replay.ReplayArtifactInvalid, reason,
            ):
                amo_replay._validate_inner_xpi_structure(payload)

        def oversized_response(_request: object, timeout: int) -> FakeHttpResponse:
            return FakeHttpResponse(
                b"",
                declared_length=amo_replay.MAX_DOWNLOAD_BYTES + 1,
            )

        with self.assertRaisesRegex(
            amo_replay.ReplayLookupUnavailable, "exceeds the size limit",
        ):
            amo_replay._request_bytes(
                "https://api.github.test/repos/o/r/actions/artifacts/1/zip",
                "token",
                accept="application/vnd.github+json",
                opener=oversized_response,
                max_bytes=amo_replay.MAX_DOWNLOAD_BYTES,
            )

        archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        oversized_metadata = self.artifact(
            archive, size_in_bytes=amo_replay.MAX_DOWNLOAD_BYTES + 1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            result, requested = self.resolve(
                [self.artifact_index([oversized_metadata])],
                Path(temporary) / "amo-output",
            )
        self.assertEqual("failed", result.status)
        self.assertEqual(1, len(requested))

    def test_lookup_permission_or_network_failure_is_blocked_environment(self) -> None:
        from urllib.error import HTTPError, URLError

        failures = (
            HTTPError("https://api.github.test", 403, "forbidden", {}, None),
            URLError("offline"),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                result, requested = self.resolve([failure], Path(temporary) / "amo-output")
                self.assertEqual("stop", result.action)
                self.assertEqual("blocked_environment", result.status)
                self.assertEqual("github_actions_lookup_unavailable", result.reason)
                self.assertEqual(1, len(requested))

    def test_expired_or_deleted_exact_replay_blocks_without_resigning(self) -> None:
        from urllib.error import HTTPError

        archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        expired = self.artifact(archive, expired=True)
        live = self.artifact(archive, artifact_id=18)
        scenarios = (
            ("expired", [self.artifact_index([expired])], 1),
            (
                "deleted",
                [
                    self.artifact_index([live]),
                    HTTPError("https://api.github.test", 404, "deleted", {}, None),
                ],
                2,
            ),
        )
        for name, responses, expected_requests in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                result, requested = self.resolve(responses, Path(temporary) / "amo-output")
                self.assertEqual("provider_lookup", result.action)
                self.assertEqual("ready", result.status)
                self.assertEqual("durable_amo_replay_expired_or_deleted", result.reason)
                self.assertEqual(expected_requests, len(requested))

    def test_cross_origin_redirect_does_not_forward_authorization(self) -> None:
        from email.message import Message
        from urllib.error import HTTPError
        from urllib.request import Request

        request = Request("https://api.github.com/repos/o/r/actions/artifacts/1/zip")
        request.add_unredirected_header("Authorization", "Bearer token-sentinel")
        headers = Message()
        headers["Location"] = "https://blob.example.test/signed-artifact"
        redirected = amo_replay.SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", headers, headers["Location"],
        )
        self.assertIsNotNone(redirected)
        self.assertNotIn("Authorization", dict(redirected.header_items()))
        with self.assertRaises(HTTPError):
            amo_replay.SafeRedirectHandler().redirect_request(
                request, None, 302, "Found", headers, "http://blob.example.test/downgrade",
            )

    def test_provider_contract_recovers_exact_signed_version_or_allows_only_exact_404(self) -> None:
        signed = signed_xpi_bytes(self.VERSION)
        download_url = "https://addons.mozilla.org/firefox/downloads/file/17/biomem.xpi"
        exact = json.dumps({
            "channel": "unlisted",
            "file": {
                "hash": f"sha256:{hashlib.sha256(signed).hexdigest()}",
                "size": len(signed),
                "status": "public",
                "url": download_url,
            },
            "is_disabled": False,
            "version": self.VERSION,
        }).encode()

        def run_provider(
            responses: list[bytes | Exception], *, allow_sign: bool = True,
        ) -> tuple[amo_replay.ReplayResult, list[object], Path, tempfile.TemporaryDirectory]:
            temporary = tempfile.TemporaryDirectory()
            output = Path(temporary.name) / "amo-output"
            requests: list[object] = []

            def opener(request: object, timeout: int) -> FakeHttpResponse:
                self.assertEqual(30, timeout)
                requests.append(request)
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return FakeHttpResponse(response)

            result = amo_replay.resolve_amo_provider(
                api_url="https://addons.mozilla.org/api/v5",
                issuer="issuer",
                secret="secret-sentinel",
                guid="biomem@bleedingdev.github.io",
                version=self.VERSION,
                output_dir=output,
                allow_sign=allow_sign,
                opener=opener,
            )
            self.assertEqual([], responses)
            return result, requests, output, temporary

        result, requests, output, temporary = run_provider([exact, signed])
        try:
            self.assertEqual(
                amo_replay.ReplayResult("reuse", "ready", "verified_existing_amo_version"), result,
            )
            self.assertEqual(signed, next(output.iterdir()).read_bytes())
            self.assertEqual(2, len(requests))
            for request in requests:
                authorization = request.get_header("Authorization")
                self.assertIsNotNone(authorization)
                self.assertTrue(authorization.startswith("JWT "))
                self.assertNotIn("secret-sentinel", authorization)
        finally:
            temporary.cleanup()

        from urllib.error import HTTPError
        version_url = "https://addons.mozilla.org/api/v5/addons/addon/biomem%40bleedingdev.github.io/versions/0.0.2/"
        not_found = HTTPError(version_url, 404, "missing", {}, None)
        result, requests, _output, temporary = run_provider([not_found])
        try:
            self.assertEqual(amo_replay.ReplayResult("sign", "ready", "amo_exact_version_not_found"), result)
            self.assertEqual(1, len(requests))
        finally:
            temporary.cleanup()
        not_found = HTTPError(version_url, 404, "missing", {}, None)
        result, _requests, _output, temporary = run_provider([not_found], allow_sign=False)
        try:
            self.assertEqual("blocked_environment", result.status)
            self.assertEqual("amo_version_visibility_unconfirmed", result.reason)
        finally:
            temporary.cleanup()
        redirected_missing = HTTPError(
            "https://mirror.example.test/missing", 404, "missing", {}, None,
        )
        result, _requests, _output, temporary = run_provider([redirected_missing])
        try:
            self.assertEqual("blocked_environment", result.status)
            self.assertEqual("provider_redirected_resource_unavailable", result.reason)
        finally:
            temporary.cleanup()

    def test_provider_errors_are_truthfully_classified_without_live_calls(self) -> None:
        from urllib.error import HTTPError, URLError

        cases = (
            (HTTPError("https://addons.mozilla.org", 401, "auth", {}, None), "blocked_environment", "provider_authentication_or_permission_required"),
            (HTTPError("https://addons.mozilla.org", 403, "permission", {}, None), "blocked_environment", "provider_authentication_or_permission_required"),
            (HTTPError("https://addons.mozilla.org", 429, "rate", {}, None), "blocked_environment", "provider_rate_limit_or_unavailable"),
            (HTTPError("https://addons.mozilla.org", 503, "down", {}, None), "blocked_environment", "provider_rate_limit_or_unavailable"),
            (URLError("offline"), "blocked_environment", "provider_transport_unavailable"),
            (ValueError("bad fixture"), "raises", ""),
        )
        for failure, status, reason in cases:
            if status == "raises":
                continue
            with self.subTest(code=reason), tempfile.TemporaryDirectory() as temporary:
                def opener(_request: object, timeout: int, error: Exception = failure) -> FakeHttpResponse:
                    raise error
                result = amo_replay.resolve_amo_provider(
                    api_url="https://addons.mozilla.org/api/v5",
                    issuer="issuer",
                    secret="secret",
                    guid="biomem@bleedingdev.github.io",
                    version=self.VERSION,
                    output_dir=Path(temporary) / "amo-output",
                    allow_sign=True,
                    opener=opener,
                )
                self.assertEqual((status, reason), (result.status, result.reason))

    def test_provider_malformed_conflicting_and_hash_mismatch_fail(self) -> None:
        signed = signed_xpi_bytes(self.VERSION)
        base = {
            "channel": "unlisted",
            "file": {
                "hash": f"sha256:{hashlib.sha256(signed).hexdigest()}",
                "size": len(signed),
                "status": "public",
                "url": "https://addons.mozilla.org/firefox/downloads/file/17/biomem.xpi",
            },
            "is_disabled": False,
            "version": self.VERSION,
        }
        scenarios = (
            (b"[]", []),
            (json.dumps({**base, "version": "0.0.3"}).encode(), []),
            (json.dumps({**base, "file": {**base["file"], "url": "https://evil.test/file.xpi"}}).encode(), []),
            (json.dumps(base).encode(), [signed + b"tamper"]),
        )
        for response, downloads in scenarios:
            with self.subTest(response=response[:20]), tempfile.TemporaryDirectory() as temporary:
                fixtures = [response, *downloads]
                def opener(_request: object, timeout: int) -> FakeHttpResponse:
                    return FakeHttpResponse(fixtures.pop(0))
                result = amo_replay.resolve_amo_provider(
                    api_url="https://addons.mozilla.org/api/v5",
                    issuer="issuer",
                    secret="secret",
                    guid="biomem@bleedingdev.github.io",
                    version=self.VERSION,
                    output_dir=Path(temporary) / "amo-output",
                    allow_sign=True,
                    opener=opener,
                )
                self.assertEqual("failed", result.status)
                self.assertEqual("amo_exact_version_invalid_or_conflicting", result.reason)
                self.assertEqual([], fixtures)

    def test_provider_state_requires_public_unlisted_enabled_version(self) -> None:
        signed = signed_xpi_bytes(self.VERSION)
        exact = {
            "channel": "unlisted",
            "file": {
                "hash": f"sha256:{hashlib.sha256(signed).hexdigest()}",
                "size": len(signed),
                "status": "public",
                "url": "https://addons.mozilla.org/firefox/downloads/file/17/biomem.xpi",
            },
            "is_disabled": False,
            "version": self.VERSION,
        }
        scenarios = (
            ({key: value for key, value in exact.items() if key != "is_disabled"}, "failed"),
            ({**exact, "is_disabled": True}, "blocked_environment"),
            ({**exact, "channel": "listed"}, "failed"),
            ({**exact, "file": {**exact["file"], "status": "unreviewed"}}, "blocked_environment"),
            ({**exact, "file": {**exact["file"], "status": "disabled"}}, "blocked_environment"),
        )
        for payload, expected_status in scenarios:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                fixtures = [json.dumps(payload).encode()]
                def opener(_request: object, timeout: int) -> FakeHttpResponse:
                    return FakeHttpResponse(fixtures.pop(0))
                result = amo_replay.resolve_amo_provider(
                    api_url="https://addons.mozilla.org/api/v5",
                    issuer="issuer",
                    secret="secret",
                    guid="biomem@bleedingdev.github.io",
                    version=self.VERSION,
                    output_dir=Path(temporary) / "amo-output",
                    allow_sign=True,
                    opener=opener,
                )
                self.assertEqual(expected_status, result.status)

    def test_fresh_run_after_amo_success_but_before_durable_upload_recovers_without_signing(self) -> None:
        signed = signed_xpi_bytes(self.VERSION)
        exact = json.dumps({
            "channel": "unlisted",
            "file": {
                "hash": f"sha256:{hashlib.sha256(signed).hexdigest()}",
                "size": len(signed),
                "status": "public",
                "url": "https://addons.mozilla.org/firefox/downloads/file/17/biomem.xpi",
            },
            "is_disabled": False,
            "version": self.VERSION,
        }).encode()
        signing_calls = 1  # First run reached AMO, then failed before durable GitHub upload.
        with tempfile.TemporaryDirectory() as temporary:
            github_result, _ = self.resolve(
                [self.artifact_index([])], Path(temporary) / "github-replay",
            )
            self.assertEqual("provider_lookup", github_result.action)
            fixtures = [exact, signed]
            def opener(_request: object, timeout: int) -> FakeHttpResponse:
                return FakeHttpResponse(fixtures.pop(0))
            provider_result = amo_replay.resolve_amo_provider(
                api_url="https://addons.mozilla.org/api/v5",
                issuer="issuer",
                secret="secret",
                guid="biomem@bleedingdev.github.io",
                version=self.VERSION,
                output_dir=Path(temporary) / "amo-output",
                allow_sign=True,
                opener=opener,
            )
            if provider_result.action == "sign":
                signing_calls += 1
        self.assertEqual("reuse", provider_result.action)
        self.assertEqual(1, signing_calls)

    def test_downstream_failure_then_fresh_retry_never_signs_twice(self) -> None:
        durable: list[bytes] = []
        signing_calls = 0
        archive = amo_replay_zip(
            version=self.VERSION,
            tag=self.TAG,
            source_sha=self.SOURCE_SHA,
            input_sha256=self.INPUT_SHA,
        )
        artifact = self.artifact(archive, artifact_id=41)
        decisions: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            for run in range(2):
                responses = [self.artifact_index([] if not durable else [artifact]), *durable]
                result, _ = self.resolve(responses, Path(temporary) / f"amo-output-{run}")
                decisions.append(result.action)
                if result.action == "provider_lookup":
                    signing_calls += 1
                    durable[:] = [archive]
                    # The later evidence/assembly boundary fails after the durable upload.
        self.assertEqual(["provider_lookup", "reuse"], decisions)
        self.assertEqual(1, signing_calls)


class BrowserWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def run_chrome_replay(
        self,
        responses: list[dict[str, object]],
        token_response: dict[str, object] | None = None,
    ) -> tuple[dict, list[dict], subprocess.CompletedProcess]:
        script = workflow_run_block(self.text, "Upload and submit the exact tested ZIP")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_bin = base / "bin"
            runner_temp = base / "runner-temp"
            chrome_input = base / "chrome-input"
            fake_bin.mkdir()
            runner_temp.mkdir()
            chrome_input.mkdir()
            package = chrome_input / "chrome-biomem.zip"
            package.write_bytes(b"exact-tested-chrome-zip")
            transcript = base / "transcript.json"
            state = base / "state"
            log = base / "curl-log.jsonl"
            all_responses = [
                token_response or {
                    "endpoint": "token", "code": 200,
                    "body": {"access_token": "provider-token"},
                },
                *responses,
            ]
            transcript.write_text(json.dumps(all_responses), encoding="utf-8")
            fake_curl = fake_bin / "curl"
            fake_curl.write_text("""#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
url = next((value for value in reversed(args) if value.startswith("https://")), "")
if "oauth2.googleapis.com/token" in url:
    endpoint = "token"
elif ":fetchStatus" in url:
    endpoint = "status"
elif ":upload" in url:
    endpoint = "upload"
elif ":publish" in url:
    endpoint = "publish"
else:
    raise SystemExit(f"unexpected URL: {url}")
state_path = Path(os.environ["FAKE_CURL_STATE"])
index = int(state_path.read_text() or "0") if state_path.exists() else 0
queue = json.loads(Path(os.environ["FAKE_CURL_TRANSCRIPT"]).read_text())
if index >= len(queue):
    raise SystemExit("fake curl transcript exhausted")
response = queue[index]
state_path.write_text(str(index + 1))
if response["endpoint"] != endpoint:
    raise SystemExit(f"expected {response['endpoint']}, got {endpoint}")
output = args[args.index("--output") + 1]
body = response.get("body", {})
Path(output).write_text(body if isinstance(body, str) else json.dumps(body))
record = {"endpoint": endpoint}
if endpoint == "upload":
    upload = Path(args[args.index("-T") + 1])
    record["sha256"] = hashlib.sha256(upload.read_bytes()).hexdigest()
with Path(os.environ["FAKE_CURL_LOG"]).open("a") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\\n")
print(response["code"], end="")
raise SystemExit(response.get("exit_code", 0))
""", encoding="utf-8")
            fake_curl.chmod(0o755)
            fake_sleep = fake_bin / "sleep"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "CWS_PUBLISHER_ID": "publisher-contract",
                "CWS_ITEM_ID": "a" * 32,
                "CWS_CLIENT_ID": "client-secret-sentinel",
                "CWS_CLIENT_SECRET": "client-secret-sentinel",
                "CWS_REFRESH_TOKEN": "refresh-secret-sentinel",
                "RELEASE_VERSION": "0.0.2",
                "FAKE_CURL_TRANSCRIPT": str(transcript),
                "FAKE_CURL_STATE": str(state),
                "FAKE_CURL_LOG": str(log),
            })
            completed = subprocess.run(
                ["bash", "-c", script], cwd=base, env=environment,
                check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertNotIn("secret-sentinel", completed.stdout + completed.stderr)
            self.assertEqual(len(all_responses), int(state.read_text()), "fixture queue not exhausted")
            self.assertFalse((runner_temp / "cws-token.json").exists(), "OAuth token was not cleaned")
            result = json.loads((base / "chrome-result.json").read_text())
            records = [json.loads(line) for line in log.read_text().splitlines()]
            return result, records, completed

    def test_dry_run_is_a_hard_network_boundary(self) -> None:
        self.assertIn('elif dry_run:', self.text)
        self.assertGreaterEqual(self.text.count('"not_attempted_dry_run"'), 2)
        self.assertIn("if: steps.readiness.outputs.attempt == 'true'", self.text)
        self.assertIn("if: needs.preflight.outputs.safari == 'true' && !inputs.dry_run", self.text)
        self.assertNotIn("continue-on-error", self.text)

    def test_chrome_uses_v2_api_and_exact_identity_checks(self) -> None:
        self.assertIn("chromewebstore.googleapis.com", self.text)
        self.assertIn("/upload/v2/$item:upload", self.text)
        self.assertIn("/v2/$item:publish", self.text)
        self.assertIn('response.get("itemId") != sys.argv[1]', self.text)
        self.assertIn("provider_identity_mismatch", self.text)
        self.assertIn("immutable_store_identity_not_configured", self.text)
        self.assertNotIn("CWS_EXPECTED_EXTENSION_ID", self.text)
        self.assertIn('response.get("crxVersion")', self.text)
        self.assertIn("PENDING_REVIEW", self.text)
        self.assertIn("store_review_pending", self.text)
        self.assertIn(":fetchStatus", self.text)
        for state in ("IN_PROGRESS", "SUCCEEDED", "FAILED", "NOT_FOUND"):
            self.assertIn(f'"{state}"', self.text)
        self.assertNotIn("UPLOAD_IN_PROGRESS", self.text)
        self.assertNotIn("UPLOAD_COMPLETE", self.text)
        self.assertNotIn("UPLOAD_FAILED", self.text)
        self.assertIn("async_upload_poll_timeout", self.text)
        self.assertIn("invalid_${2}_package_or_version", self.text)
        self.assertIn('elif [[ "$status_code" != 200 ]]', self.text)
        self.assertIn("trap 'rm -f -- \"$token_file\"' EXIT", self.text)
        self.assertIn('response.get("takenDown")', self.text)
        self.assertIn('response.get("warned")', self.text)
        self.assertIn('row.get("deployPercentage") == 100', self.text)
        self.assertIn("input_sha256=$CHROME_INPUT_SHA256", self.text)

    def test_chrome_state_machine_replays_offline_http_transcripts(self) -> None:
        item_id = "a" * 32
        empty_status = {"itemId": item_id, "lastAsyncUploadState": "NOT_FOUND"}
        def publish_attempt(post_status: dict[str, object]) -> list[dict[str, object]]:
            return [
                {"endpoint": "status", "code": 200, "body": empty_status},
                {"endpoint": "upload", "code": 200, "body": {
                    "itemId": item_id, "crxVersion": "0.0.2", "uploadState": "SUCCEEDED",
                }},
                {"endpoint": "publish", "code": 200, "body": {
                    "itemId": item_id, "state": "PUBLISHED",
                }},
                {"endpoint": "status", "code": 200, "body": post_status},
            ]

        public_status = {
            "itemId": item_id,
            "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                {"crxVersion": "0.0.2", "deployPercentage": 100},
            ]},
        }
        scenarios = (
            (
                "existing-public",
                [{"endpoint": "status", "code": 200, "body": {
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.2", "deployPercentage": 100},
                    ]},
                }}],
                ("published", "verified_existing_store_state"),
                ["token", "status"],
            ),
            (
                "partial-rollout",
                [{"endpoint": "status", "code": 200, "body": {
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.2", "deployPercentage": 0},
                    ]},
                }}],
                ("blocked_environment", "store_rollout_not_fully_deployed"),
                ["token", "status"],
            ),
            (
                "existing-mixed-versions",
                [{"endpoint": "status", "code": 200, "body": {
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.2", "deployPercentage": 100},
                        {"crxVersion": "0.0.3", "deployPercentage": 100},
                    ]},
                }}],
                ("failed", "mixed_published_versions"),
                ["token", "status"],
            ),
            (
                "existing-duplicate-partial",
                [{"endpoint": "status", "code": 200, "body": {
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.2", "deployPercentage": 100},
                        {"crxVersion": "0.0.2", "deployPercentage": 0},
                    ]},
                }}],
                ("blocked_environment", "store_rollout_not_fully_deployed"),
                ["token", "status"],
            ),
            (
                "taken-down",
                [{"endpoint": "status", "code": 200, "body": {"itemId": item_id, "takenDown": True}}],
                ("blocked_environment", "store_item_taken_down"),
                ["token", "status"],
            ),
            (
                "different-pending-version",
                [{"endpoint": "status", "code": 200, "body": {
                    "itemId": item_id,
                    "submittedItemRevisionStatus": {"state": "PENDING_REVIEW", "distributionChannels": [
                        {"crxVersion": "0.0.3", "deployPercentage": 0},
                    ]},
                }}],
                ("blocked_environment", "different_store_revision_pending"),
                ["token", "status"],
            ),
            (
                "fetch-invalid",
                [{"endpoint": "status", "code": 422, "body": {"error": "invalid"}}],
                ("failed", "invalid_status_package_or_version"),
                ["token", "status"],
            ),
            (
                "malformed-status",
                [{"endpoint": "status", "code": 200, "body": []}],
                ("failed", "malformed_store_status"),
                ["token", "status"],
            ),
            (
                "sync-publish",
                publish_attempt(public_status),
                ("published", "verified_store_state"),
                ["token", "status", "upload", "publish", "status"],
            ),
            (
                "post-publish-partial",
                publish_attempt({
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.2", "deployPercentage": 0},
                    ]},
                }),
                ("blocked_environment", "store_rollout_not_fully_deployed"),
                ["token", "status", "upload", "publish", "status"],
            ),
            (
                "post-publish-taken-down",
                publish_attempt({**public_status, "takenDown": True}),
                ("blocked_environment", "store_item_taken_down"),
                ["token", "status", "upload", "publish", "status"],
            ),
            (
                "post-publish-warned",
                publish_attempt({**public_status, "warned": True}),
                ("blocked_environment", "store_policy_warning_requires_manual_action"),
                ["token", "status", "upload", "publish", "status"],
            ),
            (
                "post-publish-version-mismatch",
                publish_attempt({
                    "itemId": item_id,
                    "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                        {"crxVersion": "0.0.3", "deployPercentage": 100},
                    ]},
                }),
                ("failed", "provider_version_mismatch"),
                ["token", "status", "upload", "publish", "status"],
            ),
            (
                "async-strict-stop",
                [
                    {"endpoint": "status", "code": 200, "body": empty_status},
                    {"endpoint": "upload", "code": 200, "body": {
                        "itemId": item_id, "uploadState": "IN_PROGRESS",
                    }},
                    {"endpoint": "status", "code": 200, "body": {
                        "itemId": item_id, "lastAsyncUploadState": "SUCCEEDED",
                    }},
                ],
                ("blocked_environment", "async_upload_completed_version_unverifiable"),
                ["token", "status", "upload", "status"],
            ),
        )
        expected_upload_digest = hashlib.sha256(b"exact-tested-chrome-zip").hexdigest()
        for name, responses, expected_result, expected_endpoints in scenarios:
            with self.subTest(name=name):
                result, records, _ = self.run_chrome_replay(responses)
                self.assertEqual(expected_result, (result["status"], result["reason"]))
                self.assertEqual(expected_endpoints, [record["endpoint"] for record in records])
                for record in records:
                    if record["endpoint"] == "upload":
                        self.assertEqual(expected_upload_digest, record["sha256"])

    def test_chrome_transport_failures_stop_before_the_next_mutation(self) -> None:
        item_id = "a" * 32
        status = {"itemId": item_id, "lastAsyncUploadState": "NOT_FOUND"}
        upload = {"itemId": item_id, "crxVersion": "0.0.2", "uploadState": "SUCCEEDED"}
        publish = {"itemId": item_id, "state": "PUBLISHED"}
        public = {
            "itemId": item_id,
            "publishedItemRevisionStatus": {"state": "PUBLISHED", "distributionChannels": [
                {"crxVersion": "0.0.2", "deployPercentage": 100},
            ]},
        }
        cases = (
            ("token", [], {"endpoint": "token", "code": "000", "body": {}, "exit_code": 7}, ["token"]),
            ("status", [{"endpoint": "status", "code": "000", "body": {}, "exit_code": 7}], None, ["token", "status"]),
            ("upload", [
                {"endpoint": "status", "code": 200, "body": status},
                {"endpoint": "upload", "code": "000", "body": {}, "exit_code": 7},
            ], None, ["token", "status", "upload"]),
            ("publish", [
                {"endpoint": "status", "code": 200, "body": status},
                {"endpoint": "upload", "code": 200, "body": upload},
                {"endpoint": "publish", "code": "000", "body": {}, "exit_code": 7},
            ], None, ["token", "status", "upload", "publish"]),
            ("async-poll", [
                {"endpoint": "status", "code": 200, "body": status},
                {"endpoint": "upload", "code": 200, "body": {
                    "itemId": item_id, "uploadState": "IN_PROGRESS",
                }},
                {"endpoint": "status", "code": "000", "body": {}, "exit_code": 7},
            ], None, ["token", "status", "upload", "status"]),
            ("post-status", [
                {"endpoint": "status", "code": 200, "body": status},
                {"endpoint": "upload", "code": 200, "body": upload},
                {"endpoint": "publish", "code": 200, "body": publish},
                {"endpoint": "status", "code": "000", "body": public, "exit_code": 7},
            ], None, ["token", "status", "upload", "publish", "status"]),
        )
        for name, responses, token_response, endpoints in cases:
            with self.subTest(boundary=name):
                result, records, _ = self.run_chrome_replay(responses, token_response)
                self.assertEqual(
                    ("blocked_environment", "provider_transport_unavailable"),
                    (result["status"], result["reason"]),
                )
                self.assertEqual(endpoints, [record["endpoint"] for record in records])

    def test_firefox_only_promotes_verified_amo_output(self) -> None:
        self.assertIn("release/browser-tooling/node_modules/.bin/web-ext", self.text)
        self.assertIn("firefox-biomem-unsigned.xpi", self.text)
        self.assertIn("browser-ci-only-firefox-unsigned-input", self.text)
        self.assertIn("firefox_sha256", self.text)
        self.assertIn("META-INF/MOZILLA.RSA", self.text)
        self.assertIn("biomem@bleedingdev.github.io", self.text)
        self.assertIn("browser-firefox-current", self.text)
        self.assertIn(
            "browser-durable-firefox-amo-signed",
            (ROOT / "scripts/release/amo_replay.py").read_text(encoding="utf-8"),
        )
        self.assertIn("invalid_amo_cryptographic_signature", self.text)
        self.assertIn("invalid_amo_signature_binding", self.text)
        self.assertIn("invalid_amo_signer_identity", self.text)
        self.assertIn("amo_payload_drift", self.text)
        self.assertIn("signed_output_awaiting_release_attachment", self.text)
        self.assertIn("verified-firefox-amo.json", self.text)
        self.assertIn('"signed_sha256": hashlib.sha256(target.read_bytes()).hexdigest()', self.text)
        self.assertIn('"size": target.stat().st_size', self.text)
        self.assertIn('"source_sha": sys.argv[4]', self.text)
        self.assertIn('"tag": sys.argv[3]', self.text)
        self.assertNotIn('"verified_amo_signed_output"', self.text)
        self.assertNotIn("path: firefox-input/*.xpi", self.text)

    def test_amo_replay_is_exact_durable_and_provider_preceding(self) -> None:
        readiness = self.text.index("- name: Classify AMO readiness without contacting Mozilla")
        replay = self.text.index("- name: Resolve a durable AMO replay before provider contact")
        provider = self.text.index("- name: Classify AMO provider readiness after a replay miss")
        signing = self.text.index("- name: Sign the exact tested input through AMO")
        durable_upload = self.text.index(
            "- name: Retain the newly signed AMO result as the single durable replay identity"
        )
        transient_upload = self.text.index(
            "- name: Upload exact current-run Firefox handoff envelope"
        )
        self.assertLess(readiness, replay)
        self.assertLess(replay, provider)
        self.assertLess(provider, signing)
        self.assertLess(signing, durable_upload)
        self.assertIn("actions: read", self.text)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", self.text[replay:provider])
        self.assertNotIn("--token", self.text[replay:provider])
        self.assertIn("steps.replay.outputs.artifact_name", self.text[durable_upload:transient_upload])
        self.assertIn("steps.sign.outputs.verify == 'true'", self.text[durable_upload:transient_upload])
        self.assertIn("steps.provider.outputs.action == 'reuse'", self.text[durable_upload:transient_upload])
        self.assertIn("steps.recovery.outputs.action == 'reuse'", self.text[durable_upload:transient_upload])
        self.assertIn("no_durable_amo_replay_found", self.text[durable_upload:transient_upload])
        handoff_start = self.text.index("- name: Create exact current-run Firefox handoff envelope")
        handoff = self.text[handoff_start:self.text.index("- name: Upload Firefox evidence")]
        self.assertIn("firefox-handoff.json", handoff)
        self.assertIn('"run_id": int(os.environ["RUN_ID"])', handoff)
        self.assertIn('"run_attempt": int(os.environ["RUN_ATTEMPT"])', handoff)
        self.assertIn("verified Firefox metadata drift", handoff)
        self.assertIn("github.run_id", self.text[transient_upload:])
        self.assertIn("github.run_attempt", self.text[transient_upload:])
        self.assertEqual(1, self.text.count("steps.replay.outputs.artifact_name"))
        self.assertIn("amo_listed_review_required", self.text[readiness:replay])

    def test_current_run_browser_handoffs_are_exact_and_truthful(self) -> None:
        chrome_create = self.text.index("- name: Create exact current-run Chrome handoff")
        chrome_upload = self.text.index("- name: Upload exact current-run Chrome handoff")
        firefox_create = self.text.index("- name: Create exact current-run Firefox handoff envelope")
        firefox_upload = self.text.index("- name: Upload exact current-run Firefox handoff envelope")
        self.assertLess(chrome_create, chrome_upload)
        self.assertLess(firefox_create, firefox_upload)
        chrome = self.text[chrome_create:chrome_upload]
        firefox = self.text[firefox_create:firefox_upload]
        for field in ("filename", "run_attempt", "run_id", "sha256", "size", "source_sha", "tag", "version"):
            self.assertIn(f'"{field}"', chrome)
            self.assertIn(f'"{field}"', firefox)
        self.assertIn("cp dist/chrome-biomem.zip browser-chrome-ready/", chrome)
        self.assertIn('"ready": ready', firefox)
        self.assertIn('if ready:', firefox)
        self.assertIn("shutil.copyfile(source", firefox)
        self.assertIn("verified-firefox-amo.json", firefox)
        self.assertIn("if: always()", self.text[firefox_upload:firefox_upload + 180])

    def test_non_dry_browser_publication_is_exact_tag_and_source_bound(self) -> None:
        preflight = self.text[self.text.index("  preflight:"):self.text.index("  build-inputs:")]
        self.assertIn('if [[ "$RELEASE_DRY_RUN" != "true" ]]', preflight)
        self.assertIn('[[ "$WORKFLOW_REF" == "refs/tags/$RELEASE_TAG" ]]', preflight)
        self.assertIn('[[ "$WORKFLOW_SHA" == "$SOURCE_SHA" ]]', preflight)
        self.assertIn('[[ "$(git rev-parse "$RELEASE_TAG^{commit}")" == "$SOURCE_SHA" ]]', preflight)
        self.assertIn("fetch-depth: 0", preflight)

    def test_provider_check_precedes_web_ext_and_ambiguous_retry_never_signs_again(self) -> None:
        provider = self.text.index("- name: Query the exact AMO version before creating it")
        signing = self.text.index("- name: Sign the exact tested input through AMO")
        recovery = self.text.index("- name: Recover the exact AMO version after an ambiguous signing result")
        self.assertLess(provider, signing)
        self.assertLess(signing, recovery)
        self.assertEqual(1, self.text.count("node_modules/.bin/web-ext\" sign"))
        recovery_step = self.text[recovery:self.text.index("- name: Verify a new or replayed AMO output identically")]
        self.assertIn("--provider-check --allow-sign false", recovery_step)
        self.assertNotIn("web-ext", recovery_step)

        classifier = next(
            program for program in embedded_python_programs(self.text)
            if "ambiguous_identity" in program
        )
        reconciler = next(
            program for program in embedded_python_programs(self.text)
            if "firefox-sign-failure.json" in program
        )
        scenarios = (
            ("local deterministic packaging error", "2", "failed", "amo_signing_failed", "failed"),
            ("409 version already exists and was deleted", "1", "blocked_environment", "provider_version_identity_consumed_or_ambiguous", "blocked_environment"),
            ("429 too many requests rate limit", "1", "blocked_environment", "provider_version_identity_consumed_or_ambiguous", "blocked_environment"),
            ("", "124", "blocked_environment", "provider_version_identity_consumed_or_ambiguous", "blocked_environment"),
            ("timeout waiting for AMO", "1", "blocked_environment", "provider_version_identity_consumed_or_ambiguous", "blocked_environment"),
        )
        for log, exit_status, expected_original, expected_reason, expected_final in scenarios:
            with self.subTest(log=log), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                (base / "amo-sign.log").write_text(log)
                completed = subprocess.run(
                    [sys.executable, "-c", classifier], cwd=base,
                    env={**os.environ, "AMO_SIGN_STATUS": exit_status},
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                original = json.loads((base / "firefox-result.json").read_text())
                self.assertEqual((expected_original, expected_reason), (original["status"], original["reason"]))
                (base / "firefox-sign-failure.json").write_text(json.dumps(original))
                (base / "firefox-result.json").write_text(json.dumps({
                    "attempt": False,
                    "channel": "unlisted",
                    "reason": "amo_version_visibility_unconfirmed",
                    "status": "blocked_environment",
                }))
                completed = subprocess.run(
                    [sys.executable, "-c", reconciler], cwd=base,
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                final = json.loads((base / "firefox-result.json").read_text())
                self.assertEqual(expected_final, final["status"])

    def test_github_replay_token_is_not_passed_or_emitted(self) -> None:
        helper = (ROOT / "scripts/release/amo_replay.py").read_text(encoding="utf-8")
        sentinel = "github-token-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "github-output"
            result_path = base / "firefox-result.json"
            environment = dict(os.environ)
            environment["GITHUB_TOKEN"] = sentinel
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/release/amo_replay.py"),
                    "--api-url", "http://127.0.0.1:1",
                    "--repository", "BleedingDev/biomem",
                    "--version", "0.0.2",
                    "--tag", "v0.0.2",
                    "--source-sha", "a" * 40,
                    "--input-sha256", "b" * 64,
                    "--output-dir", str(base / "amo-output"),
                    "--github-output", str(output),
                    "--result", str(result_path),
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            emitted = completed.stdout + completed.stderr + output.read_text() + result_path.read_text()
            self.assertNotIn(sentinel, emitted)
            self.assertEqual("blocked_environment", json.loads(result_path.read_text())["status"])
        self.assertNotIn('add_argument("--token"', helper)

    def test_amo_tooling_is_integrity_locked_before_secrets_are_exposed(self) -> None:
        package = json.loads(TOOLING_PACKAGE.read_text(encoding="utf-8"))
        lock = json.loads(TOOLING_LOCK.read_text(encoding="utf-8"))
        self.assertEqual("10.6.0", package["dependencies"]["web-ext"])
        self.assertEqual(3, lock["lockfileVersion"])
        self.assertEqual(
            "10.6.0",
            lock["packages"]["node_modules/web-ext"]["version"],
        )
        unlocked = [
            name
            for name, metadata in lock["packages"].items()
            if name and not metadata.get("link") and not metadata.get("integrity")
        ]
        self.assertEqual([], unlocked, f"packages without integrity: {unlocked}")

        install = self.text.index("- name: Install integrity-locked AMO tooling without secrets")
        readiness = self.text.index("- name: Classify AMO readiness without contacting Mozilla")
        replay = self.text.index("- name: Resolve a durable AMO replay before provider contact")
        provider = self.text.index("- name: Classify AMO provider readiness after a replay miss")
        signing = self.text.index("- name: Sign the exact tested input through AMO")
        self.assertLess(readiness, replay)
        self.assertLess(replay, provider)
        self.assertLess(readiness, install)
        self.assertLess(install, signing)
        install_step = self.text[install:self.text.index("- name: Validate committed Mozilla")]
        signing_step = self.text[signing:self.text.index("- name: Record truthful Firefox evidence")]
        self.assertIn("npm ci --ignore-scripts", install_step)
        self.assertNotIn("AMO_JWT_ISSUER", install_step)
        self.assertNotIn("AMO_JWT_SECRET", install_step)
        readiness_step = self.text[readiness:provider]
        self.assertNotIn("AMO_JWT_ISSUER:", readiness_step)
        self.assertNotIn("AMO_JWT_SECRET:", readiness_step)
        self.assertIn("AMO_CREDENTIALS_CONFIGURED", self.text[provider:install])
        self.assertNotIn("npx --yes", self.text)
        self.assertNotIn("web-ext@", signing_step)
        self.assertIn("node_modules/.bin/web-ext", signing_step)
        self.assertIn("def unsafe_image_format(data):", self.text)
        self.assertIn('data.startswith(b"\\xff\\x0a")', self.text)
        self.assertIn("heif_brands", self.text)
        self.assertIn("oversized PNG", self.text)
        self.assertIn("timeout --signal=TERM --kill-after=30s 10m", self.text)

    def test_amo_signature_uses_pinned_mozilla_trust_and_full_jar_binding(self) -> None:
        trust_check = self.text.index("Validate committed Mozilla AMO production trust root without secrets")
        readiness = self.text.index("Classify AMO readiness without contacting Mozilla")
        self.assertLess(readiness, trust_check)
        self.assertIn(
            "if: steps.replay.outputs.action == 'reuse' || steps.provider.outputs.action == 'reuse' || steps.provider.outputs.action == 'sign'",
            self.text[trust_check - 120:trust_check + 320],
        )
        self.assertIn("69a98604f9c424d17ede053f68d8265272351aa35d23099b3a8384284309abf0", self.text)
        self.assertIn("c8a80e9afaef4e219b6fb5d7a71d0f101223bac5001ac28f9b0d43dc59a106db", self.text)
        self.assertEqual(
            "69a98604f9c424d17ede053f68d8265272351aa35d23099b3a8384284309abf0",
            hashlib.sha256(AMO_TRUST_ROOT.read_bytes()).hexdigest(),
        )
        if shutil.which("openssl"):
            der = subprocess.run(
                ["openssl", "x509", "-in", AMO_TRUST_ROOT, "-outform", "DER"],
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(
                "c8a80e9afaef4e219b6fb5d7a71d0f101223bac5001ac28f9b0d43dc59a106db",
                hashlib.sha256(der).hexdigest(),
            )
        self.assertIn('"-no-CApath", "-no-CAstore"', self.text)
        self.assertIn('"-verify_depth", "2"', self.text)
        self.assertIn("def signer_cn_matches(subject_output, expected):", self.text)
        self.assertIn('"sep_multiline,utf8"', self.text)
        self.assertNotIn("Code signing : Yes", self.text)
        self.assertNotIn("Code signing CA : No", self.text)
        self.assertIn("SHA256-Digest-Manifest", self.text)
        self.assertIn("signed entry digest mismatch", self.text)
        self.assertNotIn('"-noverify"', self.text)
        self.assertNotIn('"openssl", "smime"', self.text)

    def test_missing_amo_credentials_block_before_any_network_or_tooling(self) -> None:
        program = next(
            item for item in embedded_python_programs(self.text)
            if "AMO_CREDENTIALS_CONFIGURED" in item
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "github-output"
            environment = dict(os.environ)
            environment.update({
                "SELECTED": "true",
                "DRY_RUN": "false",
                "AMO_PUBLISH_ENABLED": "true",
                "AMO_CHANNEL": "unlisted",
                "AMO_CREDENTIALS_CONFIGURED": "false",
            })
            completed = subprocess.run(
                [sys.executable, "-c", program, str(output)], cwd=base, env=environment,
                check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                "blocked_environment",
                json.loads((base / "firefox-result.json").read_text())["status"],
            )
            self.assertEqual("lookup=false\n", output.read_text())
        replay = self.text.index("- name: Resolve a durable AMO replay before provider contact")
        provider = self.text.index("- name: Classify AMO provider readiness after a replay miss")
        signing = self.text.index("- name: Sign the exact tested input through AMO")
        self.assertLess(replay, provider)
        self.assertLess(provider, signing)
        lookup_step = self.text[replay:provider]
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", lookup_step)
        self.assertNotIn("AMO_JWT_ISSUER", lookup_step)
        self.assertNotIn("AMO_JWT_SECRET", lookup_step)
        self.assertNotIn("--token", lookup_step)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required by the trust contract")
    def test_forged_self_signed_amo_cms_is_rejected_by_an_unrelated_trust_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            content = base / "mozilla.sf"
            content.write_text("Signature-Version: 1.0\n\n", encoding="ascii")
            for stem in ("trusted", "forged"):
                generated = subprocess.run(
                    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                     "-keyout", str(base / f"{stem}.key"), "-out", str(base / f"{stem}.pem"),
                     "-subj", f"/CN={stem}", "-days", "1"],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(0, generated.returncode, generated.stderr)
            signature = base / "mozilla.rsa"
            signed = subprocess.run(
                ["openssl", "cms", "-sign", "-binary", "-in", str(content),
                 "-signer", str(base / "forged.pem"), "-inkey", str(base / "forged.key"),
                 "-outform", "DER", "-out", str(signature)],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(0, signed.returncode, signed.stderr)
            forged = subprocess.run(
                ["openssl", "cms", "-verify", "-binary", "-inform", "DER",
                 "-in", str(signature), "-content", str(content),
                 "-CAfile", str(base / "trusted.pem"), "-purpose", "any",
                 "-out", os.devnull],
                check=False, capture_output=True,
            )
            bypass = subprocess.run(
                ["openssl", "cms", "-verify", "-binary", "-inform", "DER",
                 "-in", str(signature), "-content", str(content), "-noverify",
                 "-out", os.devnull],
                check=False, capture_output=True,
            )
            self.assertNotEqual(0, forged.returncode)
            self.assertEqual(0, bypass.returncode)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required by the trust contract")
    def test_cn_only_no_eku_signer_is_accepted_and_duplicate_cn_is_rejected(self) -> None:
        program = next(
            item for item in embedded_python_programs(self.text)
            if "def signer_cn_matches" in item
        )
        tree = ast.parse(program)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "signer_cn_matches"
        )
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "signer-cn", "exec"), namespace)
        matches = namespace["signer_cn_matches"]
        expected = "biomem@bleedingdev.github.io"
        self.assertTrue(matches(f"subject=\n    CN={expected}\n", expected))
        self.assertFalse(matches("subject=\n    CN=wrong@example.test\n", expected))
        self.assertFalse(matches(f"subject=\n    CN={expected}\n    CN=duplicate\n", expected))

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            commands = (
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(base / "root.key"), "-out", str(base / "root.pem"),
                 "-subj", "/CN=test-root", "-days", "1"],
                ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(base / "leaf.key"), "-out", str(base / "leaf.csr"),
                 "-subj", f"/CN={expected}"],
                ["openssl", "x509", "-req", "-in", str(base / "leaf.csr"),
                 "-CA", str(base / "root.pem"), "-CAkey", str(base / "root.key"),
                 "-CAcreateserial", "-out", str(base / "leaf.pem"), "-days", "1"],
            )
            for command in commands:
                completed = subprocess.run(command, check=False, capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            certificate_text = subprocess.run(
                ["openssl", "x509", "-in", str(base / "leaf.pem"), "-noout", "-text"],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertNotIn("Extended Key Usage", certificate_text)
            content = base / "mozilla.sf"
            content.write_text("Signature-Version: 1.0\n\n", encoding="ascii")
            signature = base / "mozilla.rsa"
            subprocess.run(
                ["openssl", "cms", "-sign", "-binary", "-in", str(content),
                 "-signer", str(base / "leaf.pem"), "-inkey", str(base / "leaf.key"),
                 "-certfile", str(base / "root.pem"), "-outform", "DER", "-out", str(signature)],
                check=True, capture_output=True,
            )
            verified = subprocess.run(
                ["openssl", "cms", "-verify", "-binary", "-inform", "DER",
                 "-in", str(signature), "-content", str(content),
                 "-CAfile", str(base / "root.pem"), "-purpose", "any", "-out", os.devnull],
                check=False, capture_output=True,
            )
            self.assertEqual(0, verified.returncode, verified.stderr.decode(errors="replace"))

    def test_jar_binding_rejects_payload_replay(self) -> None:
        program = next(
            item for item in embedded_python_programs(self.text)
            if "def verify_jar_binding" in item
        )
        tree = ast.parse(program)
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"jar_sections", "verify_jar_binding"}
        ]
        import base64
        import hashlib
        import hmac
        namespace = {"base64": base64, "hashlib": hashlib, "hmac": hmac}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "jar-binding", "exec"), namespace)
        payload = {"manifest.json": b'{"version":"0.0.2"}'}
        digest = base64.b64encode(hashlib.sha256(payload["manifest.json"]).digest())
        manifest = b"Manifest-Version: 1.0\n\nName: manifest.json\nSHA256-Digest: " + digest + b"\n\n"
        signature_file = (
            b"Signature-Version: 1.0\nSHA256-Digest-Manifest: "
            + base64.b64encode(hashlib.sha256(manifest).digest()) + b"\n\n"
        )
        verifier = namespace["verify_jar_binding"]
        verifier(manifest, signature_file, payload)
        with self.assertRaisesRegex(ValueError, "signed entry digest mismatch"):
            verifier(manifest, signature_file, {"manifest.json": b"mutated"})

    def test_jar_binding_includes_optional_cose_signature_metadata(self) -> None:
        program = next(
            item for item in embedded_python_programs(self.text)
            if "def verify_jar_binding" in item
        )
        tree = ast.parse(program)
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"jar_sections", "verify_jar_binding"}
        ]
        import base64
        import hashlib
        import hmac
        namespace = {"base64": base64, "hashlib": hashlib, "hmac": hmac}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "jar-binding", "exec"), namespace)
        payload = {
            "manifest.json": b'{"version":"0.0.2"}',
            "META-INF/cose.manifest": b"cose manifest",
            "META-INF/cose.sig": b"cose signature",
        }
        sections = []
        for name, data in payload.items():
            sections.append(
                b"Name: " + name.encode() + b"\nSHA256-Digest: "
                + base64.b64encode(hashlib.sha256(data).digest()) + b"\n\n"
            )
        manifest = b"Manifest-Version: 1.0\n\n" + b"".join(sections)
        signature_file = (
            b"Signature-Version: 1.0\nSHA256-Digest-Manifest: "
            + base64.b64encode(hashlib.sha256(manifest).digest()) + b"\n\n"
        )
        verifier = namespace["verify_jar_binding"]
        verifier(manifest, signature_file, payload)
        with self.assertRaisesRegex(ValueError, "unsigned or missing archive entry"):
            verifier(manifest, signature_file, {"manifest.json": payload["manifest.json"]})

        self.assertIn("jar_bound_payload = dict(signed_payload)", self.text)
        self.assertIn("for canonical_name in present_cose:", self.text)
        self.assertIn(
            "manifest_file.read_bytes(), signature_file.read_bytes(), jar_bound_payload,",
            self.text,
        )

    def test_amo_payload_drift_logs_names_and_hashes_without_file_contents(self) -> None:
        self.assertIn("payload_drift = signed_payload != unsigned_payload", self.text)
        self.assertIn('"added_by_provider": sorted(signed_names - unsigned_names)', self.text)
        self.assertIn('"missing_from_provider": sorted(unsigned_names - signed_names)', self.text)
        self.assertIn('"provider_sha256": hashlib.sha256(signed_payload[name]).hexdigest()', self.text)
        self.assertIn('"unsigned_sha256": hashlib.sha256(unsigned_payload[name]).hexdigest()', self.text)
        self.assertIn("elif payload_drift:", self.text)

    def test_amo_metadata_uses_the_repository_license(self) -> None:
        package = json.loads(TOOLING_PACKAGE.read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))
        self.assertEqual("MIT", package["license"])
        self.assertIn('"version": {"license": "MIT"}', self.text)
        self.assertNotIn("AGPL-3.0-only", self.text)

    def test_safari_public_output_can_only_cross_the_apple_boundary(self) -> None:
        self.assertIn("uses: ./.github/workflows/release-sign-apple.yml", self.text)
        self.assertIn("channel: safari_public", self.text)
        self.assertNotIn("safari-biomem-macos-development-adhoc.zip", self.text)

    def test_environment_obstacles_never_become_pass(self) -> None:
        for obstacle in (
            "missing_provider_configuration",
            "manual_enablement_required",
            "provider_authentication_or_permission_required",
            "store_review_or_manual_action_required",
            "provider_account_review_or_permission_required",
        ):
            self.assertIn(obstacle, self.text)
        self.assertNotIn('status, reason, attempt = "published", "missing_', self.text)
        self.assertNotIn('status, reason, attempt = "published", "manual_', self.text)

    def test_embedded_python_is_syntactically_valid(self) -> None:
        programs = embedded_python_programs(self.text)
        self.assertGreater(len(programs), 0)
        for number, program in enumerate(programs, start=1):
            compile(program, f"publish-browser-channels-heredoc-{number}", "exec")

    def test_unsafe_image_detector_uses_content_not_filename(self) -> None:
        detector_nodes = None
        for program in embedded_python_programs(self.text):
            tree = ast.parse(program)
            functions = {
                node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
            }
            if "unsafe_image_format" in functions:
                detector_nodes = [functions["iso_boxes"], functions["unsafe_image_format"]]
                break
        self.assertIsNotNone(detector_nodes)
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=detector_nodes, type_ignores=[]), "detector", "exec"), namespace)
        detector = namespace["unsafe_image_format"]
        jxl = b"\x00\x00\x00\x0cJXL \r\n\x87\n" + b"\x00\x00\x00\x10ftypjxl \x00\x00\x00\x00"
        samples = {
            "payload.svg": b"icns" + b"\x00" * 12,
            "payload.txt": b"\xff\x0a" + b"\x00" * 14,
            "container.txt": jxl,
        }
        for brand in (b"avif", b"mif1", b"msf1", b"heic", b"heix", b"hevc", b"hevx"):
            samples[f"{brand.decode()}.svg"] = b"\x00\x00\x00\x10ftyp" + brand + b"\x00\x00\x00\x00"
        for conceptual_name, payload in samples.items():
            with self.subTest(name=conceptual_name):
                self.assertIsNotNone(detector(payload))
        self.assertIsNone(detector(b"<svg xmlns='http://www.w3.org/2000/svg'/>") )
        self.assertIsNone(detector(b"plain text"))
        self.assertIsNone(detector(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16))

        classifier = next(
            program for program in embedded_python_programs(self.text)
            if "def unsafe_image_format(data):" in program
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dist = base / "dist"
            dist.mkdir()
            manifest = json.dumps({"manifest_version": 3, "version": "0.0.2"})
            with zipfile.ZipFile(dist / "chrome-biomem.zip", "w") as archive:
                archive.writestr("manifest.json", manifest)
                archive.writestr("disguised.svg", b"\x00\x00\x00\x00JXL ")
            with zipfile.ZipFile(dist / "firefox-biomem-unsigned.xpi", "w") as archive:
                archive.writestr("manifest.json", manifest)
            (dist / "browser-artifacts.json").write_text(json.dumps({"artifacts": [
                {"name": "chrome-biomem.zip", "release_visibility": "public_inspection"},
                {"name": "firefox-biomem-unsigned.xpi", "release_visibility": "ci_only", "distributable": False},
            ]}), encoding="utf-8")
            environment = dict(os.environ)
            environment["RELEASE_VERSION"] = "0.0.2"
            completed = subprocess.run(
                [sys.executable, "-c", classifier], cwd=base, env=environment,
                check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("disguised.svg (jxl-container-malformed)", completed.stderr)

    def test_archive_classifier_rejects_zip_symlink_metadata(self) -> None:
        classifier = next(
            program for program in embedded_python_programs(self.text)
            if "def unsafe_image_format(data):" in program
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dist = base / "dist"
            dist.mkdir()
            manifest = json.dumps({"manifest_version": 3, "version": "0.0.2"})
            with zipfile.ZipFile(dist / "chrome-biomem.zip", "w") as archive:
                archive.writestr("manifest.json", manifest)
                link = zipfile.ZipInfo("escape.txt")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, "/outside/secret")
            with zipfile.ZipFile(dist / "firefox-biomem-unsigned.xpi", "w") as archive:
                archive.writestr("manifest.json", manifest)
            (dist / "browser-artifacts.json").write_text(json.dumps({"artifacts": [
                {"name": "chrome-biomem.zip", "release_visibility": "public_inspection"},
                {"name": "firefox-biomem-unsigned.xpi", "release_visibility": "ci_only", "distributable": False},
            ]}), encoding="utf-8")
            environment = dict(os.environ)
            environment["RELEASE_VERSION"] = "0.0.2"
            completed = subprocess.run(
                [sys.executable, "-c", classifier], cwd=base, env=environment,
                check=False, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("symlink ZIP entry", completed.stderr)

    def test_secrets_are_step_scoped_and_no_private_key_is_uploaded(self) -> None:
        self.assertNotIn("secrets: inherit", self.text)
        self.assertNotIn("dist/keys", self.text)
        self.assertNotIn("chrome-biomem.crx", self.text)
        self.assertIn("CWS_REFRESH_TOKEN: ${{ secrets.CWS_REFRESH_TOKEN }}", self.text)
        self.assertIn("AMO_JWT_SECRET: ${{ secrets.AMO_JWT_SECRET }}", self.text)


class BrowserDocumentationTests(unittest.TestCase):
    def test_docs_match_enforced_amo_certificate_contract(self) -> None:
        text = (ROOT / "docs/channels/browser.md").read_text(encoding="utf-8")
        for value in (
            "pinned PEM and DER hashes",
            "disables\nsystem trust paths",
            "exactly one\ncertificate",
            "exactly one CN equal to that add-on ID",
            "CMS → signature file → manifest → every ZIP entry digest",
            "byte-for-byte identical",
            "does not enforce additional signer EKU, KU, or CA\nproperties",
        ):
            self.assertIn(value, text)
        self.assertNotIn("non-CA code-signing leaf", text)

    def test_docs_name_all_truthful_outcomes(self) -> None:
        text = (ROOT / "docs/channels/browser.md").read_text(encoding="utf-8")
        for value in ("published", "skipped_not_configured", "blocked_environment", "failed"):
            self.assertIn(value, text)
        self.assertIn("Instant", text)
        self.assertIn("never calls Google, Mozilla, or Apple", text)


if __name__ == "__main__":
    unittest.main()
