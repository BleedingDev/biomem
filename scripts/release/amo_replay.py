#!/usr/bin/env python3
"""Resolve a durable, exact AMO signing result before contacting AMO again."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import secrets
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_SIGNED_XPI_BYTES = 50 * 1024 * 1024
METADATA_NAME = "verified-firefox-amo.json"


@dataclass(frozen=True)
class ReplayResult:
    action: str
    status: str
    reason: str


class ReplayLookupUnavailable(Exception):
    """The GitHub artifact service could not be queried safely."""


class ReplayArtifactInvalid(Exception):
    """A retained replay artifact conflicts with the expected identity."""


class ReplayArtifactUnavailable(Exception):
    """An exact prior result existed but retention or deletion made it unavailable."""


def durable_artifact_name(tag: str, source_sha: str, input_sha256: str) -> str:
    return f"browser-durable-firefox-amo-signed-{tag}-{source_sha}-{input_sha256}"


class SafeRedirectHandler(HTTPRedirectHandler):
    """Never forward GitHub credentials to a cross-origin artifact blob URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if old.scheme == "https" and new.scheme != "https":
            raise HTTPError(newurl, code, "refusing HTTPS redirect downgrade", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            redirected.remove_header("Authorization")
        return redirected


def _safe_urlopen(request: Request, timeout: int):
    return build_opener(SafeRedirectHandler()).open(request, timeout=timeout)


def _request_bytes(
    url: str,
    token: str,
    *,
    accept: str,
    opener: Callable[..., object],
    max_bytes: int,
    missing_is_artifact_unavailable: bool = False,
    authorization_scheme: str = "Bearer",
) -> bytes:
    request = Request(url, headers={
        "Accept": accept,
        "X-GitHub-Api-Version": "2026-03-10",
    })
    request.add_unredirected_header("Authorization", f"{authorization_scheme} {token}")
    try:
        with opener(request, timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as error:
                    raise ReplayLookupUnavailable("invalid GitHub Content-Length") from error
                if declared_size < 0 or declared_size > max_bytes:
                    raise ReplayLookupUnavailable("GitHub response exceeds the size limit")
            payload = response.read(max_bytes + 1)
    except HTTPError as error:
        if missing_is_artifact_unavailable and error.code in {404, 410}:
            raise ReplayArtifactUnavailable("durable AMO replay was deleted") from error
        raise ReplayLookupUnavailable("GitHub artifact lookup unavailable") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ReplayLookupUnavailable("GitHub artifact lookup unavailable") from error
    if len(payload) > max_bytes:
        raise ReplayLookupUnavailable("GitHub response exceeds the size limit")
    return payload


def _load_artifact_index(
    api_url: str,
    repository: str,
    token: str,
    artifact_name: str,
    *,
    opener: Callable[..., object],
) -> list[dict[str, object]]:
    query = urlencode({"name": artifact_name, "per_page": "100"})
    url = f"{api_url.rstrip('/')}/repos/{quote(repository, safe='/')}/actions/artifacts?{query}"
    raw = _request_bytes(
        url, token, accept="application/vnd.github+json", opener=opener, max_bytes=2 * 1024 * 1024,
    )
    try:
        value = json.loads(raw)
        total = value["total_count"]
        artifacts = value["artifacts"]
        if not isinstance(total, int) or total < 0 or not isinstance(artifacts, list):
            raise TypeError
        if total != len(artifacts):
            raise ReplayArtifactInvalid("artifact index is incomplete or ambiguous")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise TypeError
            if artifact.get("name") != artifact_name or not isinstance(artifact.get("expired"), bool):
                raise ReplayArtifactInvalid("artifact index contains a conflicting identity")
    except ReplayArtifactInvalid:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReplayArtifactInvalid("malformed GitHub artifact index") from error
    return artifacts


def _validated_artifact_download(
    artifact: dict[str, object],
    *,
    api_url: str,
    repository: str,
    source_sha: str,
) -> tuple[str, str, int]:
    artifact_id = artifact.get("id")
    size = artifact.get("size_in_bytes")
    digest = artifact.get("digest")
    workflow_run = artifact.get("workflow_run")
    if not isinstance(artifact_id, int) or artifact_id <= 0:
        raise ReplayArtifactInvalid("invalid durable AMO artifact identifier")
    if not isinstance(size, int) or size <= 0 or size > MAX_DOWNLOAD_BYTES:
        raise ReplayArtifactInvalid("durable AMO artifact size is invalid")
    if not isinstance(workflow_run, dict) or workflow_run.get("head_sha") != source_sha:
        raise ReplayArtifactInvalid("durable AMO artifact source identity mismatch")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ReplayArtifactInvalid("durable AMO artifact digest is missing")
    digest_hex = digest.removeprefix("sha256:")
    if len(digest_hex) != 64 or any(character not in "0123456789abcdef" for character in digest_hex):
        raise ReplayArtifactInvalid("durable AMO artifact digest is invalid")
    expected_url = (
        f"{api_url.rstrip('/')}/repos/{quote(repository, safe='/')}"
        f"/actions/artifacts/{artifact_id}/zip"
    )
    if artifact.get("archive_download_url") != expected_url:
        raise ReplayArtifactInvalid("durable AMO artifact download identity mismatch")
    return expected_url, digest_hex, size


def _validate_inner_xpi_structure(signed_bytes: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(signed_bytes)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 512:
                raise ReplayArtifactInvalid("signed XPI member count is invalid")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ReplayArtifactInvalid("signed XPI has duplicate entries")
            total_size = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                if (
                    info.filename.startswith("/")
                    or "\\" in info.filename
                    or ".." in path.parts
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise ReplayArtifactInvalid("signed XPI contains an unsafe entry")
                if info.file_size < 0 or info.file_size > 20 * 1024 * 1024:
                    raise ReplayArtifactInvalid("signed XPI member exceeds the size limit")
                total_size += info.file_size
                if total_size > MAX_SIGNED_XPI_BYTES:
                    raise ReplayArtifactInvalid("signed XPI expands beyond the size limit")
    except ReplayArtifactInvalid:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ReplayArtifactInvalid("signed XPI is not a valid ZIP") from error


def _validate_and_extract(
    archive_path: Path,
    output_dir: Path,
    *,
    version: str,
    tag: str,
    source_sha: str,
    input_sha256: str,
) -> None:
    expected_filename = f"firefox-biomem-{version}-amo-signed.xpi"
    expected_names = {expected_filename, METADATA_NAME}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != expected_names:
                raise ReplayArtifactInvalid("replay artifact has an unexpected file set")
            for info in infos:
                path = PurePosixPath(info.filename)
                if (
                    info.is_dir()
                    or info.filename.startswith("/")
                    or "\\" in info.filename
                    or ".." in path.parts
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise ReplayArtifactInvalid("replay artifact contains an unsafe entry")
                if info.file_size < 0 or info.file_size > MAX_SIGNED_XPI_BYTES:
                    raise ReplayArtifactInvalid("replay artifact member exceeds the size limit")
            metadata_raw = archive.read(METADATA_NAME)
            signed_bytes = archive.read(expected_filename)
    except ReplayArtifactInvalid:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        raise ReplayArtifactInvalid("replay artifact is not a valid ZIP") from error

    try:
        metadata = json.loads(metadata_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayArtifactInvalid("replay metadata is not valid JSON") from error
    expected_metadata = {
        "filename": expected_filename,
        "input_sha256": input_sha256,
        "signed_sha256": hashlib.sha256(signed_bytes).hexdigest(),
        "size": len(signed_bytes),
        "source_sha": source_sha,
        "tag": tag,
        "version": version,
    }
    if metadata != expected_metadata:
        raise ReplayArtifactInvalid("replay metadata does not match the immutable identity")

    _validate_inner_xpi_structure(signed_bytes)

    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / expected_filename).write_bytes(signed_bytes)


def resolve_replay(
    *,
    api_url: str,
    repository: str,
    token: str,
    artifact_name: str,
    version: str,
    tag: str,
    source_sha: str,
    input_sha256: str,
    output_dir: Path,
    opener: Callable[..., object] = _safe_urlopen,
) -> ReplayResult:
    if not token:
        return ReplayResult("stop", "blocked_environment", "github_actions_permission_required")
    try:
        artifacts = _load_artifact_index(
            api_url, repository, token, artifact_name, opener=opener,
        )
        if not artifacts:
            return ReplayResult("provider_lookup", "ready", "no_durable_amo_replay_found")
        if len(artifacts) != 1:
            raise ReplayArtifactInvalid("multiple durable AMO replay artifacts found")
        if artifacts[0]["expired"] is True:
            raise ReplayArtifactUnavailable("durable AMO replay expired")
        download_url, expected_outer_digest, expected_outer_size = _validated_artifact_download(
            artifacts[0], api_url=api_url, repository=repository, source_sha=source_sha,
        )
        archive_bytes = _request_bytes(
            download_url,
            token,
            accept="application/vnd.github+json",
            opener=opener,
            max_bytes=MAX_DOWNLOAD_BYTES,
            missing_is_artifact_unavailable=True,
        )
        if len(archive_bytes) != expected_outer_size:
            raise ReplayArtifactInvalid("durable AMO outer artifact size mismatch")
        if hashlib.sha256(archive_bytes).hexdigest() != expected_outer_digest:
            raise ReplayArtifactInvalid("durable AMO outer artifact digest mismatch")
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "artifact.zip"
            archive_path.write_bytes(archive_bytes)
            _validate_and_extract(
                archive_path,
                output_dir,
                version=version,
                tag=tag,
                source_sha=source_sha,
                input_sha256=input_sha256,
            )
    except ReplayLookupUnavailable:
        return ReplayResult("stop", "blocked_environment", "github_actions_lookup_unavailable")
    except ReplayArtifactUnavailable:
        return ReplayResult("provider_lookup", "ready", "durable_amo_replay_expired_or_deleted")
    except ReplayArtifactInvalid:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return ReplayResult("stop", "failed", "durable_amo_replay_invalid_or_conflicting")
    return ReplayResult("reuse", "ready", "durable_amo_replay_verified")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _amo_jwt(issuer: str, secret: str, *, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    header = _base64url(json.dumps(
        {"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True,
    ).encode())
    payload = _base64url(json.dumps({
        "exp": issued_at + 300,
        "iat": issued_at,
        "iss": issuer,
        "jti": secrets.token_hex(16),
    }, separators=(",", ":"), sort_keys=True).encode())
    body = f"{header}.{payload}"
    signature = _base64url(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def resolve_amo_provider(
    *,
    api_url: str,
    issuer: str,
    secret: str,
    guid: str,
    version: str,
    output_dir: Path,
    allow_sign: bool,
    opener: Callable[..., object] = _safe_urlopen,
) -> ReplayResult:
    if not issuer or not secret:
        return ReplayResult("stop", "blocked_environment", "missing_provider_configuration")
    token = _amo_jwt(issuer, secret)
    version_url = (
        f"{api_url.rstrip('/')}/addons/addon/{quote(guid, safe='')}/versions/{quote(version, safe='')}/"
    )
    request = Request(version_url, headers={"Accept": "application/json"})
    request.add_unredirected_header("Authorization", f"JWT {token}")
    try:
        with opener(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as error:
        if error.code == 404 and error.geturl() == version_url:
            if allow_sign:
                return ReplayResult("sign", "ready", "amo_exact_version_not_found")
            return ReplayResult("stop", "blocked_environment", "amo_version_visibility_unconfirmed")
        if error.code == 404:
            return ReplayResult("stop", "blocked_environment", "provider_redirected_resource_unavailable")
        if error.code in {401, 403}:
            return ReplayResult("stop", "blocked_environment", "provider_authentication_or_permission_required")
        if error.code == 429 or 500 <= error.code <= 599:
            return ReplayResult("stop", "blocked_environment", "provider_rate_limit_or_unavailable")
        return ReplayResult("stop", "failed", "unexpected_amo_version_response")
    except (URLError, TimeoutError, OSError):
        return ReplayResult("stop", "blocked_environment", "provider_transport_unavailable")
    if len(raw) > 2 * 1024 * 1024:
        return ReplayResult("stop", "failed", "malformed_amo_version_response")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError
        if value.get("version") != version:
            raise ReplayArtifactInvalid("AMO version identity mismatch")
        channel = value.get("channel")
        is_disabled = value.get("is_disabled")
        if not isinstance(channel, str) or not isinstance(is_disabled, bool):
            raise TypeError
        if channel != "unlisted":
            raise ReplayArtifactInvalid("AMO version channel mismatch")
        if is_disabled:
            return ReplayResult("stop", "blocked_environment", "amo_version_disabled")
        signed_file = value.get("file")
        if not isinstance(signed_file, dict):
            raise TypeError
        file_status = signed_file.get("status")
        if not isinstance(file_status, str):
            raise TypeError
        if file_status != "public":
            return ReplayResult("stop", "blocked_environment", "amo_version_processing_or_review_pending")
        digest = signed_file.get("hash")
        download_url = signed_file.get("url")
        expected_size = signed_file.get("size")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0 or expected_size > MAX_SIGNED_XPI_BYTES:
            raise ReplayArtifactInvalid("AMO signed file size invalid")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ReplayArtifactInvalid("AMO signed file digest missing")
        expected_digest = digest.removeprefix("sha256:")
        if len(expected_digest) != 64 or any(c not in "0123456789abcdef" for c in expected_digest):
            raise ReplayArtifactInvalid("AMO signed file digest invalid")
        parsed = urlsplit(download_url) if isinstance(download_url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.netloc != "addons.mozilla.org"
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ReplayArtifactInvalid("AMO signed file download identity invalid")
        signed_bytes = _request_bytes(
            download_url,
            token,
            accept="application/octet-stream",
            opener=opener,
            max_bytes=MAX_SIGNED_XPI_BYTES,
            authorization_scheme="JWT",
        )
        if hashlib.sha256(signed_bytes).hexdigest() != expected_digest:
            raise ReplayArtifactInvalid("AMO signed file digest mismatch")
        if len(signed_bytes) != expected_size:
            raise ReplayArtifactInvalid("AMO signed file size mismatch")
        _validate_inner_xpi_structure(signed_bytes)
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / f"firefox-biomem-{version}-amo-signed.xpi").write_bytes(signed_bytes)
    except ReplayLookupUnavailable:
        return ReplayResult("stop", "blocked_environment", "provider_transport_unavailable")
    except (ReplayArtifactInvalid, KeyError, TypeError, ValueError, json.JSONDecodeError):
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return ReplayResult("stop", "failed", "amo_exact_version_invalid_or_conflicting")
    return ReplayResult("reuse", "ready", "verified_existing_amo_version")


def _write_outputs(
    path: Path,
    result: ReplayResult,
    artifact_name: str,
) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"action={result.action}\n")
        output.write(f"status={result.status}\n")
        output.write(f"reason={result.reason}\n")
        output.write(f"artifact_name={artifact_name}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--provider-check", action="store_true")
    parser.add_argument("--amo-api-url", default="https://addons.mozilla.org/api/v5")
    parser.add_argument("--allow-sign", choices=("true", "false"), default="true")
    args = parser.parse_args()

    artifact_name = durable_artifact_name(args.tag, args.source_sha, args.input_sha256)
    if args.provider_check:
        result = resolve_amo_provider(
            api_url=args.amo_api_url,
            issuer=os.environ.get("AMO_JWT_ISSUER", ""),
            secret=os.environ.get("AMO_JWT_SECRET", ""),
            guid="biomem@bleedingdev.github.io",
            version=args.version,
            output_dir=args.output_dir,
            allow_sign=args.allow_sign == "true",
        )
    else:
        result = resolve_replay(
            api_url=args.api_url,
            repository=args.repository,
            token=os.environ.get("GITHUB_TOKEN", ""),
            artifact_name=artifact_name,
            version=args.version,
            tag=args.tag,
            source_sha=args.source_sha,
            input_sha256=args.input_sha256,
            output_dir=args.output_dir,
        )
    _write_outputs(args.github_output, result, artifact_name)
    if args.provider_check and result.action != "stop":
        args.result.write_text(json.dumps({
            "attempt": False,
            "channel": "unlisted",
            "reason": result.reason,
            "status": result.status,
        }, sort_keys=True) + "\n")
    elif result.action == "stop":
        args.result.write_text(json.dumps({
            "attempt": False,
            "channel": "unlisted",
            "reason": result.reason,
            "status": result.status,
        }, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
