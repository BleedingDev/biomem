#!/usr/bin/env python3
"""Publish a complete canonical payload through one retry-safe draft transition."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Protocol


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import canonical_artifacts  # noqa: E402


class PublicationError(RuntimeError):
    """A release state is unsafe to mutate or publish."""


class BlockedEnvironment(PublicationError):
    """GitHub did not expose a capability required for safe verification."""


@dataclass(frozen=True)
class RemoteAsset:
    name: str
    size: int
    digest: str | None


@dataclass(frozen=True)
class RemoteRelease:
    tag: str
    draft: bool
    immutable: bool
    url: str
    body: str
    assets: tuple[RemoteAsset, ...]


class ReleaseClient(Protocol):
    def immutable_releases_enabled(self, repository: str) -> bool: ...

    def resolve_tag_commit(self, repository: str, tag: str) -> str: ...

    def view(self, repository: str, tag: str) -> RemoteRelease | None: ...

    def create_draft(
        self, repository: str, tag: str, source_sha: str, title: str,
        prerelease: bool, notes_file: Path,
    ) -> None: ...

    def upload(self, repository: str, tag: str, path: Path) -> None: ...

    def publish(self, repository: str, tag: str) -> None: ...

    def dispatch(self, repository: str, tag: str, source_sha: str) -> None: ...


class GhClient:
    """Small gh CLI adapter; state decisions remain independently testable."""

    def __init__(self, token: str, immutable_releases_token: str) -> None:
        if not token:
            raise BlockedEnvironment("GH_TOKEN is required for GitHub publication")
        self._environment = {**os.environ, "GH_TOKEN": token}
        self._immutable_environment = (
            {**os.environ, "GH_TOKEN": immutable_releases_token}
            if immutable_releases_token else None
        )

    def _run(self, arguments: list[str], *, allow_not_found: bool = False) -> str | None:
        completed = subprocess.run(
            ["gh", *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment,
        )
        if completed.returncode == 0:
            return completed.stdout
        if allow_not_found and re.search(r"(?:HTTP 404|release not found)", completed.stderr, re.I):
            return None
        raise BlockedEnvironment(
            f"gh command failed ({completed.returncode}): {' '.join(arguments)}: "
            f"{completed.stderr.strip()}"
        )

    def immutable_releases_enabled(self, repository: str) -> bool:
        if self._immutable_environment is None:
            raise BlockedEnvironment(
                "IMMUTABLE_RELEASES_TOKEN with repository Administration:read is required"
            )
        completed = subprocess.run(
            [
                "gh", "api", "-H", "X-GitHub-Api-Version: 2026-03-10",
                f"repos/{repository}/immutable-releases",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._immutable_environment,
        )
        if completed.returncode != 0:
            if re.search(r"HTTP 404", completed.stderr, re.I):
                return False
            raise BlockedEnvironment(
                "immutable releases setting is unavailable: " + completed.stderr.strip()
            )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict) or value.get("enabled") is not True:
            raise BlockedEnvironment("immutable releases API returned no enabled guarantee")
        return True

    def resolve_tag_commit(self, repository: str, tag: str) -> str:
        value = self._run(["api", f"repos/{repository}/commits/{tag}", "--jq", ".sha"])
        assert value is not None
        return value.strip()

    def view(self, repository: str, tag: str) -> RemoteRelease | None:
        owner, name = repository.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$tag:String!){"
            "repository(owner:$owner,name:$name){release(tagName:$tag){databaseId}}}"
        )
        discovery_raw = self._run([
            "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"tag={tag}",
        ])
        assert discovery_raw is not None
        discovery = json.loads(discovery_raw)
        if discovery.get("errors"):
            raise BlockedEnvironment("GitHub GraphQL release discovery returned errors")
        repository_value = discovery.get("data", {}).get("repository")
        if not isinstance(repository_value, dict):
            raise BlockedEnvironment("GitHub GraphQL did not expose the repository")
        release_value = repository_value.get("release")
        if release_value is None:
            return None
        if not isinstance(release_value, dict) or not isinstance(
            release_value.get("databaseId"), int
        ):
            raise BlockedEnvironment("GitHub GraphQL did not expose the release database ID")
        raw = self._run(
            ["api", f"repos/{repository}/releases/{release_value['databaseId']}"],
            allow_not_found=True,
        )
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value.get("immutable"), bool):
            raise BlockedEnvironment("GitHub release API did not expose immutable state")
        assets = value.get("assets")
        if not isinstance(assets, list):
            raise BlockedEnvironment("GitHub release API did not expose asset inventory")
        return RemoteRelease(
            tag=str(value.get("tag_name", "")),
            draft=bool(value.get("draft")),
            immutable=value["immutable"],
            url=str(value.get("html_url", "")),
            body=str(value.get("body") or ""),
            assets=tuple(
                RemoteAsset(
                    name=str(item.get("name", "")),
                    size=item.get("size") if isinstance(item.get("size"), int) else -1,
                    digest=item.get("digest") if isinstance(item.get("digest"), str) else None,
                )
                for item in assets
            ),
        )

    def create_draft(
        self, repository: str, tag: str, source_sha: str, title: str,
        prerelease: bool, notes_file: Path,
    ) -> None:
        arguments = [
            "release", "create", tag, "--repo", repository, "--verify-tag", "--draft",
            "--target", source_sha, "--title", title, "--notes-file", str(notes_file),
        ]
        if prerelease:
            arguments.append("--prerelease")
        self._run(arguments)

    def upload(self, repository: str, tag: str, path: Path) -> None:
        self._run(["release", "upload", tag, str(path), "--repo", repository])

    def publish(self, repository: str, tag: str) -> None:
        self._run(["release", "edit", tag, "--repo", repository, "--draft=false"])

    def dispatch(self, repository: str, tag: str, source_sha: str) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="biomem-release-dispatch-", suffix=".json",
            delete=False,
        ) as temporary:
            payload_path = Path(temporary.name)
            json.dump({
                "event_type": "canonical_release_published",
                "client_payload": {"tag": tag, "source_sha": source_sha},
            }, temporary, separators=(",", ":"))
            temporary.write("\n")
        try:
            self._run([
                "api", "--method", "POST", f"repos/{repository}/dispatches",
                "--input", str(payload_path),
            ])
        finally:
            payload_path.unlink(missing_ok=True)


def ownership_marker(repository: str, tag: str, source_sha: str) -> str:
    return (
        f"<!-- biomem-release-owner schema=1 repository={repository} "
        f"tag={tag} source_sha={source_sha} -->"
    )


def _local_inventory(payload_dir: Path) -> dict[str, RemoteAsset]:
    inventory: dict[str, RemoteAsset] = {}
    for path in canonical_artifacts._files(payload_dir):
        folded = path.name.casefold()
        if folded in {name.casefold() for name in inventory}:
            raise PublicationError(f"local payload collision: {path.name}")
        inventory[path.name] = RemoteAsset(
            name=path.name,
            size=path.stat().st_size,
            digest=f"sha256:{canonical_artifacts._sha256(path)}",
        )
    return inventory


def _verify_remote_inventory(
    release: RemoteRelease, expected: dict[str, RemoteAsset], *, allow_missing: bool,
) -> list[str]:
    remote: dict[str, RemoteAsset] = {}
    for asset in release.assets:
        if not asset.name or asset.name in remote or asset.name.casefold() in {
            name.casefold() for name in remote
        }:
            raise PublicationError("remote release contains duplicate or case-colliding assets")
        remote[asset.name] = asset
    extras = sorted(set(remote) - set(expected))
    if extras:
        raise PublicationError(f"remote draft contains stale assets: {', '.join(extras)}")
    for name, actual in remote.items():
        wanted = expected[name]
        if actual.digest is None:
            raise BlockedEnvironment(f"GitHub did not expose a digest for remote asset {name}")
        if actual.size != wanted.size or actual.digest != wanted.digest:
            raise PublicationError(f"remote asset does not match the canonical payload: {name}")
    missing = sorted(set(expected) - set(remote))
    if missing and not allow_missing:
        raise PublicationError(f"remote draft is missing assets: {', '.join(missing)}")
    return missing


def publish_canonical_release(
    client: ReleaseClient,
    *,
    repository: str,
    run_id: str,
    policy_path: Path,
    source_sha: str,
    payload_dir: Path,
    release_notes: Path,
) -> str:
    policy = canonical_artifacts._policy(policy_path)
    if not run_id or not run_id.isdecimal():
        raise PublicationError("GitHub run ID must be a non-empty decimal value")
    canonical_artifacts.verify_payload(policy_path, source_sha, repository, payload_dir)
    canonical_artifacts._regular_file(release_notes, "release notes")
    marker = ownership_marker(repository, policy["tag"], source_sha)
    expected_url = canonical_artifacts._release_url(repository, policy["tag"])
    if not client.immutable_releases_enabled(repository):
        raise BlockedEnvironment("immutable releases are disabled for this repository")
    if client.resolve_tag_commit(repository, policy["tag"]) != source_sha:
        raise PublicationError("release tag no longer resolves to the pinned source SHA")

    release = client.view(repository, policy["tag"])
    if release is not None:
        if release.immutable:
            if release.draft:
                raise PublicationError("immutable release is unexpectedly still a draft")
            if release.tag != policy["tag"]:
                raise PublicationError("immutable release tag does not match the policy")
            expected = _local_inventory(payload_dir)
            _verify_remote_inventory(release, expected, allow_missing=False)
            if release.url != expected_url:
                raise PublicationError("immutable release receipt does not match the manifest")
            return release.url
        if not release.draft:
            raise PublicationError("release is already published; refusing all mutation")
        if release.tag != policy["tag"] or marker not in release.body:
            raise PublicationError("existing draft is not owned by this workflow run")
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="biomem-release-notes-", suffix=".md",
            delete=False,
        ) as temporary:
            owned_notes = Path(temporary.name)
            temporary.write(marker + "\n\n")
            temporary.write(release_notes.read_text(encoding="utf-8"))
        try:
            client.create_draft(
                repository,
                policy["tag"],
                source_sha,
                f"biomem {policy['version']}",
                policy["prerelease"],
                owned_notes,
            )
        finally:
            owned_notes.unlink(missing_ok=True)
        release = None
        for delay in (0.0, 1.0, 2.0, 4.0, 8.0, 15.0):
            if delay:
                time.sleep(delay)
            release = client.view(repository, policy["tag"])
            if release is not None:
                break
        if release is None:
            raise BlockedEnvironment(
                "GitHub did not expose the newly created draft after bounded retries"
            )
        if release.immutable or not release.draft or marker not in release.body:
            raise PublicationError("new draft state or ownership verification failed")

    expected = _local_inventory(payload_dir)
    missing = _verify_remote_inventory(release, expected, allow_missing=True)
    for name in missing:
        client.upload(repository, policy["tag"], payload_dir / name)

    ready = client.view(repository, policy["tag"])
    if (
        ready is None
        or ready.tag != policy["tag"]
        or ready.immutable
        or not ready.draft
        or marker not in ready.body
    ):
        raise PublicationError("draft ownership changed before publication")
    _verify_remote_inventory(ready, expected, allow_missing=False)
    if client.resolve_tag_commit(repository, policy["tag"]) != source_sha:
        raise PublicationError("release tag changed before the publish transition")
    if not client.immutable_releases_enabled(repository):
        raise BlockedEnvironment("immutable releases were disabled before publication")

    client.publish(repository, policy["tag"])
    published = client.view(repository, policy["tag"])
    if published is None or published.draft:
        raise PublicationError("release did not complete its single publish transition")
    if not published.immutable:
        raise PublicationError("published release is mutable; refusing to accept publication")
    if published.url != expected_url:
        raise PublicationError("published release receipt does not match the manifest")
    _verify_remote_inventory(published, expected, allow_missing=False)
    if client.resolve_tag_commit(repository, policy["tag"]) != source_sha:
        raise PublicationError("release tag changed during the publish transition")
    return published.url


def verify_and_dispatch_canonical_release(
    client: ReleaseClient,
    *,
    repository: str,
    policy_path: Path,
    source_sha: str,
    payload_dir: Path,
) -> str:
    """Verify a published immutable release, then notify downstream consumers."""
    policy = canonical_artifacts._policy(policy_path)
    canonical_artifacts.verify_payload(policy_path, source_sha, repository, payload_dir)
    expected_url = canonical_artifacts._release_url(repository, policy["tag"])
    expected = _local_inventory(payload_dir)
    if not client.immutable_releases_enabled(repository):
        raise BlockedEnvironment("immutable releases are disabled for this repository")
    if client.resolve_tag_commit(repository, policy["tag"]) != source_sha:
        raise PublicationError("release tag no longer resolves to the pinned source SHA")
    release = client.view(repository, policy["tag"])
    if release is None or release.draft or not release.immutable:
        raise PublicationError("downstream dispatch requires a published immutable release")
    if release.tag != policy["tag"] or release.url != expected_url:
        raise PublicationError("published release identity does not match the manifest")
    _verify_remote_inventory(release, expected, allow_missing=False)
    if client.resolve_tag_commit(repository, policy["tag"]) != source_sha:
        raise PublicationError("release tag changed before downstream dispatch")
    client.dispatch(repository, policy["tag"], source_sha)
    return release.url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=("publish", "dispatch"), default="publish")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--release-notes", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = GhClient(
            os.environ.get("GH_TOKEN", ""),
            os.environ.get("IMMUTABLE_RELEASES_TOKEN", ""),
        )
        if args.operation == "dispatch":
            receipt = verify_and_dispatch_canonical_release(
                client,
                repository=args.repository,
                policy_path=args.policy,
                source_sha=args.source_sha,
                payload_dir=args.payload_dir,
            )
        else:
            receipt = publish_canonical_release(
                client,
                repository=args.repository,
                run_id=args.run_id,
                policy_path=args.policy,
                source_sha=args.source_sha,
                payload_dir=args.payload_dir,
                release_notes=args.release_notes,
            )
    except BlockedEnvironment as error:
        print(f"BLOCKED_ENVIRONMENT: {error}", file=sys.stderr)
        return 3
    except (PublicationError, canonical_artifacts.ArtifactError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"GitHub publication refused: {error}", file=sys.stderr)
        return 2
    print(receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
