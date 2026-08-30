#!/usr/bin/env python3
"""Generate hash-pinned WinGet and Scoop metadata from the canonical ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
RELEASE_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(RELEASE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(RELEASE_SCRIPTS_DIR))

import canonical_artifacts  # noqa: E402
import release_policy  # noqa: E402


WINDOWS_TARGET = "windows-x86_64"
WINDOWS_ARCHIVE = "biomem-windows-x86_64.zip"
WINGET_MANIFEST_VERSION = "1.12.0"
REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
)
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class WindowsChannelError(ValueError):
    """The Windows channel metadata contract was violated."""


def _load_policy(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WindowsChannelError(f"policy must be a regular file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WindowsChannelError("release policy must be a JSON object")
    release_policy.validate_resolved_policy(value)
    return value


def _repository(value: str) -> str:
    if not REPOSITORY_RE.fullmatch(value):
        raise WindowsChannelError(f"invalid GitHub repository: {value!r}")
    owner, repository = value.split("/", 1)
    if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
        raise WindowsChannelError(f"invalid GitHub repository: {value!r}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_digest(archive: Path, expected_sha256: str) -> str:
    if archive.is_symlink() or not archive.is_file() or archive.name != WINDOWS_ARCHIVE:
        raise WindowsChannelError(
            f"archive must be the regular canonical asset {WINDOWS_ARCHIVE}: {archive}"
        )
    if not SHA256_RE.fullmatch(expected_sha256):
        raise WindowsChannelError("expected SHA-256 must contain exactly 64 hexadecimal digits")
    expected = expected_sha256.lower()
    actual = _sha256(archive)
    if actual != expected:
        raise WindowsChannelError(
            f"canonical archive SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def _empty_output(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise WindowsChannelError(f"output path must be a regular directory: {path}")
        if any(path.iterdir()):
            raise WindowsChannelError(f"output directory must be empty: {path}")
    else:
        path.mkdir(parents=True)


def _asset_url(repository: str, tag: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/{tag}/"
        f"{WINDOWS_ARCHIVE}"
    )


def _winget_documents(
    identifier: str,
    version: str,
    repository: str,
    tag: str,
    digest: str,
) -> dict[str, str]:
    schema_base = "https://aka.ms/winget-manifest"
    asset_url = _asset_url(repository, tag)
    repository_url = f"https://github.com/{repository}"
    release_url = f"{repository_url}/releases/tag/{tag}"
    publisher = identifier.split(".", 1)[0]
    common = f"PackageIdentifier: {identifier}\nPackageVersion: {version}\n"
    return {
        f"{identifier}.yaml": (
            f"# yaml-language-server: $schema={schema_base}.version.{WINGET_MANIFEST_VERSION}.schema.json\n"
            f"{common}"
            "DefaultLocale: en-US\n"
            "ManifestType: version\n"
            f"ManifestVersion: {WINGET_MANIFEST_VERSION}\n"
        ),
        f"{identifier}.installer.yaml": (
            f"# yaml-language-server: $schema={schema_base}.installer.{WINGET_MANIFEST_VERSION}.schema.json\n"
            f"{common}"
            "InstallerType: zip\n"
            "NestedInstallerType: portable\n"
            "Scope: user\n"
            "UpgradeBehavior: install\n"
            "Commands:\n"
            "- biomem\n"
            "Installers:\n"
            "- Architecture: x64\n"
            f"  InstallerUrl: {asset_url}\n"
            f"  InstallerSha256: {digest.upper()}\n"
            "  NestedInstallerFiles:\n"
            "  - RelativeFilePath: biomem.exe\n"
            "    PortableCommandAlias: biomem\n"
            "ManifestType: installer\n"
            f"ManifestVersion: {WINGET_MANIFEST_VERSION}\n"
        ),
        f"{identifier}.locale.en-US.yaml": (
            f"# yaml-language-server: $schema={schema_base}.defaultLocale.{WINGET_MANIFEST_VERSION}.schema.json\n"
            f"{common}"
            "PackageLocale: en-US\n"
            f"Publisher: {publisher}\n"
            f"PublisherUrl: https://github.com/{repository.split('/', 1)[0]}\n"
            f"PackageUrl: {repository_url}\n"
            "PackageName: biomem\n"
            "License: MIT\n"
            f"LicenseUrl: {repository_url}/blob/{tag}/LICENSE\n"
            "ShortDescription: Portable local memory for LLM conversations.\n"
            f"ReleaseNotesUrl: {release_url}\n"
            "ManifestType: defaultLocale\n"
            f"ManifestVersion: {WINGET_MANIFEST_VERSION}\n"
        ),
    }


def _scoop_manifest(version: str, repository: str, tag: str, digest: str) -> dict[str, Any]:
    repository_url = f"https://github.com/{repository}"
    return {
        "$schema": "https://raw.githubusercontent.com/ScoopInstaller/Scoop/master/schema.json",
        "version": version,
        "description": "Portable local memory for LLM conversations.",
        "homepage": repository_url,
        "license": {
            "identifier": "MIT",
            "url": f"{repository_url}/blob/{tag}/LICENSE",
        },
        "architecture": {
            "64bit": {
                "url": _asset_url(repository, tag),
                "hash": digest,
            }
        },
        "bin": "biomem.exe",
    }


def generate(
    policy_path: Path,
    repository: str,
    archive: Path,
    expected_sha256: str,
    output_dir: Path,
) -> dict[str, Any]:
    policy = _load_policy(policy_path)
    repository = _repository(repository)
    digest = _archive_digest(archive, expected_sha256)

    # Reuse the canonical producer's archive allowlist and VERSION verification.
    canonical_artifacts.verify_archive(archive, WINDOWS_TARGET, policy["version"])
    winget_id = policy["package_identifiers"]["winget"]
    scoop_id = policy["package_identifiers"]["scoop"]
    _empty_output(output_dir)

    winget_dir = output_dir / "winget" / policy["version"]
    scoop_dir = output_dir / "scoop"
    winget_dir.mkdir(parents=True)
    scoop_dir.mkdir()
    for name, contents in _winget_documents(
        winget_id, policy["version"], repository, policy["tag"], digest
    ).items():
        (winget_dir / name).write_text(contents, encoding="utf-8", newline="\n")
    scoop_path = scoop_dir / f"{scoop_id}.json"
    scoop_path.write_text(
        json.dumps(
            _scoop_manifest(policy["version"], repository, policy["tag"], digest),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "archive": WINDOWS_ARCHIVE,
        "sha256": digest,
        "url": _asset_url(repository, policy["tag"]),
        "winget": str(winget_dir),
        "scoop": str(scoop_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate(
            args.policy,
            args.repository,
            args.archive,
            args.expected_sha256,
            args.output_dir,
        )
    except (WindowsChannelError, release_policy.PolicyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Windows channel metadata error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
