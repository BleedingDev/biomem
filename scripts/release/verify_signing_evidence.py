#!/usr/bin/env python3
"""Validate the artifact-in/evidence-out boundary for optional release signing."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
SUPPORTED_CHANNELS = {
    "windows_signed": "windows",
    "safari_public": "apple",
    "macos_notarized_installer": "apple",
}


class SigningEvidenceError(ValueError):
    """A signing-boundary contract violation."""


def sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SigningEvidenceError(f"artifact must be a regular non-symlink file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SigningEvidenceError(f"JSON evidence must be an object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise SigningEvidenceError("boolean values must be true or false")
    return normalized == "true"


def evaluate_provider_readiness(
    *,
    selected: bool,
    dry_run: bool,
    configured: bool,
    manually_enabled: bool,
    adapter_enabled: bool,
) -> dict[str, str]:
    """Classify readiness without conflating configuration with signing success."""
    if not selected:
        return {"status": "skipped_not_configured", "reason_code": "not_selected"}
    if dry_run:
        return {"status": "blocked_environment", "reason_code": "not_attempted_dry_run"}
    if not configured:
        return {
            "status": "blocked_environment",
            "reason_code": "missing_provider_configuration",
        }
    if not manually_enabled:
        return {
            "status": "blocked_environment",
            "reason_code": "manual_enablement_required",
        }
    if not adapter_enabled:
        return {
            "status": "blocked_environment",
            "reason_code": "provider_adapter_not_enabled",
        }
    return {"status": "ready_for_adapter", "reason_code": "provider_configured"}


def provider_readiness(
    *,
    channel: str,
    selected: bool,
    dry_run: bool,
    required_environment: Sequence[str],
    manually_enabled: bool,
    adapter_enabled: bool,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if channel not in SUPPORTED_CHANNELS:
        raise SigningEvidenceError(f"unsupported signing channel: {channel}")
    values = os.environ if environment is None else environment
    missing = sorted(name for name in required_environment if not values.get(name, "").strip())
    classification = evaluate_provider_readiness(
        selected=selected,
        dry_run=dry_run,
        configured=not missing,
        manually_enabled=manually_enabled,
        adapter_enabled=adapter_enabled,
    )
    return {
        "schema_version": 1,
        "channel": channel,
        **classification,
        "missing_configuration": missing,
        "publication_claimed": False,
    }


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SigningEvidenceError(f"{label} must be a non-empty string")
    return value


def require_https_url(value: Any, label: str) -> str:
    text = require_text(value, label)
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SigningEvidenceError(f"{label} must be an HTTPS URL")
    return text


def require_timestamp(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("status") != "verified":
        raise SigningEvidenceError("timestamp evidence must be verified")
    timestamp = require_text(value.get("value"), "timestamp value")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise SigningEvidenceError("timestamp value must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise SigningEvidenceError("timestamp value must include a timezone")
    return {"status": "verified", "value": timestamp}


def verify_signing_evidence(
    *,
    channel: str,
    provider: str,
    input_artifact: Path,
    expected_input_sha256: str,
    signed_artifact: Path,
    native_evidence: Mapping[str, Any],
    expected_signer: str,
    expected_team_id: str | None = None,
    expected_bundle_ids: Sequence[str] = (),
) -> dict[str, Any]:
    platform = SUPPORTED_CHANNELS.get(channel)
    if platform is None:
        raise SigningEvidenceError(f"unsupported signing channel: {channel}")
    if not SHA256_RE.fullmatch(expected_input_sha256):
        raise SigningEvidenceError("expected input digest must be lowercase SHA-256")

    actual_input_sha256 = sha256(input_artifact)
    if actual_input_sha256 != expected_input_sha256:
        raise SigningEvidenceError("stale input digest: artifact does not match expected SHA-256")
    actual_output_sha256 = sha256(signed_artifact)
    if actual_output_sha256 == actual_input_sha256:
        raise SigningEvidenceError("signed output digest must differ from unsigned input digest")

    if native_evidence.get("schema_version") != 1:
        raise SigningEvidenceError("unsupported native evidence schema")
    if native_evidence.get("platform") != platform:
        raise SigningEvidenceError("native evidence platform does not match channel")
    if native_evidence.get("provider") != provider:
        raise SigningEvidenceError("native evidence provider does not match expected provider")
    if native_evidence.get("signature_valid") is not True:
        raise SigningEvidenceError("native signature verification did not pass")
    signer = require_text(native_evidence.get("signer"), "verified signer")
    if signer != expected_signer:
        raise SigningEvidenceError("verified signer does not exactly match expected signer")

    timestamp = require_timestamp(native_evidence.get("timestamp"))
    receipt = require_https_url(native_evidence.get("provider_receipt"), "provider receipt")
    identity: dict[str, Any] = {"signer": signer}
    notarization: dict[str, Any] | None = None

    if platform == "windows":
        if expected_team_id is not None or expected_bundle_ids:
            raise SigningEvidenceError("Apple identity expectations cannot be used for Windows")
    else:
        if expected_team_id is None or not TEAM_ID_RE.fullmatch(expected_team_id):
            raise SigningEvidenceError("expected Apple team ID must be 10 uppercase letters/digits")
        if not expected_signer.endswith(f" ({expected_team_id})"):
            raise SigningEvidenceError("expected Apple signer must end with the exact team ID")
        if (
            not expected_bundle_ids
            or not all(isinstance(value, str) and value for value in expected_bundle_ids)
            or len(expected_bundle_ids) != len(set(expected_bundle_ids))
        ):
            raise SigningEvidenceError("expected Apple bundle IDs must be a unique non-empty list")
        actual_team_id = require_text(native_evidence.get("team_id"), "verified Apple team ID")
        if actual_team_id != expected_team_id:
            raise SigningEvidenceError("verified Apple team ID does not exactly match expected team")
        actual_bundle_ids = native_evidence.get("bundle_ids")
        if (
            not isinstance(actual_bundle_ids, list)
            or not all(isinstance(value, str) and value for value in actual_bundle_ids)
            or len(actual_bundle_ids) != len(set(actual_bundle_ids))
        ):
            raise SigningEvidenceError("verified Apple bundle IDs must be a unique string list")
        if sorted(actual_bundle_ids) != sorted(expected_bundle_ids):
            raise SigningEvidenceError("verified Apple bundle IDs do not exactly match expectations")
        actual_notarization = native_evidence.get("notarization")
        if not isinstance(actual_notarization, dict):
            raise SigningEvidenceError("Apple evidence requires notarization details")
        if actual_notarization.get("status") != "Accepted":
            raise SigningEvidenceError("Apple notarization was not accepted")
        if actual_notarization.get("staple_valid") is not True:
            raise SigningEvidenceError("Apple notarization ticket was not stapled and validated")
        if actual_notarization.get("gatekeeper_status") != "accepted":
            raise SigningEvidenceError("Apple Gatekeeper assessment was not accepted")
        notarization = {
            "status": "Accepted",
            "id": require_text(actual_notarization.get("id"), "notarization request ID"),
            "staple_valid": True,
            "gatekeeper_status": "accepted",
        }
        identity.update(team_id=actual_team_id, bundle_ids=sorted(actual_bundle_ids))

    result: dict[str, Any] = {
        "schema_version": 1,
        "channel": channel,
        "status": "verified",
        "channel_status": "ready_for_assembly",
        "publication_claimed": False,
        "provider": provider,
        "provider_receipt": receipt,
        "input": {
            "name": input_artifact.name,
            "sha256": actual_input_sha256,
        },
        "output": {
            "name": signed_artifact.name,
            "sha256": actual_output_sha256,
            "size": signed_artifact.stat().st_size,
        },
        "verified_identity": identity,
        "timestamp": timestamp,
    }
    if notarization is not None:
        result["notarization"] = notarization
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    readiness = subparsers.add_parser("readiness")
    readiness.add_argument("--channel", choices=sorted(SUPPORTED_CHANNELS), required=True)
    readiness.add_argument("--selected", default="true")
    readiness.add_argument("--dry-run", default="false")
    readiness.add_argument("--required-env", action="append", default=[])
    readiness.add_argument("--manual-enabled", default="false")
    readiness.add_argument("--adapter-enabled", default="false")
    readiness.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--channel", choices=sorted(SUPPORTED_CHANNELS), required=True)
    verify.add_argument("--provider", required=True)
    verify.add_argument("--input-artifact", type=Path, required=True)
    verify.add_argument("--expected-input-sha256", required=True)
    verify.add_argument("--signed-artifact", type=Path, required=True)
    verify.add_argument("--native-evidence", type=Path, required=True)
    verify.add_argument("--expected-signer", required=True)
    verify.add_argument("--expected-team-id")
    verify.add_argument("--expected-bundle-id", action="append", default=[])
    verify.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "readiness":
            value = provider_readiness(
                channel=args.channel,
                selected=parse_bool(args.selected),
                dry_run=parse_bool(args.dry_run),
                required_environment=args.required_env,
                manually_enabled=parse_bool(args.manual_enabled),
                adapter_enabled=parse_bool(args.adapter_enabled),
            )
        else:
            value = verify_signing_evidence(
                channel=args.channel,
                provider=args.provider,
                input_artifact=args.input_artifact,
                expected_input_sha256=args.expected_input_sha256,
                signed_artifact=args.signed_artifact,
                native_evidence=load_object(args.native_evidence),
                expected_signer=args.expected_signer,
                expected_team_id=args.expected_team_id,
                expected_bundle_ids=args.expected_bundle_id,
            )
        write_json(args.output, value)
    except (OSError, json.JSONDecodeError, SigningEvidenceError) as error:
        print(f"signing evidence error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
