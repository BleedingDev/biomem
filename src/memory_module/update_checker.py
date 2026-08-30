"""Non-mutating checks for newer canonical biomem GitHub releases.

The updater is intentionally notification-only. It never downloads an
artifact, invokes an installer, probes package managers, or changes user data.
Installation remains owned by the package manager (or by the user for direct
GitHub downloads). A check performs at most two bounded metadata requests: one
release-list request and, only when an update exists, one manifest request.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request


logger = logging.getLogger("biomem.update")

GITHUB_REPO = "BleedingDev/biomem"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=20"
CHECK_TIMEOUT = 15
TOTAL_CHECK_TIMEOUT = CHECK_TIMEOUT * 2
UPDATE_CHECK_INTERVAL = 3600
MAX_METADATA_BYTES = 1024 * 1024
MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
CHROME_EXTENSION_NAME = "chrome-biomem.zip"
FIREFOX_ATTACHMENT_REASON = "signed_output_attached_to_canonical_release"

MANIFEST_VERIFIED = "verified_manifest_inventory"
MANIFEST_UNAVAILABLE = "unavailable"
MANIFEST_UNREACHABLE = "unreachable"
MANIFEST_INVALID = "invalid"
PROVENANCE_UNVERIFIED = "unverified"
PROVENANCE_NOT_CHECKED = "not_checked"

_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
)
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class _SemVer:
    major: int
    minor: int
    patch: int

    @property
    def text(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class UpdateInfo:
    """A newer release that can be presented without mutating the machine."""

    version: str
    tag: str
    release_url: str
    manifest_status: str
    provenance_status: str
    upgrade_routes: Tuple[str, ...]


def _parse_semver(value: str) -> Optional[_SemVer]:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value)
    if not match:
        return None
    return _SemVer(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """Compatibility helper returning only the numeric semantic-version core."""
    parsed = _parse_semver(version_str)
    if parsed is None:
        return (0, 0, 0)
    return (parsed.major, parsed.minor, parsed.patch)


def _read_json_response(response) -> Tuple[Any, bytes]:
    try:
        raw = response.read(MAX_METADATA_BYTES + 1)
    except TypeError:
        # Preserve compatibility with minimal response doubles used by callers.
        raw = response.read()
    if not isinstance(raw, bytes) or len(raw) > MAX_METADATA_BYTES:
        raise ValueError("release metadata exceeds the safe size limit")
    return json.loads(raw.decode("utf-8")), raw


def _fetch_releases(*, timeout: float = CHECK_TIMEOUT) -> List[Dict[str, Any]]:
    """Fetch one bounded page of release metadata; failures are quiet and final."""
    from .net import build_ssl_context

    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "biomem-update-checker",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=build_ssl_context(),
        ) as response:
            payload, _ = _read_json_response(response)
    except (OSError, ValueError, UnicodeError) as exc:
        logger.debug("Update check unavailable: %s", exc)
        return []

    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        logger.debug("Update check ignored a malformed GitHub response.")
        return []
    return payload[:20]


def _fetch_release_assets() -> List[Tuple[str, Dict[str, Any]]]:
    """Compatibility view exposing only canonical release metadata assets."""
    metadata_assets: List[Tuple[str, Dict[str, Any]]] = []
    for release in _fetch_releases():
        tag = release.get("tag_name")
        if release.get("draft") or _parse_semver(tag) is None:
            continue
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if asset.get("name") not in {MANIFEST_NAME, CHECKSUMS_NAME}:
                continue
            if asset.get("state") != "uploaded":
                continue
            metadata_assets.append((release.get("name") or tag, asset))
    return metadata_assets


def _select_release(
    current_version: str,
    releases: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    current = _parse_semver(current_version)
    if current is None:
        logger.debug("Update check skipped malformed current version %r.", current_version)
        return None

    selected: Optional[Dict[str, Any]] = None
    selected_version = current

    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            continue
        version = _parse_semver(tag)
        if version is None or version <= current:
            continue
        is_prerelease = bool(release.get("prerelease"))
        if is_prerelease is not (version.major == 0):
            continue
        if selected is None or version > selected_version:
            selected = release
            selected_version = version

    return selected


def _release_url(tag: str) -> str:
    quoted_tag = urllib.parse.quote(tag, safe="")
    return f"https://github.com/{GITHUB_REPO}/releases/tag/{quoted_tag}"


def _manifest_download_url(tag: str) -> str:
    quoted_tag = urllib.parse.quote(tag, safe="")
    return f"https://github.com/{GITHUB_REPO}/releases/download/{quoted_tag}/{MANIFEST_NAME}"


def _valid_release_receipt(value: Any, tag: str) -> bool:
    return value == _release_url(tag)


def _valid_attestation_receipt_shape(value: Any) -> bool:
    """Validate only the claimed receipt URL shape, never its cryptographic proof."""
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(
            rf"/{re.escape(GITHUB_REPO)}/attestations/[A-Za-z0-9._:-]+",
            parsed.path,
        )
    )


def _expected_core_artifact_names(version: str) -> Tuple[str, ...]:
    return (
        f"biomem_memory-{version}-py3-none-any.whl",
        f"biomem_memory-{version}.tar.gz",
        "biomem-linux-x86_64.tar.gz",
        "biomem-linux-aarch64.tar.gz",
        "biomem-windows-x86_64.zip",
        "biomem-macos-x86_64.tar.gz",
        "biomem-macos-arm64.tar.gz",
    )


def _firefox_attachment_name(version: str) -> str:
    return f"firefox-biomem-{version}-amo-signed.xpi"


def _firefox_attachment_is_published(
    manifest: Dict[str, Any],
    tag: str,
    version: str,
) -> bool:
    channels = manifest.get("channels")
    if not isinstance(channels, dict):
        return False
    firefox = channels.get("firefox_amo")
    if not isinstance(firefox, dict):
        return False
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        return False
    selected = policy.get("selected_optional_channels")
    if (
        not isinstance(selected, list)
        or not all(isinstance(channel, str) for channel in selected)
        or len(selected) != len(set(selected))
        or "firefox_amo" not in selected
    ):
        return False
    filename = _firefox_attachment_name(version)
    receipt = (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/{filename}"
    )
    return bool(
        firefox.get("channel") == "firefox_amo"
        and firefox.get("selected") is True
        and firefox.get("class") == "optional"
        and firefox.get("cost") == "zero"
        and firefox.get("selection") == "explicit"
        and firefox.get("status") == "published"
        and firefox.get("reason_code") == FIREFOX_ATTACHMENT_REASON
        and firefox.get("receipt") == receipt
    )


def _expected_canonical_artifact_names(
    version: str,
    *,
    include_firefox_attachment: bool,
) -> Tuple[str, ...]:
    names = (*_expected_core_artifact_names(version), CHROME_EXTENSION_NAME)
    if include_firefox_attachment:
        return (*names, _firefox_attachment_name(version))
    return names


def _release_inventory_is_canonical(
    release: Dict[str, Any],
    manifest: Dict[str, Any],
    manifest_bytes: bytes,
) -> bool:
    assets = release.get("assets")
    if not isinstance(assets, list) or not all(isinstance(asset, dict) for asset in assets):
        return False
    by_name: Dict[str, Dict[str, Any]] = {}
    casefolded_names = set()
    for asset in assets:
        name = asset.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in by_name
            or name.casefold() in casefolded_names
            or asset.get("state") != "uploaded"
            or not isinstance(asset.get("size"), int)
            or asset["size"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(asset.get("digest", "")))
        ):
            return False
        by_name[name] = asset
        casefolded_names.add(name.casefold())

    manifest_artifacts = manifest.get("artifacts")
    if not isinstance(manifest_artifacts, list):
        return False
    if not all(isinstance(artifact, dict) for artifact in manifest_artifacts):
        return False
    expected_names = {
        *(artifact.get("name") for artifact in manifest_artifacts),
        CHECKSUMS_NAME,
        MANIFEST_NAME,
    }
    if set(by_name) != expected_names:
        return False

    for artifact in manifest_artifacts:
        name = artifact["name"]
        remote = by_name[name]
        if remote["size"] != artifact["size"]:
            return False
        remote_digest = remote.get("digest")
        if remote_digest != f"sha256:{artifact['sha256']}":
            return False

    manifest_asset = by_name[MANIFEST_NAME]
    if manifest_asset["size"] != len(manifest_bytes):
        return False
    manifest_digest = manifest_asset.get("digest")
    expected_manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    if manifest_digest != expected_manifest_digest:
        return False

    checksums_bytes = "".join(
        f"{artifact['sha256']}  {artifact['name']}\n" for artifact in manifest_artifacts
    ).encode("utf-8")
    checksums_asset = by_name[CHECKSUMS_NAME]
    if checksums_asset["size"] != len(checksums_bytes):
        return False
    checksums_digest = checksums_asset.get("digest")
    expected_checksums_digest = f"sha256:{hashlib.sha256(checksums_bytes).hexdigest()}"
    if checksums_digest != expected_checksums_digest:
        return False
    return True


def _validate_manifest(payload: Any, release: Dict[str, Any], version: str) -> bool:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    tag = release.get("tag_name")
    release_record = payload.get("release")
    if not isinstance(release_record, dict):
        return False
    if (
        release_record.get("tag") != tag
        or release_record.get("version") != version
        or release_record.get("dry_run") is not False
        or release_record.get("prerelease") is not bool(release.get("prerelease"))
        or not _SOURCE_SHA_RE.fullmatch(str(release_record.get("source_sha", "")))
    ):
        return False
    if payload.get("release_metadata") != [CHECKSUMS_NAME, MANIFEST_NAME]:
        return False

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    names = []
    casefolded_names = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False
        name = artifact.get("name")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or Path(name).name != name
            or name in names
            or name.casefold() in casefolded_names
            or not _DIGEST_RE.fullmatch(str(artifact.get("sha256", "")))
            or not isinstance(artifact.get("size"), int)
            or artifact["size"] <= 0
        ):
            return False
        names.append(name)
        casefolded_names.add(name.casefold())

    firefox_attachment = _firefox_attachment_is_published(payload, str(tag), version)
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return False
    firefox = channels.get("firefox_amo")
    expected_firefox_receipt = (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{urllib.parse.quote(str(tag), safe='')}/{_firefox_attachment_name(version)}"
    )
    if isinstance(firefox, dict) and (
        firefox.get("reason_code") == FIREFOX_ATTACHMENT_REASON
        or firefox.get("receipt") == expected_firefox_receipt
    ) and not firefox_attachment:
        return False
    if tuple(names) != _expected_canonical_artifact_names(
        version,
        include_firefox_attachment=firefox_attachment,
    ):
        return False

    for channel_name in ("github_release", "direct_cli"):
        channel = channels.get(channel_name)
        if (
            not isinstance(channel, dict)
            or channel.get("channel") != channel_name
            or channel.get("status") != "published"
            or not _valid_release_receipt(channel.get("receipt"), str(tag))
        ):
            return False

    provenance = payload.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("provider") != "github_actions_build_provenance"
        or provenance.get("status") != "published"
        or not _valid_attestation_receipt_shape(provenance.get("receipt"))
        or provenance.get("subjects") != names
    ):
        return False
    return True


def _manifest_verification_state(
    release: Dict[str, Any],
    version: str,
    *,
    timeout: float = CHECK_TIMEOUT,
) -> str:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return MANIFEST_INVALID
    expected_url = _manifest_download_url(str(release.get("tag_name", "")))
    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == MANIFEST_NAME
    ]
    if not candidates:
        return MANIFEST_UNAVAILABLE
    if len(candidates) != 1:
        return MANIFEST_INVALID
    asset = candidates[0]
    if asset.get("state") != "uploaded":
        return MANIFEST_INVALID
    if asset.get("browser_download_url") != expected_url:
        return MANIFEST_INVALID

    from .net import build_ssl_context

    request = urllib.request.Request(
        expected_url,
        headers={"Accept": "application/octet-stream", "User-Agent": "biomem-update-checker"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=build_ssl_context(),
        ) as response:
            payload, raw_payload = _read_json_response(response)
    except OSError as exc:
        logger.debug("Release manifest unavailable: %s", exc)
        return MANIFEST_UNREACHABLE
    except (ValueError, UnicodeError) as exc:
        logger.debug("Release manifest is invalid: %s", exc)
        return MANIFEST_INVALID
    if not _validate_manifest(payload, release, version):
        return MANIFEST_INVALID
    if not _release_inventory_is_canonical(release, payload, raw_payload):
        return MANIFEST_INVALID
    return MANIFEST_VERIFIED


def _upgrade_routes(release_url: str) -> Tuple[str, ...]:
    """List choices without detecting or choosing a package manager."""
    return (
        "uv: uv tool upgrade biomem-memory",
        "pipx: pipx upgrade biomem-memory",
        "WinGet: winget upgrade --id BleedingDev.biomem --exact",
        "Homebrew: brew upgrade BleedingDev/tap/biomem",
        f"Direct GitHub (verify release-manifest.json and SHA256SUMS.txt): {release_url}",
    )


def _get_update_info(current_version: str) -> Optional[UpdateInfo]:
    deadline = time.monotonic() + TOTAL_CHECK_TIMEOUT

    def remaining_timeout() -> float:
        return max(0.0, min(CHECK_TIMEOUT, deadline - time.monotonic()))

    release_timeout = remaining_timeout()
    if release_timeout <= 0:
        return None
    selected = _select_release(
        current_version,
        _fetch_releases(timeout=release_timeout),
    )
    if selected is None:
        return None
    parsed = _parse_semver(str(selected.get("tag_name", "")))
    if parsed is None:
        return None
    tag = str(selected["tag_name"])
    release_url = _release_url(tag)
    manifest_timeout = remaining_timeout()
    manifest_status = (
        _manifest_verification_state(selected, parsed.text, timeout=manifest_timeout)
        if manifest_timeout > 0
        else MANIFEST_UNREACHABLE
    )
    return UpdateInfo(
        version=parsed.text,
        tag=tag,
        release_url=release_url,
        manifest_status=manifest_status,
        provenance_status=(
            PROVENANCE_UNVERIFIED
            if manifest_status == MANIFEST_VERIFIED
            else PROVENANCE_NOT_CHECKED
        ),
        upgrade_routes=_upgrade_routes(release_url),
    )


def check_for_update(
    current_version: str,
    tray=None,
    auto_download: bool = True,
    auto_install: bool = True,
    backup_callback: Optional[Any] = None,
) -> Optional[str]:
    """Check for a release and notify; legacy auto flags are intentionally ignored."""
    del auto_download, auto_install, backup_callback
    try:
        info = _get_update_info(current_version)
        if info is None:
            logger.debug(
                "Current version %s is up to date or update metadata is unavailable.",
                current_version,
            )
            return None
        display = f"biomem {info.version}"
        logger.info("New biomem version found: %s (current: %s)", display, current_version)
        _notify_user(
            display,
            info.release_url,
            tray,
            manifest_status=info.manifest_status,
            provenance_status=info.provenance_status,
            upgrade_routes=info.upgrade_routes,
        )
        return display
    except Exception as exc:  # Update checks must never affect startup.
        logger.debug("Update check unavailable: %s", exc)
        return None


def download_and_install_update(
    download_url: str,
    asset_name: str,
    expected_size: int = 0,
    tray=None,
    auto_install: bool = True,
    backup_callback: Optional[Any] = None,
) -> bool:
    """Permanently disabled compatibility entry point; performs no I/O."""
    del download_url, asset_name, expected_size, tray, auto_install, backup_callback
    logger.warning(
        "Automatic update downloads are disabled. Use a listed package-manager command "
        "or the verified GitHub release page."
    )
    return False


def _trigger_silent_installation(
    exe_path: Path | str,
    tray=None,
    backup_callback: Optional[Any] = None,
) -> bool:
    """Permanently disabled compatibility entry point; never executes code."""
    del exe_path, tray, backup_callback
    logger.warning("Silent installer execution is permanently disabled.")
    return False


def _notify_user(
    release_name: str,
    html_url: str,
    tray=None,
    *,
    manifest_status: str = MANIFEST_UNAVAILABLE,
    provenance_status: str = PROVENANCE_NOT_CHECKED,
    upgrade_routes: Tuple[str, ...] = (),
) -> None:
    """Present a release and explicit manual routes without opening or changing anything."""
    routes = upgrade_routes or _upgrade_routes(html_url)
    message = (
        f"A new version is available: {release_name}\n"
        f"Manifest/inventory verification: {manifest_status}\n"
        f"Cryptographic provenance verification: {provenance_status}\n"
        "Choose the route matching how you installed biomem:\n"
        + "\n".join(f"  {route}" for route in routes)
    )
    logger.info(message)
    if tray and getattr(tray, "_icon", None):
        try:
            tray._icon.notify(
                title="biomem Update Available",
                message=(
                    f"{release_name} is available. Manifest: {manifest_status}. "
                    f"Provenance: {provenance_status}. Use your existing package manager "
                    "or the canonical GitHub release page."
                ),
            )
        except Exception:
            logger.debug("Tray update notification was unavailable.")


def _update_check_loop(
    current_version: str,
    tray=None,
    auto_download: bool = True,
    auto_install: bool = True,
    backup_callback: Optional[Any] = None,
) -> None:
    """Check once per interval; failures never trigger an immediate retry."""
    while True:
        try:
            check_for_update(
                current_version,
                tray,
                auto_download,
                auto_install,
                backup_callback,
            )
        except Exception as exc:
            logger.debug("Periodic update check unavailable: %s", exc)
        time.sleep(UPDATE_CHECK_INTERVAL)


def check_for_update_async(
    current_version: str,
    tray=None,
    auto_download: bool = True,
    auto_install: bool = True,
    backup_callback: Optional[Any] = None,
):
    """Start the existing daemon loop; all update activity remains check-only."""
    thread = threading.Thread(
        target=_update_check_loop,
        args=(current_version, tray, auto_download, auto_install, backup_callback),
        daemon=True,
        name="biomem-update-check",
    )
    thread.start()
