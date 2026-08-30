#!/usr/bin/env python3
"""Generate a checksum-pinned Homebrew formula from canonical release metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import release_policy  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = ROOT / "packaging/homebrew/Formula/biomem.rb.in"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER_RE = re.compile(r"@[A-Z0-9_]+@")
MACOS_TARGETS = {
    "macos-arm64": "ARM64",
    "macos-x86_64": "X86_64",
}


class FormulaError(ValueError):
    """The release metadata cannot safely produce a Homebrew formula."""


def _regular_file(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise FormulaError(f"{description} must be a regular non-symlink file: {path}")


def load_policy(path: Path) -> dict[str, Any]:
    _regular_file(path, "resolved release policy")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FormulaError("resolved release policy must be a JSON object")
    release_policy.validate_resolved_policy(value)
    return value


def parse_checksums(path: Path, expected_names: set[str]) -> dict[str, str]:
    _regular_file(path, "canonical SHA256SUMS.txt")
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        digest, separator, name = line.partition("  ")
        member = PurePosixPath(name)
        if (
            separator != "  "
            or not name
            or not SHA256_RE.fullmatch(digest)
            or member.is_absolute()
            or len(member.parts) != 1
            or member.parts[0] in {"", ".", ".."}
        ):
            raise FormulaError(f"invalid canonical checksum line {line_number}")
        if name in checksums or name.casefold() in {item.casefold() for item in checksums}:
            raise FormulaError(f"duplicate or case-colliding checksum entry: {name}")
        checksums[name] = digest
    if set(checksums) != expected_names:
        missing = sorted(expected_names - set(checksums))
        extra = sorted(set(checksums) - expected_names)
        raise FormulaError(
            f"checksum allowlist mismatch (missing={missing}, extra={extra})"
        )
    return checksums


def render_formula(
    policy: dict[str, Any], repository: str, checksums: dict[str, str], template: str,
) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise FormulaError(f"invalid GitHub repository: {repository!r}")

    artifacts = {
        item["platform"]: item["name"]
        for item in policy["expected_core_artifacts"]
        if item.get("kind") == "standalone_cli" and item.get("platform") in MACOS_TARGETS
    }
    if set(artifacts) != set(MACOS_TARGETS):
        raise FormulaError("release policy must contain exactly the Intel and arm64 macOS archives")

    replacements = {
        "REPOSITORY": repository,
    }
    for target, label in MACOS_TARGETS.items():
        name = artifacts[target]
        replacements[f"{label}_URL"] = (
            f"https://github.com/{repository}/releases/download/{policy['tag']}/{name}"
        )
        replacements[f"{label}_SHA256"] = checksums[name]

    for key in replacements:
        placeholder = f"@{key}@"
        if template.count(placeholder) != 1:
            raise FormulaError(f"formula template must contain {placeholder} exactly once")
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(f"@{key}@", value)
    unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise FormulaError(f"unresolved formula placeholders: {', '.join(unresolved)}")
    return rendered if rendered.endswith("\n") else rendered + "\n"


def generate(
    policy_path: Path,
    checksums_path: Path,
    repository: str,
    output: Path,
    template_path: Path = DEFAULT_TEMPLATE,
) -> None:
    policy = load_policy(policy_path)
    _regular_file(template_path, "Homebrew formula template")
    expected_names = {item["name"] for item in policy["expected_core_artifacts"]}
    checksums = parse_checksums(checksums_path, expected_names)
    rendered = render_formula(
        policy, repository, checksums, template_path.read_text(encoding="utf-8")
    )
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise FormulaError(f"formula output must be a regular file path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        generate(args.policy, args.checksums, args.repository, args.output, args.template)
    except (FormulaError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Homebrew formula error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
