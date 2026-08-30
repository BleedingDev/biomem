#!/usr/bin/env python3
"""Build, verify, smoke-test, and assemble canonical release artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, BinaryIO
import venv
import zipfile


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import release_policy  # noqa: E402


class ArtifactError(ValueError):
    """A canonical artifact contract violation."""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ArtifactError(f"expected a JSON object in {path}")
    return value


def _policy(path: Path) -> dict[str, Any]:
    value = _json(path)
    release_policy.validate_resolved_policy(value)
    return value


def _sha256(path: Path) -> str:
    return release_policy.sha256(path)


def _regular_file(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ArtifactError(f"{description} must be a regular file: {path}")


def _empty_output(directory: Path) -> None:
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactError(f"output path is not a directory: {directory}")
        if any(directory.iterdir()):
            raise ArtifactError(f"output directory must be empty: {directory}")
    else:
        directory.mkdir(parents=True)


def _copy_stream(source: Path, destination: BinaryIO) -> None:
    with source.open("rb") as stream:
        shutil.copyfileobj(stream, destination, length=1024 * 1024)


def _verification_note(version: str, target: str) -> bytes:
    return (
        f"biomem {version} for {target}\n\n"
        "Run `biomem --version` (or `biomem.exe --version` on Windows) to smoke-test.\n"
        "Verify the downloaded archive with SHA256SUMS.txt and GitHub's artifact "
        "attestation before installation. Provenance does not replace Gatekeeper or "
        "Authenticode identity checks.\n"
    ).encode("utf-8")


def _archive_sources(binary: Path, license_path: Path, version: str, target: str) -> list[tuple[str, Path | bytes, int]]:
    binary_name = "biomem.exe" if target.startswith("windows-") else "biomem"
    return [
        (binary_name, binary, 0o755),
        ("LICENSE", license_path, 0o644),
        ("VERSION", (version + "\n").encode("utf-8"), 0o644),
        ("VERIFY.txt", _verification_note(version, target), 0o644),
    ]


def _write_zip(output: Path, sources: list[tuple[str, Path | bytes, int]]) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source, mode in sources:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            with archive.open(info, "w") as destination:
                if isinstance(source, Path):
                    _copy_stream(source, destination)
                else:
                    destination.write(source)


def _write_tar_gz(output: Path, sources: list[tuple[str, Path | bytes, int]]) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, source, mode in sources:
                    info = tarfile.TarInfo(name)
                    info.mode = mode
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if isinstance(source, Path):
                        info.size = source.stat().st_size
                        with source.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        info.size = len(source)
                        archive.addfile(info, fileobj=_BytesReader(source))


class _BytesReader:
    """Minimal file object used by tarfile without copying metadata bytes again."""

    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._value) - self._offset
        result = self._value[self._offset:self._offset + size]
        self._offset += len(result)
        return result


def package_archive(
    policy_path: Path, target: str, binary: Path, license_path: Path, output_dir: Path,
) -> Path:
    policy = _policy(policy_path)
    target_data = next((item for item in policy["targets"] if item["target"] == target), None)
    if target_data is None:
        raise ArtifactError(f"unknown release target: {target}")
    _regular_file(binary, "standalone binary")
    _regular_file(license_path, "license")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"biomem-{target}.{target_data['archive']}"
    if output.exists() or output.is_symlink():
        raise ArtifactError(f"refusing to overwrite archive: {output}")
    sources = _archive_sources(binary, license_path, policy["version"], target)
    archive_suffix = ".tar.gz" if output.name.endswith(".tar.gz") else output.suffix
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}{archive_suffix}")
    try:
        if target_data["archive"] == "zip":
            _write_zip(temporary, sources)
        elif target_data["archive"] == "tar.gz":
            _write_tar_gz(temporary, sources)
        else:
            raise ArtifactError(f"unsupported archive format: {target_data['archive']}")
        verify_archive(temporary, target, policy["version"])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _safe_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {"", ".", ".."}:
        raise ArtifactError(f"unsafe or nested archive member: {name!r}")


def _expected_archive_members(target: str) -> set[str]:
    binary_name = "biomem.exe" if target.startswith("windows-") else "biomem"
    return {binary_name, "LICENSE", "VERSION", "VERIFY.txt"}


def _check_member_names(names: list[str], target: str) -> None:
    for name in names:
        _safe_member_name(name)
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ArtifactError("archive contains duplicate or case-colliding members")
    expected = _expected_archive_members(target)
    if set(names) != expected:
        raise ArtifactError(
            f"archive member allowlist mismatch: expected {sorted(expected)}, got {sorted(names)}"
        )


def verify_archive(path: Path, target: str, version: str, extract_to: Path | None = None) -> None:
    _regular_file(path, "standalone archive")
    if extract_to is not None:
        _empty_output(extract_to)
    version_bytes: bytes | None = None
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            _check_member_names([item.filename for item in members], target)
            for item in members:
                mode = item.external_attr >> 16
                if item.is_dir() or stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise ArtifactError(f"archive contains a non-regular member: {item.filename}")
                with archive.open(item) as source:
                    if item.filename == "VERSION":
                        version_bytes = source.read()
                    elif extract_to is not None:
                        destination = extract_to / item.filename
                        with destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(destination, mode & 0o777 or 0o644)
                if extract_to is not None and item.filename == "VERSION":
                    (extract_to / item.filename).write_bytes(version_bytes or b"")
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            _check_member_names([item.name for item in members], target)
            for item in members:
                if not item.isreg():
                    raise ArtifactError(f"archive contains a non-regular member: {item.name}")
                source = archive.extractfile(item)
                if source is None:
                    raise ArtifactError(f"cannot read archive member: {item.name}")
                with source:
                    if item.name == "VERSION":
                        version_bytes = source.read()
                        if extract_to is not None:
                            (extract_to / item.name).write_bytes(version_bytes)
                    elif extract_to is not None:
                        destination = extract_to / item.name
                        with destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(destination, item.mode & 0o777)
    else:
        raise ArtifactError(f"unsupported standalone archive: {path.name}")
    if version_bytes != (version + "\n").encode("utf-8"):
        raise ArtifactError("archive VERSION does not match the release policy")


def record_digest(producer: str, source_sha: str, output: Path, artifacts: list[Path]) -> None:
    release_policy.validate_source_sha(source_sha)
    if not producer or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in producer):
        raise ArtifactError(f"invalid producer name: {producer!r}")
    names: set[str] = set()
    records = []
    for path in sorted(artifacts, key=lambda value: value.name):
        _regular_file(path, "producer artifact")
        if path.name in names or path.name.casefold() in {name.casefold() for name in names}:
            raise ArtifactError(f"producer artifact collision: {path.name}")
        names.add(path.name)
        records.append({"name": path.name, "sha256": _sha256(path), "size": path.stat().st_size})
    if not records:
        raise ArtifactError("producer digest requires at least one artifact")
    release_policy.write_json(output, {
        "schema_version": 1,
        "producer": producer,
        "source_sha": source_sha,
        "artifacts": records,
    })


def _files(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactError(f"expected a regular directory: {directory}")
    files = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ArtifactError(f"bundle contains a non-regular file: {path}")
        files.append(path)
    return sorted(files, key=lambda value: value.name)


def _artifact_directories(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactError(f"artifact download root is not a regular directory: {directory}")
    result: list[Path] = []
    folded: set[str] = set()
    for path in sorted(directory.iterdir(), key=lambda value: value.name):
        if path.is_symlink() or not path.is_dir():
            raise ArtifactError(f"artifact download root contains an invalid entry: {path.name}")
        if path.name.casefold() in folded:
            raise ArtifactError("artifact download root contains a case collision")
        folded.add(path.name.casefold())
        result.append(path)
    return result


def _canonical_inventory(policy: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    firefox = directory / f"firefox-biomem-{policy['version']}-amo-signed.xpi"
    return release_policy.verify_canonical_artifacts(
        policy, directory, include_firefox_xpi=firefox.is_file(),
    )


def _canonical_firefox_evidence(
    policy: dict[str, Any], evidence_dir: Path,
) -> tuple[dict[str, Any], bool]:
    files = _files(evidence_dir)
    contract = release_policy.load_contract()
    expected_names = {
        f"{name}.json"
        for name, definition in contract["channels"].items()
        if definition["selection"] == "explicit"
    }
    actual_names = {path.name for path in files}
    with_publication_core = expected_names | {"github_release.json", "direct_cli.json"}
    if frozenset(actual_names) not in {
        frozenset(expected_names), frozenset(with_publication_core),
    }:
        raise ArtifactError("optional channel evidence does not match the exact contract")
    evidence = release_policy.read_evidence(evidence_dir)
    for name, item in evidence.items():
        try:
            canonical_item = release_policy.make_evidence(
                policy,
                name,
                item.get("status", ""),
                item.get("reason_code", ""),
                item.get("receipt"),
            )
        except (KeyError, release_policy.PolicyError) as error:
            raise ArtifactError(f"channel evidence is invalid for {name}") from error
        if item != canonical_item:
            raise ArtifactError(f"channel evidence is not canonical for {name}")
    item = evidence.get("firefox_amo")
    if not isinstance(item, dict):
        raise ArtifactError("Firefox channel evidence is missing")
    canonical = item
    trigger = canonical == {
        "channel": "firefox_amo",
        "selected": True,
        "status": "blocked_environment",
        "reason_code": "signed_output_awaiting_release_attachment",
        "receipt": None,
    }
    return canonical, trigger


def assemble_publication_assets(
    policy_path: Path,
    source_sha: str,
    run_id: str,
    run_attempt: str,
    core_dir: Path,
    chrome_downloads_dir: Path,
    firefox_downloads_dir: Path,
    evidence_dir: Path,
    output_dir: Path,
) -> None:
    policy = _policy(policy_path)
    release_policy.validate_source_sha(source_sha)
    if not run_id.isdecimal() or int(run_id) <= 0:
        raise ArtifactError("GitHub run ID must be a positive decimal value")
    if not run_attempt.isdecimal() or int(run_attempt) <= 0:
        raise ArtifactError("GitHub run attempt must be a positive decimal value")
    core = release_policy.verify_artifacts(policy, core_dir)
    firefox_evidence, attach_firefox = _canonical_firefox_evidence(policy, evidence_dir)

    chrome_directories = _artifact_directories(chrome_downloads_dir)
    expected_chrome_handoff = f"browser-ready-chrome-current-{run_id}-{run_attempt}"
    if [path.name for path in chrome_directories] != [expected_chrome_handoff]:
        raise ArtifactError("expected the exact current-run Chrome publication artifact")
    chrome_directory = chrome_directories[0]
    chrome_files = _files(chrome_directory)
    if [path.name for path in chrome_files] != ["chrome-biomem.zip", "verified-chrome.json"]:
        raise ArtifactError("Chrome publication handoff has an unexpected file set")
    chrome = chrome_directory / "chrome-biomem.zip"
    if chrome.stat().st_size <= 0 or chrome.stat().st_size > 1024 * 1024 * 1024:
        raise ArtifactError("Chrome publication asset size is invalid")
    chrome_sha = _sha256(chrome)
    chrome_metadata = _json(chrome_directory / "verified-chrome.json")
    if chrome_metadata != {
        "filename": "chrome-biomem.zip",
        "run_attempt": int(run_attempt),
        "run_id": int(run_id),
        "sha256": chrome_sha,
        "size": chrome.stat().st_size,
        "source_sha": source_sha,
        "tag": policy["tag"],
        "version": policy["version"],
    }:
        raise ArtifactError("Chrome publication handoff metadata does not match the payload")

    firefox_directories = _artifact_directories(firefox_downloads_dir)
    expected_firefox_handoff = f"browser-firefox-current-{run_id}-{run_attempt}"
    if [path.name for path in firefox_directories] != [expected_firefox_handoff]:
        raise ArtifactError("expected the exact current-run Firefox handoff envelope")
    handoff = firefox_directories[0]
    handoff_files = _files(handoff)
    envelope = _json(handoff / "firefox-handoff.json")
    envelope_keys = {
        "filename", "input_sha256", "ready", "reason", "run_attempt", "run_id",
        "sha256", "size", "source_sha", "status", "tag", "version",
    }
    if set(envelope) != envelope_keys:
        raise ArtifactError("Firefox handoff envelope has unexpected fields")
    if (
        envelope["run_attempt"] != int(run_attempt)
        or envelope["run_id"] != int(run_id)
        or envelope["source_sha"] != source_sha
        or envelope["tag"] != policy["tag"]
        or envelope["version"] != policy["version"]
        or envelope["status"] != firefox_evidence["status"]
        or envelope["reason"] != firefox_evidence["reason_code"]
        or not release_policy.SHA256_RE.fullmatch(str(envelope["input_sha256"]))
        or not isinstance(envelope["ready"], bool)
    ):
        raise ArtifactError("Firefox handoff envelope identity does not match the release")

    signed: Path | None = None
    if attach_firefox:
        filename = f"firefox-biomem-{policy['version']}-amo-signed.xpi"
        if [path.name for path in handoff_files] != [
            filename, "firefox-handoff.json", "verified-firefox-amo.json",
        ]:
            raise ArtifactError("Firefox publication handoff has an unexpected file set")
        signed = handoff / filename
        if signed.stat().st_size <= 0 or signed.stat().st_size > 1024 * 1024 * 1024:
            raise ArtifactError("Firefox signed publication asset size is invalid")
        signed_sha = _sha256(signed)
        if envelope != {
            "filename": filename,
            "input_sha256": envelope["input_sha256"],
            "ready": True,
            "reason": "signed_output_awaiting_release_attachment",
            "run_attempt": int(run_attempt),
            "run_id": int(run_id),
            "sha256": signed_sha,
            "size": signed.stat().st_size,
            "source_sha": source_sha,
            "status": "blocked_environment",
            "tag": policy["tag"],
            "version": policy["version"],
        }:
            raise ArtifactError("Firefox ready envelope does not match the signed payload")
        metadata = _json(handoff / "verified-firefox-amo.json")
        expected_metadata = {
            "filename": filename,
            "input_sha256": envelope["input_sha256"],
            "signed_sha256": signed_sha,
            "size": signed.stat().st_size,
            "source_sha": source_sha,
            "tag": policy["tag"],
            "version": policy["version"],
        }
        if metadata != expected_metadata:
            raise ArtifactError("Firefox publication handoff metadata does not match the payload")
    else:
        if [path.name for path in handoff_files] != ["firefox-handoff.json"]:
            raise ArtifactError("Firefox signed XPI exists without the exact attachment evidence")
        if (
            envelope["ready"] is not False
            or envelope["filename"] is not None
            or envelope["sha256"] is not None
            or envelope["size"] is not None
        ):
            raise ArtifactError("Firefox non-ready handoff envelope contains signed output")

    _empty_output(output_dir)
    for artifact in core:
        source = core_dir / artifact["name"]
        shutil.copyfile(source, output_dir / source.name)
    shutil.copyfile(chrome, output_dir / chrome.name)
    if signed is not None:
        shutil.copyfile(signed, output_dir / signed.name)
    release_policy.verify_canonical_artifacts(
        policy, output_dir, include_firefox_xpi=attach_firefox,
    )


def _bundle_contract(policy: dict[str, Any]) -> dict[str, set[str]]:
    artifacts = policy["expected_core_artifacts"]
    return {
        "python": {item["name"] for item in artifacts if item["kind"].startswith("python_")},
        **{
            target["target"]: {f"biomem-{target['target']}.{target['archive']}"}
            for target in policy["targets"]
        },
    }


def assemble(
    policy_path: Path, source_sha: str, bundles_dir: Path, digests_dir: Path, output_dir: Path,
) -> None:
    policy = _policy(policy_path)
    release_policy.validate_source_sha(source_sha)
    contract = _bundle_contract(policy)
    expected_bundle_dirs = {f"release-core-{producer}" for producer in contract}
    actual_bundle_dirs = {path.name for path in bundles_dir.iterdir()}
    if actual_bundle_dirs != expected_bundle_dirs:
        raise ArtifactError(
            f"core bundle set mismatch: expected {sorted(expected_bundle_dirs)}, "
            f"got {sorted(actual_bundle_dirs)}"
        )
    expected_digest_dirs = {f"producer-digest-{producer}" for producer in contract}
    actual_digest_dirs = {path.name for path in digests_dir.iterdir()}
    if actual_digest_dirs != expected_digest_dirs:
        raise ArtifactError(
            f"producer digest set mismatch: expected {sorted(expected_digest_dirs)}, "
            f"got {sorted(actual_digest_dirs)}"
        )

    resolved: dict[str, Path] = {}
    for producer, expected_names in contract.items():
        bundle_name = f"release-core-{producer}"
        files = _files(bundles_dir / bundle_name)
        actual_names = {path.name for path in files}
        if actual_names != expected_names:
            raise ArtifactError(
                f"{bundle_name} allowlist mismatch: expected {sorted(expected_names)}, "
                f"got {sorted(actual_names)}"
            )
        for path in files:
            if path.name.casefold() in {name.casefold() for name in resolved}:
                raise ArtifactError(f"cross-producer artifact collision: {path.name}")
            resolved[path.name] = path

        digest_dir = digests_dir / f"producer-digest-{producer}"
        digest_files = _files(digest_dir)
        expected_digest_name = f"producer-digest-{producer}.json"
        if [path.name for path in digest_files] != [expected_digest_name]:
            raise ArtifactError(f"unexpected producer digest files for {producer}")
        digest = _json(digest_files[0])
        if digest.get("schema_version") != 1 or digest.get("producer") != producer:
            raise ArtifactError(f"invalid producer digest identity for {producer}")
        if digest.get("source_sha") != source_sha:
            raise ArtifactError(f"producer source SHA mismatch for {producer}")
        records = digest.get("artifacts")
        if (
            not isinstance(records, list)
            or not all(isinstance(item, dict) for item in records)
            or len(records) != len(expected_names)
            or {item.get("name") for item in records} != expected_names
        ):
            raise ArtifactError(f"producer digest allowlist mismatch for {producer}")
        by_name = {item["name"]: item for item in records}
        for path in files:
            record = by_name[path.name]
            if record.get("sha256") != _sha256(path) or record.get("size") != path.stat().st_size:
                raise ArtifactError(f"producer digest mismatch for {path.name}")

    _empty_output(output_dir)
    for expected in policy["expected_core_artifacts"]:
        source = resolved[expected["name"]]
        shutil.copyfile(source, output_dir / source.name)
    release_policy.verify_artifacts(policy, output_dir)


def write_checksums(policy: dict[str, Any], artifacts_dir: Path, output: Path) -> None:
    inventory = _canonical_inventory(policy, artifacts_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{item['sha256']}  {item['name']}\n" for item in inventory),
        encoding="utf-8",
    )


def verify_checksums(policy: dict[str, Any], artifacts_dir: Path, checksums: Path) -> None:
    _regular_file(checksums, "checksum file")
    expected = {
        item["name"]: item["sha256"]
        for item in _canonical_inventory(policy, artifacts_dir)
    }
    actual: dict[str, str] = {}
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if separator != "  " or not name or name in actual:
            raise ArtifactError("SHA256SUMS.txt has invalid or duplicate entries")
        if not all(character in "0123456789abcdef" for character in digest) or len(digest) != 64:
            raise ArtifactError(f"invalid checksum for {name}")
        actual[name] = digest
    if actual != expected:
        raise ArtifactError("SHA256SUMS.txt does not exactly match the canonical assets")


def _release_url(repository: str, tag: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ArtifactError(f"invalid GitHub repository: {repository!r}")
    return f"https://github.com/{repository}/releases/tag/{tag}"


def prepare_payload(
    policy_path: Path,
    source_sha: str,
    repository: str,
    artifacts_dir: Path,
    evidence_dir: Path,
    provenance_evidence: Path,
    output_dir: Path,
) -> None:
    policy = _policy(policy_path)
    release_policy.validate_source_sha(source_sha)
    inventory = _canonical_inventory(policy, artifacts_dir)
    receipt = _release_url(repository, policy["tag"])
    _empty_output(output_dir)
    payload_dir = output_dir / "payload"
    payload_dir.mkdir()
    for artifact in inventory:
        source = artifacts_dir / artifact["name"]
        shutil.copyfile(source, payload_dir / source.name)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-evidence-", dir=output_dir.parent) as temporary:
        staged_evidence = Path(temporary)
        if evidence_dir.exists():
            for path in _files(evidence_dir):
                if path.suffix == ".json":
                    shutil.copyfile(path, staged_evidence / path.name)
        release_policy.write_json(
            staged_evidence / "github_release.json",
            release_policy.make_evidence(
                policy, "github_release", "published", "atomic_release_postcondition", receipt,
            ),
        )
        release_policy.write_json(
            staged_evidence / "direct_cli.json",
            release_policy.make_evidence(
                policy, "direct_cli", "published", "canonical_github_assets", receipt,
            ),
        )
        firefox, attach_firefox = _canonical_firefox_evidence(policy, staged_evidence)
        signed_name = f"firefox-biomem-{policy['version']}-amo-signed.xpi"
        signed_present = (artifacts_dir / signed_name).is_file()
        if attach_firefox != signed_present:
            raise ArtifactError(
                "Firefox attachment evidence and canonical asset inventory disagree"
            )
        if attach_firefox:
            asset_receipt = (
                f"https://github.com/{repository}/releases/download/"
                f"{policy['tag']}/{signed_name}"
            )
            release_policy.write_json(
                staged_evidence / "firefox_amo.json",
                release_policy.make_evidence(
                    policy,
                    "firefox_amo",
                    "published",
                    release_policy.FIREFOX_ATTACHMENT_REASON,
                    asset_receipt,
                ),
            )
        manifest = release_policy.build_manifest(
            policy, staged_evidence, artifacts_dir, source_sha, provenance_evidence,
        )
    release_policy.validate_manifest(manifest)
    release_policy.write_json(payload_dir / "release-manifest.json", manifest)
    write_checksums(policy, artifacts_dir, payload_dir / "SHA256SUMS.txt")
    verify_checksums(policy, artifacts_dir, payload_dir / "SHA256SUMS.txt")
    notes = [
        f"# biomem {policy['version']}",
        "",
        "Alpha release: interfaces, packaging, and installation channels may change "
        "before 1.0.0.",
        "",
        f"Canonical source commit: `{source_sha}`.",
        "",
        "## Install",
        "",
        f"See the [tagged installation guide](https://github.com/{repository}/blob/"
        f"{policy['tag']}/docs/install-from-github-releases.md) for platform archive "
        "selection, verification, and manual browser-extension setup.",
        "",
        "The release always includes five platform archives and `chrome-biomem.zip`.",
        (
            f"It also includes the Mozilla-signed `{signed_name}`."
            if attach_firefox
            else "A Firefox XPI is absent unless Mozilla signing is verified."
        ),
        "",
        "## Verify",
        "",
        "Verify the downloaded files:",
        "",
        "```sh",
        f"gh release verify-asset {policy['tag']} <downloaded-asset-path> --repo {repository}",
        f"gh attestation verify <downloaded-asset-path> --repo {repository}",
        "sha256sum -c SHA256SUMS.txt",
        "```",
        "",
        "GitHub provenance attests the workflow and source commit; it does not replace "
        "Gatekeeper or Authenticode publisher identity.",
        "",
    ]
    (output_dir / "release-notes.md").write_text("\n".join(notes), encoding="utf-8")
    verify_payload(policy_path, source_sha, repository, payload_dir)


def verify_payload(policy_path: Path, source_sha: str, repository: str, payload_dir: Path) -> None:
    policy = _policy(policy_path)
    paths = _files(payload_dir)
    distributable_names = {
        path.name for path in paths
        if path.name not in {"SHA256SUMS.txt", "release-manifest.json"}
    }
    signed_name = f"firefox-biomem-{policy['version']}-amo-signed.xpi"
    expected_distributable = {
        item["name"] for item in release_policy.expected_canonical_artifacts(
            policy["version"], release_policy.load_contract(),
            include_firefox_xpi=signed_name in distributable_names,
        )
    }
    expected_all = expected_distributable | {"SHA256SUMS.txt", "release-manifest.json"}
    if {path.name for path in paths} != expected_all:
        raise ArtifactError("publication payload does not exactly match the canonical allowlist")
    temporary_parent = payload_dir.parent
    with tempfile.TemporaryDirectory(prefix=".release-assets-verify-", dir=temporary_parent) as temporary:
        artifacts = Path(temporary)
        for name in expected_distributable:
            shutil.copyfile(payload_dir / name, artifacts / name)
        inventory = _canonical_inventory(policy, artifacts)
        verify_checksums(policy, artifacts, payload_dir / "SHA256SUMS.txt")
    manifest = _json(payload_dir / "release-manifest.json")
    release_policy.validate_manifest(manifest)
    if manifest["release"]["source_sha"] != source_sha:
        raise ArtifactError("publication manifest source SHA mismatch")
    if manifest["release"]["tag"] != policy["tag"]:
        raise ArtifactError("publication manifest tag mismatch")
    receipt = _release_url(repository, policy["tag"])
    for channel in ("github_release", "direct_cli"):
        if manifest["channels"][channel]["receipt"] != receipt:
            raise ArtifactError(f"publication receipt mismatch for {channel}")
    firefox = manifest["channels"]["firefox_amo"]
    if signed_name in expected_distributable:
        expected_firefox_receipt = (
            f"https://github.com/{repository}/releases/download/"
            f"{policy['tag']}/{signed_name}"
        )
        if (
            firefox["status"] != "published"
            or firefox["reason_code"] != release_policy.FIREFOX_ATTACHMENT_REASON
            or firefox["receipt"] != expected_firefox_receipt
        ):
            raise ArtifactError("Firefox canonical attachment receipt is invalid")
    manifest_digests = {item["name"]: item["sha256"] for item in manifest["artifacts"]}
    if manifest_digests != {item["name"]: item["sha256"] for item in inventory}:
        raise ArtifactError("publication manifest digests do not match the payload")


def _venv_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    if completed.returncode:
        raise ArtifactError(f"smoke command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}")
    return completed.stdout


def smoke_exact(policy_path: Path, target: str, artifacts_dir: Path) -> None:
    policy = _policy(policy_path)
    target_data = next((item for item in policy["targets"] if item["target"] == target), None)
    if target_data is None:
        raise ArtifactError(f"unknown release target: {target}")
    wheel = artifacts_dir / f"biomem_memory-{policy['version']}-py3-none-any.whl"
    sdist = artifacts_dir / f"biomem_memory-{policy['version']}.tar.gz"
    standalone = artifacts_dir / f"biomem-{target}.{target_data['archive']}"
    expected = {wheel.name, sdist.name, standalone.name}
    actual = {path.name for path in _files(artifacts_dir)}
    if actual != expected:
        raise ArtifactError(f"smoke artifact set mismatch: expected {sorted(expected)}, got {sorted(actual)}")

    with tempfile.TemporaryDirectory(prefix="biomem-release-smoke-") as temporary:
        root = Path(temporary)
        extracted = root / "standalone"
        verify_archive(standalone, target, policy["version"], extracted)
        binary_name = "biomem.exe" if target.startswith("windows-") else "biomem"
        binary_output = _run([str(extracted / binary_name), "--version"], root)
        if f"v{policy['version']}" not in binary_output:
            raise ArtifactError("standalone smoke output does not contain the release version")

        for label, artifact in (("wheel", wheel), ("sdist", sdist)):
            environment = root / label
            venv.EnvBuilder(with_pip=True).create(environment)
            _run([
                str(_venv_python(environment)), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-deps", str(artifact.resolve()),
            ], root)
            metadata_smoke = """
import importlib.metadata as metadata
from pathlib import Path
import sys

distribution = metadata.distribution("biomem-memory")
assert distribution.version == sys.argv[1]
scripts = {
    entry.name: entry.value
    for entry in distribution.entry_points
    if entry.group == "console_scripts"
}
assert scripts == {
    "biomem": "memory_module.cli:main",
    "biomem-mcp": "memory_module.mcp_server:main",
    "biomem-server": "memory_module.main:main",
}
assert Path(distribution.locate_file("memory_module/main.py")).is_file()
assert Path(distribution.locate_file("memory_module/assets/biomem_logo.svg")).is_file()
"""
            _run([
                str(_venv_python(environment)), "-c", metadata_smoke, policy["version"],
            ], root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    package = commands.add_parser("package")
    package.add_argument("--policy", type=Path, required=True)
    package.add_argument("--target", required=True)
    package.add_argument("--binary", type=Path, required=True)
    package.add_argument("--license", type=Path, required=True)
    package.add_argument("--output-dir", type=Path, required=True)

    digest = commands.add_parser("record-digest")
    digest.add_argument("--producer", required=True)
    digest.add_argument("--source-sha", required=True)
    digest.add_argument("--output", type=Path, required=True)
    digest.add_argument("artifacts", type=Path, nargs="+")

    assembly = commands.add_parser("assemble")
    assembly.add_argument("--policy", type=Path, required=True)
    assembly.add_argument("--source-sha", required=True)
    assembly.add_argument("--bundles-dir", type=Path, required=True)
    assembly.add_argument("--digests-dir", type=Path, required=True)
    assembly.add_argument("--output-dir", type=Path, required=True)

    publication_assets = commands.add_parser("assemble-publication-assets")
    publication_assets.add_argument("--policy", type=Path, required=True)
    publication_assets.add_argument("--source-sha", required=True)
    publication_assets.add_argument("--run-id", required=True)
    publication_assets.add_argument("--run-attempt", required=True)
    publication_assets.add_argument("--core-dir", type=Path, required=True)
    publication_assets.add_argument("--chrome-downloads-dir", type=Path, required=True)
    publication_assets.add_argument("--firefox-downloads-dir", type=Path, required=True)
    publication_assets.add_argument("--evidence-dir", type=Path, required=True)
    publication_assets.add_argument("--output-dir", type=Path, required=True)

    checksums = commands.add_parser("checksums")
    checksums.add_argument("--policy", type=Path, required=True)
    checksums.add_argument("--artifacts-dir", type=Path, required=True)
    checksums.add_argument("--output", type=Path, required=True)

    smoke = commands.add_parser("smoke")
    smoke.add_argument("--policy", type=Path, required=True)
    smoke.add_argument("--target", required=True)
    smoke.add_argument("--artifacts-dir", type=Path, required=True)

    prepare = commands.add_parser("prepare-payload")
    prepare.add_argument("--policy", type=Path, required=True)
    prepare.add_argument("--source-sha", required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--artifacts-dir", type=Path, required=True)
    prepare.add_argument("--evidence-dir", type=Path, required=True)
    prepare.add_argument("--provenance-evidence", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)

    verify = commands.add_parser("verify-payload")
    verify.add_argument("--policy", type=Path, required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--payload-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "package":
            package_archive(args.policy, args.target, args.binary, args.license, args.output_dir)
        elif args.command == "record-digest":
            record_digest(args.producer, args.source_sha, args.output, args.artifacts)
        elif args.command == "assemble":
            assemble(
                args.policy, args.source_sha, args.bundles_dir, args.digests_dir,
                args.output_dir,
            )
        elif args.command == "assemble-publication-assets":
            assemble_publication_assets(
                args.policy,
                args.source_sha,
                args.run_id,
                args.run_attempt,
                args.core_dir,
                args.chrome_downloads_dir,
                args.firefox_downloads_dir,
                args.evidence_dir,
                args.output_dir,
            )
        elif args.command == "checksums":
            write_checksums(_policy(args.policy), args.artifacts_dir, args.output)
            verify_checksums(_policy(args.policy), args.artifacts_dir, args.output)
        elif args.command == "smoke":
            smoke_exact(args.policy, args.target, args.artifacts_dir)
        elif args.command == "prepare-payload":
            prepare_payload(
                args.policy, args.source_sha, args.repository, args.artifacts_dir,
                args.evidence_dir, args.provenance_evidence, args.output_dir,
            )
        else:
            verify_payload(
                args.policy, args.source_sha, args.repository, args.payload_dir,
            )
    except (ArtifactError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"canonical artifact error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
