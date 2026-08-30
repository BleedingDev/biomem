"""Security regressions for the check-only release notifier."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

from memory_module import update_checker


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit=-1):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def _release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    assets=None,
    html_url: str = "https://attacker.invalid/release",
):
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "draft": draft,
        "prerelease": prerelease,
        "html_url": html_url,
        "assets": list(assets or []),
    }


def _manifest(tag: str, version: str):
    return {
        "schema_version": 1,
        "release": {
            "tag": tag,
            "version": version,
            "source_sha": "a" * 40,
            "prerelease": int(version.split(".", 1)[0]) == 0,
            "dry_run": False,
        },
        "release_metadata": ["SHA256SUMS.txt", "release-manifest.json"],
        "artifacts": [
            {"name": name, "sha256": "b" * 64, "size": 42}
            for name in (
                f"biomem_memory-{version}-py3-none-any.whl",
                f"biomem_memory-{version}.tar.gz",
                "biomem-linux-x86_64.tar.gz",
                "biomem-linux-aarch64.tar.gz",
                "biomem-windows-x86_64.zip",
                "biomem-macos-x86_64.tar.gz",
                "biomem-macos-arm64.tar.gz",
                "chrome-biomem.zip",
            )
        ],
        "channels": {
            "github_release": {
                "channel": "github_release",
                "status": "published",
                "receipt": f"https://github.com/BleedingDev/biomem/releases/tag/{tag}",
            },
            "direct_cli": {
                "channel": "direct_cli",
                "status": "published",
                "receipt": f"https://github.com/BleedingDev/biomem/releases/tag/{tag}",
            },
            "winget": {
                "channel": "winget",
                "status": "failed",
                "receipt": None,
            },
            "firefox_amo": {
                "channel": "firefox_amo",
                "status": "skipped_not_configured",
                "reason_code": "not_selected",
                "receipt": None,
            },
        },
        "provenance": {
            "provider": "github_actions_build_provenance",
            "status": "published",
            "receipt": f"https://github.com/BleedingDev/biomem/attestations/{tag}",
            "subjects": [
                f"biomem_memory-{version}-py3-none-any.whl",
                f"biomem_memory-{version}.tar.gz",
                "biomem-linux-x86_64.tar.gz",
                "biomem-linux-aarch64.tar.gz",
                "biomem-windows-x86_64.zip",
                "biomem-macos-x86_64.tar.gz",
                "biomem-macos-arm64.tar.gz",
                "chrome-biomem.zip",
            ],
        },
    }


def _attach_published_firefox(manifest):
    version = manifest["release"]["version"]
    tag = manifest["release"]["tag"]
    filename = f"firefox-biomem-{version}-amo-signed.xpi"
    manifest["artifacts"].append({"name": filename, "sha256": "c" * 64, "size": 43})
    manifest["provenance"]["subjects"].append(filename)
    manifest["channels"]["firefox_amo"] = {
        "channel": "firefox_amo",
        "selected": True,
        "class": "optional",
        "cost": "zero",
        "selection": "explicit",
        "status": "published",
        "reason_code": update_checker.FIREFOX_ATTACHMENT_REASON,
        "receipt": (
            "https://github.com/BleedingDev/biomem/releases/download/"
            f"{tag}/{filename}"
        ),
    }
    manifest["policy"] = {"selected_optional_channels": ["firefox_amo"]}
    return filename


def _canonical_release_assets(manifest):
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    checksums_bytes = "".join(
        f"{artifact['sha256']}  {artifact['name']}\n"
        for artifact in manifest["artifacts"]
    ).encode("utf-8")
    assets = [
        {
            "name": artifact["name"],
            "state": "uploaded",
            "size": artifact["size"],
            "digest": f"sha256:{artifact['sha256']}",
        }
        for artifact in manifest["artifacts"]
    ]
    assets.extend(
        [
            {
                "name": "SHA256SUMS.txt",
                "state": "uploaded",
                "size": len(checksums_bytes),
                "digest": f"sha256:{hashlib.sha256(checksums_bytes).hexdigest()}",
            },
            {
                "name": "release-manifest.json",
                "state": "uploaded",
                "size": len(manifest_bytes),
                "digest": f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
                "browser_download_url": (
                    "https://github.com/BleedingDev/biomem/releases/download/"
                    f"{manifest['release']['tag']}/release-manifest.json"
                ),
            },
        ]
    )
    return assets


class UpdateCheckerTests(unittest.TestCase):
    def test_stable_client_selects_latest_stable_semver_only(self):
        releases = [
            _release("v1.3.0-rc.1", prerelease=True),
            _release("v1.2.0"),
            _release("v9.0.0", draft=True),
            _release("nightly"),
            _release("v1.4.0", prerelease=True),  # GitHub/tag disagreement
        ]

        selected = update_checker._select_release("1.0.0", releases)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["tag_name"], "v1.2.0")

    def test_zero_major_alpha_line_accepts_github_prereleases(self):
        selected = update_checker._select_release(
            "0.0.2",
            [_release("v0.0.3", prerelease=True)],
        )

        self.assertEqual(selected["tag_name"], "v0.0.3")

    def test_noncanonical_tags_and_prerelease_flags_are_rejected(self):
        invalid = [
            _release("v1.2.3-rc.1", prerelease=True),
            _release("v1.2.3+build.1"),
            _release(" v1.2.3 "),
            _release("v1.2.3", prerelease=True),
            _release("v0.2.0", prerelease=False),
        ]

        self.assertIsNone(update_checker._select_release("0.0.2", invalid))

    def test_malformed_and_offline_github_responses_are_quiet(self):
        payloads = [
            {"message": "rate limited"},
            b"not json",
            ["not-a-release"],
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with patch("urllib.request.urlopen", return_value=_Response(payload)):
                    self.assertEqual(update_checker._fetch_releases(), [])

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            self.assertEqual(update_checker._fetch_releases(), [])

    def test_update_info_uses_derived_release_page_and_verified_manifest(self):
        tag = "v1.2.3"
        manifest = _manifest(tag, "1.2.3")
        release = _release(
            tag,
            assets=_canonical_release_assets(manifest),
        )

        with patch.object(update_checker, "_fetch_releases", return_value=[release]):
            with patch("urllib.request.urlopen", return_value=_Response(manifest)):
                info = update_checker._get_update_info("1.0.0")

        self.assertEqual(info.version, "1.2.3")
        self.assertEqual(info.manifest_status, "verified_manifest_inventory")
        self.assertEqual(info.provenance_status, "unverified")
        self.assertEqual(
            info.release_url,
            "https://github.com/BleedingDev/biomem/releases/tag/v1.2.3",
        )
        rendered = "\n".join(info.upgrade_routes)
        self.assertIn("uv tool upgrade biomem-memory", rendered)
        self.assertIn("pipx upgrade biomem-memory", rendered)
        self.assertIn("winget upgrade --id BleedingDev.biomem --exact", rendered)
        self.assertIn("brew upgrade BleedingDev/tap/biomem", rendered)
        self.assertIn(info.release_url, rendered)
        self.assertNotIn("dev-build.exe", rendered)

    def test_manifest_is_not_verified_when_missing_malformed_or_unreachable(self):
        release = _release("v1.2.3")
        self.assertEqual(
            update_checker._manifest_verification_state(release, "1.2.3"),
            "unavailable",
        )

        asset = {
            "name": "release-manifest.json",
            "state": "uploaded",
            "browser_download_url": (
                "https://github.com/BleedingDev/biomem/releases/download/"
                "v1.2.3/release-manifest.json"
            ),
        }
        release = _release("v1.2.3", assets=[asset])
        with patch("urllib.request.urlopen", return_value=_Response({"schema_version": 999})):
            self.assertEqual(
                update_checker._manifest_verification_state(release, "1.2.3"),
                "invalid",
            )
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(
                update_checker._manifest_verification_state(release, "1.2.3"),
                "unreachable",
            )

        malformed_payloads = [
            b"not json",
            b"\xff",
            b"x" * (update_checker.MAX_METADATA_BYTES + 1),
        ]
        for payload in malformed_payloads:
            with self.subTest(payload_size=len(payload)):
                with patch("urllib.request.urlopen", return_value=_Response(payload)):
                    self.assertEqual(
                        update_checker._manifest_verification_state(release, "1.2.3"),
                        "invalid",
                    )

    def test_development_artifact_cannot_validate_as_canonical(self):
        release = _release("v1.2.3")
        manifest = _manifest("v1.2.3", "1.2.3")
        manifest["artifacts"][0]["name"] = "dev-build.exe"

        self.assertFalse(update_checker._validate_manifest(manifest, release, "1.2.3"))

    def test_release_inventory_must_exactly_match_verified_manifest(self):
        manifest = _manifest("v1.2.3", "1.2.3")
        canonical_assets = _canonical_release_assets(manifest)

        mutations = {}
        mutations["missing core asset"] = canonical_assets[1:]
        mutations["extra development asset"] = [
            *canonical_assets,
            {"name": "dev-build.exe", "state": "uploaded", "size": 12},
        ]
        wrong_size = deepcopy(canonical_assets)
        wrong_size[0]["size"] += 1
        mutations["size mismatch"] = wrong_size
        wrong_digest = deepcopy(canonical_assets)
        wrong_digest[0]["digest"] = f"sha256:{'d' * 64}"
        mutations["digest mismatch"] = wrong_digest
        checksum_index = next(
            index
            for index, asset in enumerate(canonical_assets)
            if asset["name"] == "SHA256SUMS.txt"
        )
        wrong_checksum_size = deepcopy(canonical_assets)
        wrong_checksum_size[checksum_index]["size"] += 1
        mutations["checksum size mismatch"] = wrong_checksum_size
        wrong_checksum_digest = deepcopy(canonical_assets)
        wrong_checksum_digest[checksum_index]["digest"] = f"sha256:{'e' * 64}"
        mutations["checksum digest mismatch"] = wrong_checksum_digest
        missing_core_digest = deepcopy(canonical_assets)
        missing_core_digest[0].pop("digest")
        mutations["missing core digest"] = missing_core_digest
        missing_checksum_digest = deepcopy(canonical_assets)
        missing_checksum_digest[checksum_index].pop("digest")
        mutations["missing checksum digest"] = missing_checksum_digest
        missing_manifest_digest = deepcopy(canonical_assets)
        next(
            asset for asset in missing_manifest_digest if asset["name"] == "release-manifest.json"
        ).pop("digest")
        mutations["missing manifest digest"] = missing_manifest_digest
        missing_core_state = deepcopy(canonical_assets)
        missing_core_state[0].pop("state")
        mutations["missing core upload state"] = missing_core_state
        missing_manifest_state = deepcopy(canonical_assets)
        next(
            asset for asset in missing_manifest_state if asset["name"] == "release-manifest.json"
        ).pop("state")
        mutations["missing manifest upload state"] = missing_manifest_state

        for name, assets in mutations.items():
            with self.subTest(name=name):
                release = _release("v1.2.3", assets=assets)
                with patch("urllib.request.urlopen", return_value=_Response(manifest)):
                    self.assertEqual(
                        update_checker._manifest_verification_state(release, "1.2.3"),
                        "invalid",
                    )

    def test_chrome_extension_is_a_required_canonical_release_asset(self):
        manifest = _manifest("v1.2.3", "1.2.3")
        chrome_index = next(
            index
            for index, artifact in enumerate(manifest["artifacts"])
            if artifact["name"] == "chrome-biomem.zip"
        )
        manifest["artifacts"].pop(chrome_index)
        manifest["provenance"]["subjects"].remove("chrome-biomem.zip")
        release = _release("v1.2.3", assets=_canonical_release_assets(manifest))

        self.assertFalse(update_checker._validate_manifest(manifest, release, "1.2.3"))

        canonical = _manifest("v1.2.3", "1.2.3")
        assets = [
            asset
            for asset in _canonical_release_assets(canonical)
            if asset["name"] != "chrome-biomem.zip"
        ]
        release = _release("v1.2.3", assets=assets)
        with patch("urllib.request.urlopen", return_value=_Response(canonical)):
            self.assertEqual(
                update_checker._manifest_verification_state(release, "1.2.3"),
                "invalid",
            )

    def test_signed_firefox_attachment_requires_exact_symmetric_evidence(self):
        canonical = _manifest("v1.2.3", "1.2.3")
        filename = _attach_published_firefox(canonical)
        release = _release("v1.2.3", assets=_canonical_release_assets(canonical))
        self.assertTrue(update_checker._validate_manifest(canonical, release, "1.2.3"))

        mutations = {}

        without_evidence = _manifest("v1.2.3", "1.2.3")
        without_evidence["artifacts"].append(
            {"name": filename, "sha256": "c" * 64, "size": 43}
        )
        without_evidence["provenance"]["subjects"].append(filename)
        mutations["attachment without evidence"] = without_evidence

        without_attachment = _manifest("v1.2.3", "1.2.3")
        _attach_published_firefox(without_attachment)
        without_attachment["artifacts"].pop()
        without_attachment["provenance"]["subjects"].pop()
        mutations["evidence without attachment"] = without_attachment

        wrong_receipt = deepcopy(canonical)
        wrong_receipt["channels"]["firefox_amo"]["receipt"] = (
            "https://github.com/Other/biomem/releases/download/v1.2.3/" + filename
        )
        mutations["wrong repository receipt"] = wrong_receipt

        wrong_tag = deepcopy(canonical)
        wrong_tag["channels"]["firefox_amo"]["receipt"] = (
            "https://github.com/BleedingDev/biomem/releases/download/v9.9.9/" + filename
        )
        mutations["wrong tag receipt"] = wrong_tag

        evidence_field_mutations = (
            ("selected", False),
            ("class", "paid"),
            ("cost", "expensive"),
            ("selection", "always"),
        )
        for field, value in evidence_field_mutations:
            tampered = deepcopy(canonical)
            tampered["channels"]["firefox_amo"][field] = value
            mutations[f"wrong evidence {field}"] = tampered

        missing_policy_selection = deepcopy(canonical)
        missing_policy_selection["policy"]["selected_optional_channels"] = []
        mutations["missing policy selection"] = missing_policy_selection

        for name, manifest in mutations.items():
            with self.subTest(name=name):
                release = _release("v1.2.3", assets=_canonical_release_assets(manifest))
                self.assertFalse(
                    update_checker._validate_manifest(manifest, release, "1.2.3")
                )

    def test_noncanonical_browser_assets_and_case_collisions_are_rejected(self):
        forbidden_names = (
            "firefox-biomem-unsigned.xpi",
            "safari-biomem-development-adhoc.zip",
            "chrome-biomem.crx",
        )
        for forbidden_name in forbidden_names:
            with self.subTest(forbidden_name=forbidden_name):
                manifest = _manifest("v1.2.3", "1.2.3")
                manifest["artifacts"].append(
                    {"name": forbidden_name, "sha256": "d" * 64, "size": 44}
                )
                manifest["provenance"]["subjects"].append(forbidden_name)
                release = _release("v1.2.3", assets=_canonical_release_assets(manifest))
                self.assertFalse(
                    update_checker._validate_manifest(manifest, release, "1.2.3")
                )

        manifest = _manifest("v1.2.3", "1.2.3")
        release_assets = _canonical_release_assets(manifest)
        release_assets.append(
            {
                "name": "Chrome-Biomem.zip",
                "state": "uploaded",
                "size": 42,
                "digest": f"sha256:{'d' * 64}",
            }
        )
        release = _release("v1.2.3", assets=release_assets)
        with patch("urllib.request.urlopen", return_value=_Response(manifest)):
            self.assertEqual(
                update_checker._manifest_verification_state(release, "1.2.3"),
                "invalid",
            )

    def test_checksum_and_provenance_subjects_follow_optional_inventory_exactly(self):
        manifest = _manifest("v1.2.3", "1.2.3")
        filename = _attach_published_firefox(manifest)

        subject_drift = deepcopy(manifest)
        subject_drift["provenance"]["subjects"].remove(filename)
        release = _release("v1.2.3", assets=_canonical_release_assets(subject_drift))
        self.assertFalse(
            update_checker._validate_manifest(subject_drift, release, "1.2.3")
        )

        assets = _canonical_release_assets(manifest)
        checksum = next(asset for asset in assets if asset["name"] == "SHA256SUMS.txt")
        checksum["digest"] = f"sha256:{'e' * 64}"
        release = _release("v1.2.3", assets=assets)
        with patch("urllib.request.urlopen", return_value=_Response(manifest)):
            self.assertEqual(
                update_checker._manifest_verification_state(release, "1.2.3"),
                "invalid",
            )

    def test_receipts_are_bound_to_release_and_attestation_routes(self):
        release = _release("v1.2.3")
        canonical = _manifest("v1.2.3", "1.2.3")
        self.assertTrue(update_checker._validate_manifest(canonical, release, "1.2.3"))

        invalid_receipts = [
            ("github_release", "https://github.com/BleedingDev/biomem/releases/tag/v9.9.9"),
            ("provenance", "https://github.com/BleedingDev/biomem/issues/1"),
            ("provenance", "https://github.com/BleedingDev/biomem/releases/tag/v1.2.3"),
            ("provenance", "https://github.com/Other/biomem/attestations/123"),
        ]
        for target, receipt in invalid_receipts:
            with self.subTest(target=target, receipt=receipt):
                manifest = deepcopy(canonical)
                if target == "provenance":
                    manifest["provenance"]["receipt"] = receipt
                else:
                    manifest["channels"][target]["receipt"] = receipt
                self.assertFalse(update_checker._validate_manifest(manifest, release, "1.2.3"))

    def test_update_cycle_uses_at_most_two_requests_with_shared_timeout_budget(self):
        manifest = _manifest("v1.2.3", "1.2.3")
        release = _release("v1.2.3", assets=_canonical_release_assets(manifest))
        responses = [_Response([release]), _Response(manifest)]

        with patch("urllib.request.urlopen", side_effect=responses) as network:
            with patch.object(update_checker.time, "monotonic", side_effect=[100.0, 100.0, 120.0]):
                info = update_checker._get_update_info("1.0.0")

        self.assertIsNotNone(info)
        self.assertEqual(network.call_count, 2)
        urls = [call.args[0].full_url for call in network.call_args_list]
        self.assertEqual(urls[0], update_checker.RELEASES_API)
        self.assertEqual(
            urls[1],
            "https://github.com/BleedingDev/biomem/releases/download/"
            "v1.2.3/release-manifest.json",
        )
        timeouts = [call.kwargs["timeout"] for call in network.call_args_list]
        self.assertTrue(all(0 < timeout <= update_checker.CHECK_TIMEOUT for timeout in timeouts))
        self.assertLessEqual(sum(timeouts), update_checker.TOTAL_CHECK_TIMEOUT)

        with patch("urllib.request.urlopen", return_value=_Response([])) as no_update_network:
            info = update_checker._get_update_info("1.0.0")
        self.assertIsNone(info)
        no_update_network.assert_called_once()

    def test_check_is_notification_only_even_when_auto_flags_are_true(self):
        info = update_checker.UpdateInfo(
            version="1.2.3",
            tag="v1.2.3",
            release_url="https://github.com/BleedingDev/biomem/releases/tag/v1.2.3",
            manifest_status="verified_manifest_inventory",
            provenance_status="unverified",
            upgrade_routes=("uv tool upgrade biomem-memory",),
        )
        with patch.object(update_checker, "_get_update_info", return_value=info):
            with patch.object(update_checker, "download_and_install_update") as download:
                with patch.object(update_checker, "_trigger_silent_installation") as execute:
                    with patch.object(update_checker, "_notify_user") as notify:
                        result = update_checker.check_for_update(
                            "1.0.0",
                            auto_download=True,
                            auto_install=True,
                            backup_callback=Mock(),
                        )

        self.assertEqual(result, "biomem 1.2.3")
        download.assert_not_called()
        execute.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["provenance_status"], "unverified")

    def test_notification_never_claims_cryptographic_provenance_verification(self):
        with self.assertLogs(update_checker.logger, level="INFO") as captured:
            update_checker._notify_user(
                "biomem 1.2.3",
                "https://github.com/BleedingDev/biomem/releases/tag/v1.2.3",
                manifest_status="verified_manifest_inventory",
                provenance_status="unverified",
            )

        rendered = "\n".join(captured.output)
        self.assertIn(
            "Manifest/inventory verification: verified_manifest_inventory",
            rendered,
        )
        self.assertIn("Cryptographic provenance verification: unverified", rendered)
        self.assertNotIn("Cryptographic provenance verification: verified", rendered)

    def test_legacy_download_and_execution_entry_points_are_permanently_disabled(self):
        backup = Mock()
        with patch("urllib.request.urlopen") as network:
            with patch("builtins.open") as file_open:
                with patch("subprocess.Popen") as execute:
                    self.assertFalse(
                        update_checker.download_and_install_update(
                            "https://attacker.invalid/setup.exe",
                            "setup.exe",
                            auto_install=True,
                            backup_callback=backup,
                        )
                    )
                    self.assertFalse(
                        update_checker._trigger_silent_installation(
                            "/tmp/setup.exe", backup_callback=backup
                        )
                    )
        network.assert_not_called()
        file_open.assert_not_called()
        execute.assert_not_called()
        backup.assert_not_called()

    def test_background_loop_checks_once_then_waits_without_retry_storm(self):
        stopped = RuntimeError("stop test loop")
        with patch.object(update_checker, "check_for_update", side_effect=OSError("offline")) as check:
            with patch.object(update_checker.time, "sleep", side_effect=stopped) as sleep:
                with self.assertRaisesRegex(RuntimeError, "stop test loop"):
                    update_checker._update_check_loop("1.0.0")
        check.assert_called_once()
        sleep.assert_called_once_with(update_checker.UPDATE_CHECK_INTERVAL)

    def test_async_entry_point_preserves_daemon_thread_contract(self):
        thread = Mock(spec=threading.Thread)
        with patch.object(update_checker.threading, "Thread", return_value=thread) as factory:
            result = update_checker.check_for_update_async("1.0.0")

        self.assertIsNone(result)
        self.assertTrue(factory.call_args.kwargs["daemon"])
        self.assertIs(factory.call_args.kwargs["target"], update_checker._update_check_loop)
        thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
