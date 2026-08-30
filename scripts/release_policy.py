#!/usr/bin/env python3
"""Canonical release policy, artifact naming, and channel-manifest tooling."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "release" / "release-policy.json"
TAG_RE = re.compile(
    r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$"
)
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIREFOX_ATTACHMENT_REASON = "signed_output_attached_to_canonical_release"


class PolicyError(ValueError):
    """A release-policy contract violation."""


def load_contract(path: Path = POLICY_PATH) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != 1:
        raise PolicyError("unsupported release policy schema")
    return contract


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise PolicyError("boolean values must be 'true' or 'false'")
    return normalized == "true"


def parse_tag(tag: str) -> tuple[str, bool]:
    match = TAG_RE.fullmatch(tag)
    if not match:
        raise PolicyError(f"release tag must be vMAJOR.MINOR.PATCH, got {tag!r}")
    version = match.group("version")
    return version, int(version.split(".", 1)[0]) == 0


def parse_channels(raw: str, contract: dict[str, Any]) -> list[str]:
    requested = [] if raw.strip().lower() in {"", "none"} else [
        value.strip() for value in raw.split(",") if value.strip()
    ]
    if len(requested) != len(set(requested)):
        raise PolicyError("optional channels must not be repeated")
    optional = {
        name for name, data in contract["channels"].items()
        if data["selection"] == "explicit"
    }
    unknown = sorted(set(requested) - optional)
    if unknown:
        raise PolicyError(f"unknown or non-optional channels: {', '.join(unknown)}")
    return [name for name in contract["channels"] if name in requested]


def expected_artifacts(version: str, contract: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = [
        {
            "name": f"biomem_memory-{version}-py3-none-any.whl",
            "kind": "python_wheel",
            "platform": "any",
        },
        {
            "name": f"biomem_memory-{version}.tar.gz",
            "kind": "python_sdist",
            "platform": "source",
        },
    ]
    for target in contract["targets"]:
        artifacts.append({
            "name": f"biomem-{target['target']}.{target['archive']}",
            "kind": "standalone_cli",
            "platform": target["target"],
        })
    return artifacts


def expected_canonical_artifacts(
    version: str, contract: dict[str, Any], *, include_firefox_xpi: bool,
) -> list[dict[str, Any]]:
    artifacts = [
        *expected_artifacts(version, contract),
        {
            "name": "chrome-biomem.zip",
            "kind": "browser_store_input",
            "platform": "chromium",
        },
    ]
    if include_firefox_xpi:
        artifacts.append({
            "name": f"firefox-biomem-{version}-amo-signed.xpi",
            "kind": "signed_browser_extension",
            "platform": "firefox",
        })
    return artifacts


def resolve_policy(tag: str, channels: str, dry_run: str | bool) -> dict[str, Any]:
    contract = load_contract()
    version, prerelease = parse_tag(tag)
    selected = parse_channels(channels, contract)
    is_dry_run = parse_bool(dry_run)
    return {
        "schema_version": contract["schema_version"],
        "tag": tag,
        "version": version,
        "prerelease": prerelease,
        "dry_run": is_dry_run,
        "selected_optional_channels": selected,
        "package_identifiers": contract["package_identifiers"],
        "targets": contract["targets"],
        "expected_core_artifacts": expected_artifacts(version, contract),
    }


def validate_resolved_policy(policy: dict[str, Any]) -> None:
    selected = policy.get("selected_optional_channels")
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise PolicyError("resolved policy optional channels must be a string list")
    expected = resolve_policy(
        policy.get("tag", ""), ",".join(selected), policy.get("dry_run", "")
    )
    if policy != expected:
        raise PolicyError("resolved policy does not match the canonical release contract")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_github_output(policy: dict[str, Any], output_path: Path) -> None:
    lines = {
        "version": policy["version"],
        "prerelease": str(policy["prerelease"]).lower(),
        "dry-run": str(policy["dry_run"]).lower(),
        "selected-channels": json.dumps(policy["selected_optional_channels"], separators=(",", ":")),
        "targets": json.dumps(policy["targets"], separators=(",", ":")),
    }
    with output_path.open("a", encoding="utf-8") as output:
        for key, value in lines.items():
            output.write(f"{key}={value}\n")


def make_evidence(
    policy: dict[str, Any], channel: str, status: str, reason_code: str,
    receipt: str | None = None,
) -> dict[str, Any]:
    validate_resolved_policy(policy)
    contract = load_contract()
    channels = contract["channels"]
    if channel not in channels:
        raise PolicyError(f"unknown channel: {channel}")
    if status not in contract["channel_statuses"]:
        raise PolicyError(f"unknown channel status: {status}")
    selected = channels[channel]["selection"] == "always" or channel in policy["selected_optional_channels"]
    if status == "published" and (policy["dry_run"] or not selected):
        raise PolicyError("published requires a selected, non-dry-run channel")
    if status == "published" and not receipt:
        raise PolicyError("published requires a verified remote receipt")
    if status != "published" and receipt:
        raise PolicyError("only published channel evidence may include a receipt")
    if status == "skipped_not_configured" and selected:
        raise PolicyError("selected channels cannot be skipped_not_configured")
    if status in {"blocked_environment", "failed"} and not selected:
        raise PolicyError(f"{status} requires a selected channel")
    return {
        "channel": channel,
        "selected": selected,
        "status": status,
        "reason_code": reason_code,
        "receipt": receipt,
    }


def read_evidence(directory: Path) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return evidence
    for path in sorted(directory.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        channel = item.get("channel")
        if not isinstance(channel, str) or channel in evidence:
            raise PolicyError(f"invalid or duplicate channel evidence in {path}")
        evidence[channel] = item
    return evidence


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifacts(policy: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    validate_resolved_policy(policy)
    return _verify_exact_artifacts(policy["expected_core_artifacts"], directory, "core")


def verify_canonical_artifacts(
    policy: dict[str, Any], directory: Path, *, include_firefox_xpi: bool,
) -> list[dict[str, Any]]:
    validate_resolved_policy(policy)
    expected = expected_canonical_artifacts(
        policy["version"], load_contract(), include_firefox_xpi=include_firefox_xpi,
    )
    return _verify_exact_artifacts(expected, directory, "canonical")


def _verify_exact_artifacts(
    expected: list[dict[str, Any]], directory: Path, description: str,
) -> list[dict[str, Any]]:
    if directory.is_symlink() or not directory.is_dir():
        raise PolicyError(f"{description} artifact directory is not a regular directory")
    expected_names = {item["name"] for item in expected}
    present_names: set[str] = set()
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise PolicyError(
                f"{description} artifact set contains a non-regular file: {path.name}"
            )
        if path.name.casefold() in {name.casefold() for name in present_names}:
            raise PolicyError(f"{description} artifact set contains a case collision")
        present_names.add(path.name)
    extras = sorted(present_names - expected_names)
    if extras:
        raise PolicyError(
            f"unexpected {description} release artifacts: {', '.join(extras)}"
        )
    resolved = []
    for item in expected:
        path = directory / item["name"]
        if not path.is_file():
            raise PolicyError(f"missing {description} release artifact: {item['name']}")
        if path.stat().st_size <= 0:
            raise PolicyError(f"empty {description} release artifact: {item['name']}")
        resolved.append({**item, "sha256": sha256(path), "size": path.stat().st_size})
    return resolved


def validate_source_sha(source_sha: str) -> None:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise PolicyError("source SHA must be a full 40-character lowercase Git commit SHA")


def load_provenance_evidence(
    path: Path | None, source_sha: str, subjects: list[str],
) -> dict[str, Any]:
    if path is None or not path.is_file():
        raise PolicyError("normal releases require successful provenance evidence")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("status") != "published":
        raise PolicyError("provenance evidence must record published")
    validate_source_sha(value.get("source_sha", ""))
    if value["source_sha"] != source_sha:
        raise PolicyError("provenance source SHA does not match policy")
    if not value.get("receipt"):
        raise PolicyError("provenance evidence requires a verified remote receipt")
    if value.get("provider") != "github_actions_build_provenance":
        raise PolicyError("unexpected provenance provider")
    return {
        "provider": value.get("provider", "github_actions_build_provenance"),
        "subjects": subjects,
        "status": "published",
        "receipt": value["receipt"],
    }


def build_manifest(
    policy: dict[str, Any], evidence_dir: Path, artifacts_dir: Path, source_sha: str,
    provenance_evidence: Path | None = None,
) -> dict[str, Any]:
    if policy["dry_run"]:
        raise PolicyError("dry runs produce release-preflight.json, not release-manifest.json")
    return build_release_record(
        policy, evidence_dir, artifacts_dir, source_sha,
        preflight=False, provenance_evidence=provenance_evidence,
    )


def build_preflight(
    policy: dict[str, Any], evidence_dir: Path, artifacts_dir: Path, source_sha: str
) -> dict[str, Any]:
    if not policy["dry_run"]:
        raise PolicyError("release preflight requires dry_run=true")
    value = build_release_record(
        policy, evidence_dir, artifacts_dir, source_sha,
        preflight=True, provenance_evidence=None,
    )
    value["execution_mode"] = "dry_run"
    return value


def build_release_record(
    policy: dict[str, Any], evidence_dir: Path, artifacts_dir: Path, source_sha: str,
    *, preflight: bool, provenance_evidence: Path | None,
) -> dict[str, Any]:
    validate_source_sha(source_sha)
    contract = load_contract()
    evidence = read_evidence(evidence_dir)
    unknown = sorted(set(evidence) - set(contract["channels"]))
    if unknown:
        raise PolicyError(f"evidence names unknown channels: {', '.join(unknown)}")

    channels: dict[str, Any] = {}
    for name, definition in contract["channels"].items():
        selected = definition["selection"] == "always" or name in policy["selected_optional_channels"]
        item = None if preflight else evidence.get(name)
        if item is None:
            if selected:
                item = make_evidence(
                    policy,
                    name,
                    "blocked_environment" if preflight else "failed",
                    "not_attempted_dry_run" if preflight else "missing_internal_evidence",
                )
            else:
                item = make_evidence(policy, name, "skipped_not_configured", "not_selected")
        else:
            item = make_evidence(
                policy, name, item["status"], item.get("reason_code", ""), item.get("receipt")
            )
        channels[name] = {**definition, **item}

    firefox = channels["firefox_amo"]
    include_firefox_xpi = (
        firefox["status"] == "published"
        and firefox["reason_code"] == FIREFOX_ATTACHMENT_REASON
    )
    artifacts = verify_canonical_artifacts(
        policy, artifacts_dir, include_firefox_xpi=include_firefox_xpi,
    )
    subjects = [item["name"] for item in artifacts]

    return {
        "schema_version": contract["schema_version"],
        "release": {
            "tag": policy["tag"],
            "version": policy["version"],
            "source_sha": source_sha,
            "prerelease": policy["prerelease"],
            "dry_run": policy["dry_run"],
        },
        "package_identifiers": policy["package_identifiers"],
        "policy": {
            "selected_optional_channels": policy["selected_optional_channels"],
        },
        "artifacts": artifacts,
        "release_metadata": ["SHA256SUMS.txt", "release-manifest.json"],
        "provenance": ({
            "provider": "github_actions_build_provenance",
            "subjects": subjects,
            "status": "blocked_environment",
            "reason_code": "not_attempted_dry_run",
            "receipt": None,
        } if preflight else load_provenance_evidence(provenance_evidence, source_sha, subjects)),
        "channels": channels,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    contract = load_contract()
    if manifest.get("schema_version") != contract["schema_version"]:
        raise PolicyError("release manifest schema does not match the release contract")
    release = manifest.get("release", {})
    version, prerelease = parse_tag(release.get("tag", ""))
    if release.get("version") != version or release.get("prerelease") is not prerelease:
        raise PolicyError("release manifest tag/version policy is inconsistent")
    if release.get("dry_run") is not False:
        raise PolicyError("release-manifest.json cannot describe a dry run")
    validate_source_sha(release.get("source_sha", ""))
    if manifest.get("package_identifiers") != contract["package_identifiers"]:
        raise PolicyError("manifest package identifiers do not match the release contract")

    selected = manifest.get("policy", {}).get("selected_optional_channels")
    if not isinstance(selected, list):
        raise PolicyError("manifest selected_optional_channels must be a list")
    canonical_selected = parse_channels(",".join(selected), contract)
    if selected != canonical_selected:
        raise PolicyError("manifest optional channel selection is not canonical")

    channels = manifest.get("channels")
    if not isinstance(channels, dict) or set(channels) != set(contract["channels"]):
        raise PolicyError("manifest must contain every expected channel exactly once")
    for name, definition in contract["channels"].items():
        item = channels[name]
        expected_selected = definition["selection"] == "always" or name in selected
        if item.get("selected") is not expected_selected:
            raise PolicyError(f"manifest selected flag is invalid for {name}")
        status = item.get("status")
        if status not in contract["channel_statuses"]:
            raise PolicyError(f"manifest status is invalid for {name}: {status}")
        if expected_selected and status == "skipped_not_configured":
            raise PolicyError(f"selected channel {name} cannot be skipped_not_configured")
        if not expected_selected and status != "skipped_not_configured":
            raise PolicyError(f"unselected channel {name} must be skipped_not_configured")
        if definition["class"] == "core" and status != "published":
            raise PolicyError(f"core channel {name} must be published")
        if item.get("channel") != name:
            raise PolicyError(f"manifest channel identity is invalid for {name}")
        if item.get("class") != definition["class"] or item.get("cost") != definition["cost"]:
            raise PolicyError(f"manifest channel policy metadata is invalid for {name}")
        if not item.get("reason_code"):
            raise PolicyError(f"manifest channel {name} requires a reason_code")
        if status == "published" and not item.get("receipt"):
            raise PolicyError(f"published channel {name} requires a verified remote receipt")
        if status != "published" and item.get("receipt"):
            raise PolicyError(f"non-published channel {name} cannot have a receipt")

    firefox = channels["firefox_amo"]
    includes_firefox_xpi = firefox.get("reason_code") == FIREFOX_ATTACHMENT_REASON
    if includes_firefox_xpi:
        if firefox.get("status") != "published":
            raise PolicyError("attached Firefox XPI requires published channel evidence")
        filename = f"firefox-biomem-{version}-amo-signed.xpi"
        expected_suffix = f"/releases/download/{release['tag']}/{filename}"
        receipt = firefox.get("receipt")
        if (
            not isinstance(receipt, str)
            or not receipt.startswith("https://github.com/")
            or not receipt.endswith(expected_suffix)
        ):
            raise PolicyError("attached Firefox XPI receipt is not the canonical release asset URL")
    expected = expected_canonical_artifacts(
        version, contract, include_firefox_xpi=includes_firefox_xpi,
    )
    expected_names = [item["name"] for item in expected]
    artifact_names = [item.get("name") for item in manifest.get("artifacts", [])]
    if artifact_names != expected_names:
        raise PolicyError("manifest artifact names do not match the canonical set")
    for actual, wanted in zip(manifest["artifacts"], expected):
        if actual.get("kind") != wanted["kind"] or actual.get("platform") != wanted["platform"]:
            raise PolicyError(f"manifest artifact metadata is invalid for {wanted['name']}")
        if not SHA256_RE.fullmatch(str(actual.get("sha256", ""))):
            raise PolicyError(f"manifest artifact digest is invalid for {wanted['name']}")
        if not isinstance(actual.get("size"), int) or actual["size"] <= 0:
            raise PolicyError(f"manifest artifact size is invalid for {wanted['name']}")
    if manifest.get("release_metadata") != ["SHA256SUMS.txt", "release-manifest.json"]:
        raise PolicyError("manifest release metadata set is invalid")
    provenance = manifest.get("provenance", {})
    if provenance.get("status") != "published" or not provenance.get("receipt"):
        raise PolicyError("release manifest requires successful provenance evidence")
    if provenance.get("provider") != "github_actions_build_provenance":
        raise PolicyError("release manifest provenance provider is invalid")
    if provenance.get("subjects") != expected_names:
        raise PolicyError("release manifest provenance subjects are invalid")


def enforce_selected(manifest: dict[str, Any]) -> None:
    validate_manifest(manifest)
    selected = set(manifest["policy"]["selected_optional_channels"])
    blocking = {
        name: channel["status"]
        for name, channel in manifest["channels"].items()
        if name in selected and channel["status"] != "published"
    }
    if blocking:
        raise PolicyError(f"selected channels are not publishable: {blocking}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    policy = subparsers.add_parser("policy")
    policy.add_argument("--tag", required=True)
    policy.add_argument("--channels", default="none")
    policy.add_argument("--dry-run", default="false")
    policy.add_argument("--output", type=Path, required=True)
    policy.add_argument("--github-output", type=Path)

    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--policy", type=Path, required=True)
    evidence.add_argument("--channel", required=True)
    evidence.add_argument("--status", required=True)
    evidence.add_argument("--reason", required=True)
    evidence.add_argument("--receipt")
    evidence.add_argument("--output", type=Path, required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--policy", type=Path, required=True)
    manifest.add_argument("--evidence-dir", type=Path, required=True)
    manifest.add_argument("--artifacts-dir", type=Path, required=True)
    manifest.add_argument("--source-sha", required=True)
    manifest.add_argument("--provenance-evidence", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--policy", type=Path, required=True)
    preflight.add_argument("--evidence-dir", type=Path, required=True)
    preflight.add_argument("--artifacts-dir", type=Path, required=True)
    preflight.add_argument("--source-sha", required=True)
    preflight.add_argument("--output", type=Path, required=True)

    inventory = subparsers.add_parser("verify-artifacts")
    inventory.add_argument("--policy", type=Path, required=True)
    inventory.add_argument("--artifacts-dir", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)

    check = subparsers.add_parser("check-manifest")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--enforce-selected", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "policy":
            value = resolve_policy(args.tag, args.channels, args.dry_run)
            write_json(args.output, value)
            if args.github_output:
                write_github_output(value, args.github_output)
        elif args.command == "evidence":
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            write_json(
                args.output,
                make_evidence(
                    policy, args.channel, args.status, args.reason, args.receipt,
                ),
            )
        elif args.command == "verify-artifacts":
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            write_json(args.output, {"artifacts": verify_artifacts(policy, args.artifacts_dir)})
        elif args.command in {"manifest", "preflight"}:
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            if args.command == "manifest":
                value = build_manifest(
                    policy, args.evidence_dir, args.artifacts_dir, args.source_sha,
                    args.provenance_evidence,
                )
            else:
                value = build_preflight(
                    policy, args.evidence_dir, args.artifacts_dir, args.source_sha,
                )
            write_json(
                args.output,
                value,
            )
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            validate_manifest(manifest)
            if args.enforce_selected:
                enforce_selected(manifest)
    except (OSError, json.JSONDecodeError, KeyError, PolicyError) as error:
        print(f"release policy error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
