from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_policy = load_module("release_policy_for_publisher_tests", ROOT / "scripts/release_policy.py")
canonical = load_module("canonical_artifacts", ROOT / "scripts/release/canonical_artifacts.py")
publisher = load_module("publish_github_release", ROOT / "scripts/release/publish_github_release.py")

SOURCE_SHA = "a" * 40
REPOSITORY = "example/biomem"
RUN_ID = "12345"


class FakeClient:
    def __init__(self, tag: str, source_sha: str, url: str) -> None:
        self.tag = tag
        self.source_sha = source_sha
        self.url = url
        self.draft_url = url
        self.release: publisher.RemoteRelease | None = None
        self.writes: list[tuple[str, str]] = []
        self.immutable_enabled = True
        self.immutable_checks = 0
        self.immutable_error: Exception | None = None
        self.move_tag_on_publish = False
        self.publish_mutable = False
        self.missing_views_after_create = 0
        self.created = False
        self.dispatch_attempts = 0
        self.dispatch_failures = 0

    def immutable_releases_enabled(self, repository: str) -> bool:
        self.immutable_checks += 1
        if self.immutable_error is not None:
            raise self.immutable_error
        return self.immutable_enabled

    def resolve_tag_commit(self, repository: str, tag: str) -> str:
        return self.source_sha

    def view(self, repository: str, tag: str) -> publisher.RemoteRelease | None:
        if self.created and self.missing_views_after_create:
            self.missing_views_after_create -= 1
            return None
        return self.release

    def create_draft(
        self, repository: str, tag: str, source_sha: str, title: str,
        prerelease: bool, notes_file: Path,
    ) -> None:
        self.writes.append(("create", tag))
        self.created = True
        self.release = publisher.RemoteRelease(
            tag=tag,
            draft=True,
            immutable=False,
            url=self.draft_url,
            body=notes_file.read_text(encoding="utf-8"),
            assets=(),
        )

    def upload(self, repository: str, tag: str, path: Path) -> None:
        self.writes.append(("upload", path.name))
        assert self.release is not None
        asset = publisher.RemoteAsset(
            path.name, path.stat().st_size, f"sha256:{canonical._sha256(path)}"
        )
        self.release = publisher.RemoteRelease(
            **{**self.release.__dict__, "assets": (*self.release.assets, asset)}
        )

    def publish(self, repository: str, tag: str) -> None:
        self.writes.append(("publish", tag))
        assert self.release is not None
        self.release = publisher.RemoteRelease(
            **{
                **self.release.__dict__,
                "draft": False,
                "immutable": not self.publish_mutable,
                "url": self.url,
            }
        )
        if self.move_tag_on_publish:
            self.source_sha = "b" * 40

    def dispatch(self, repository: str, tag: str, source_sha: str) -> None:
        self.dispatch_attempts += 1
        if self.dispatch_failures:
            self.dispatch_failures -= 1
            raise publisher.BlockedEnvironment("repository dispatch temporarily unavailable")
        self.writes.append(("dispatch", tag))


class GitHubPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.policy = release_policy.resolve_policy("v1.2.3", "none", False)
        self.policy_path = self.root / "policy.json"
        release_policy.write_json(self.policy_path, self.policy)
        core = self.root / "core"
        core.mkdir()
        for artifact in self.policy["expected_core_artifacts"]:
            (core / artifact["name"]).write_bytes(artifact["name"].encode("utf-8"))
        (core / "chrome-biomem.zip").write_bytes(b"chrome-store-input")
        evidence = self.root / "evidence"
        evidence.mkdir()
        contract = release_policy.load_contract()
        for channel, definition in contract["channels"].items():
            if definition["selection"] == "explicit":
                release_policy.write_json(
                    evidence / f"{channel}.json",
                    release_policy.make_evidence(
                        self.policy, channel, "skipped_not_configured", "not_selected",
                    ),
                )
        provenance = self.root / "provenance.json"
        release_policy.write_json(provenance, {
            "status": "published",
            "source_sha": SOURCE_SHA,
            "provider": "github_actions_build_provenance",
            "receipt": "https://github.example/attestations/123",
        })
        self.publication = self.root / "publication"
        canonical.prepare_payload(
            self.policy_path,
            SOURCE_SHA,
            REPOSITORY,
            core,
            evidence,
            provenance,
            self.publication,
        )
        self.payload = self.publication / "payload"
        self.notes = self.publication / "release-notes.md"
        self.url = f"https://github.com/{REPOSITORY}/releases/tag/{self.policy['tag']}"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def client(self) -> FakeClient:
        return FakeClient(self.policy["tag"], SOURCE_SHA, self.url)

    def publish(self, client: FakeClient, run_id: str = RUN_ID) -> str:
        return publisher.publish_canonical_release(
            client,
            repository=REPOSITORY,
            run_id=run_id,
            policy_path=self.policy_path,
            source_sha=SOURCE_SHA,
            payload_dir=self.payload,
            release_notes=self.notes,
        )

    def owned_draft(self, client: FakeClient, assets=()) -> None:
        client.release = publisher.RemoteRelease(
            tag=self.policy["tag"],
            draft=True,
            immutable=False,
            url=self.url,
            body=publisher.ownership_marker(REPOSITORY, self.policy["tag"], SOURCE_SHA),
            assets=tuple(assets),
        )

    def test_fresh_release_uploads_exact_inventory_and_publishes_once(self) -> None:
        client = self.client()
        self.assertEqual(self.publish(client), self.url)
        self.assertEqual(client.immutable_checks, 2)
        self.assertEqual(sum(action == "create" for action, _ in client.writes), 1)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)
        uploaded = {value for action, value in client.writes if action == "upload"}
        self.assertEqual(uploaded, {path.name for path in self.payload.iterdir()})
        self.assertNotIn("clobber", " ".join(value for _, value in client.writes))

    def test_new_draft_visibility_is_retried_before_any_upload(self) -> None:
        client = self.client()
        client.missing_views_after_create = 5
        with mock.patch.object(publisher.time, "sleep") as sleep:
            self.assertEqual(self.publish(client), self.url)
        self.assertEqual(sleep.call_args_list, [
            mock.call(1.0), mock.call(2.0), mock.call(4.0), mock.call(8.0), mock.call(15.0),
        ])
        self.assertEqual(sum(action == "create" for action, _ in client.writes), 1)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)

    def test_untagged_draft_url_becomes_canonical_only_after_publication(self) -> None:
        client = self.client()
        client.draft_url = f"https://github.com/{REPOSITORY}/releases/tag/untagged-deadbeef"
        self.assertEqual(self.publish(client), self.url)
        assert client.release is not None
        self.assertEqual(client.release.url, self.url)
        self.assertTrue(client.release.immutable)

    def test_new_draft_visibility_exhaustion_is_blocked_without_upload_or_publish(self) -> None:
        client = self.client()
        client.missing_views_after_create = 6
        with mock.patch.object(publisher.time, "sleep") as sleep:
            with self.assertRaisesRegex(publisher.BlockedEnvironment, "bounded retries"):
                self.publish(client)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(client.writes, [("create", self.policy["tag"])])

    def test_mutable_repository_is_blocked_before_any_release_write(self) -> None:
        client = self.client()
        client.immutable_enabled = False
        with self.assertRaisesRegex(publisher.BlockedEnvironment, "disabled"):
            self.publish(client)
        self.assertEqual(client.immutable_checks, 1)
        self.assertEqual(client.writes, [])

    def test_unavailable_immutable_preflight_is_blocked_before_any_release_write(self) -> None:
        client = self.client()
        client.immutable_error = publisher.BlockedEnvironment("setting unavailable")
        with self.assertRaisesRegex(publisher.BlockedEnvironment, "unavailable"):
            self.publish(client)
        self.assertEqual(client.immutable_checks, 1)
        self.assertEqual(client.writes, [])

    def test_retry_keeps_matching_partial_assets_and_only_uploads_missing(self) -> None:
        client = self.client()
        retained_path = sorted(self.payload.iterdir())[0]
        retained = publisher.RemoteAsset(
            retained_path.name,
            retained_path.stat().st_size,
            f"sha256:{canonical._sha256(retained_path)}",
        )
        self.owned_draft(client, (retained,))
        self.publish(client, run_id="67890")
        self.assertNotIn(("upload", retained.name), client.writes)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)

    def test_stale_or_changed_remote_assets_refuse_before_any_write(self) -> None:
        for asset in (
            publisher.RemoteAsset("stale.zip", 1, "sha256:" + "0" * 64),
            publisher.RemoteAsset(
                sorted(self.payload.iterdir())[0].name, 1, "sha256:" + "0" * 64
            ),
        ):
            with self.subTest(asset=asset.name, size=asset.size):
                client = self.client()
                self.owned_draft(client, (asset,))
                with self.assertRaises(publisher.PublicationError):
                    self.publish(client)
                self.assertEqual(client.writes, [])

    def test_missing_remote_digest_is_blocked_environment_not_pass(self) -> None:
        client = self.client()
        path = sorted(self.payload.iterdir())[0]
        self.owned_draft(client, (publisher.RemoteAsset(path.name, path.stat().st_size, None),))
        with self.assertRaises(publisher.BlockedEnvironment):
            self.publish(client)
        self.assertEqual(client.writes, [])

    def test_unsafe_existing_releases_are_never_mutated(self) -> None:
        for draft, immutable in ((False, False), (True, True)):
            with self.subTest(draft=draft, immutable=immutable):
                client = self.client()
                client.release = publisher.RemoteRelease(
                    tag=self.policy["tag"],
                    draft=draft,
                    immutable=immutable,
                    url=self.url,
                    body=publisher.ownership_marker(
                        REPOSITORY, self.policy["tag"], SOURCE_SHA
                    ),
                    assets=(),
                )
                with self.assertRaisesRegex(publisher.PublicationError, "refusing|immutable"):
                    self.publish(client)
                self.assertEqual(client.writes, [])

    def test_exact_immutable_release_is_retry_success_without_mutation(self) -> None:
        client = self.client()
        self.assertEqual(self.publish(client), self.url)
        client.writes.clear()
        self.assertEqual(self.publish(client, run_id="67890"), self.url)
        self.assertEqual(client.writes, [])

    def test_dispatch_failure_retries_without_a_second_publication_mutation(self) -> None:
        client = self.client()
        self.assertEqual(self.publish(client), self.url)
        publication_writes = tuple(client.writes)
        self.assertEqual(sum(action == "publish" for action, _ in publication_writes), 1)
        client.dispatch_failures = 1
        with self.assertRaisesRegex(publisher.BlockedEnvironment, "temporarily unavailable"):
            publisher.verify_and_dispatch_canonical_release(
                client,
                repository=REPOSITORY,
                policy_path=self.policy_path,
                source_sha=SOURCE_SHA,
                payload_dir=self.payload,
            )
        self.assertEqual(tuple(client.writes), publication_writes)
        self.assertEqual(
            publisher.verify_and_dispatch_canonical_release(
                client,
                repository=REPOSITORY,
                policy_path=self.policy_path,
                source_sha=SOURCE_SHA,
                payload_dir=self.payload,
            ),
            self.url,
        )
        self.assertEqual(client.dispatch_attempts, 2)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)
        self.assertEqual(sum(action == "dispatch" for action, _ in client.writes), 1)

    def test_unowned_draft_is_never_adopted(self) -> None:
        client = self.client()
        client.release = publisher.RemoteRelease(
            tag=self.policy["tag"], draft=True, immutable=False, url=self.url,
            body="manual draft", assets=(),
        )
        with self.assertRaisesRegex(publisher.PublicationError, "not owned"):
            self.publish(client)
        self.assertEqual(client.writes, [])

    def test_mismatched_stable_ownership_marker_is_never_adopted(self) -> None:
        for tag, source_sha in (("v9.9.9", SOURCE_SHA), (self.policy["tag"], "b" * 40)):
            with self.subTest(tag=tag, source_sha=source_sha):
                client = self.client()
                client.release = publisher.RemoteRelease(
                    tag=self.policy["tag"], draft=True, immutable=False, url=self.url,
                    body=publisher.ownership_marker(REPOSITORY, tag, source_sha), assets=(),
                )
                with self.assertRaisesRegex(publisher.PublicationError, "not owned"):
                    self.publish(client, run_id="67890")
                self.assertEqual(client.writes, [])

    def test_tag_retargeted_during_publish_is_detected(self) -> None:
        client = self.client()
        client.move_tag_on_publish = True
        with self.assertRaisesRegex(publisher.PublicationError, "changed during"):
            self.publish(client)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)
        assert client.release is not None
        self.assertFalse(client.release.draft)
        self.assertTrue(client.release.immutable)

    def test_mutable_publication_is_never_accepted_as_success(self) -> None:
        client = self.client()
        client.publish_mutable = True
        with self.assertRaisesRegex(publisher.PublicationError, "published release is mutable"):
            self.publish(client)
        self.assertEqual(sum(action == "publish" for action, _ in client.writes), 1)
        assert client.release is not None
        self.assertFalse(client.release.draft)
        self.assertFalse(client.release.immutable)


class GhClientImmutablePreflightTests(unittest.TestCase):
    def test_missing_administration_read_token_is_blocked_without_calling_gh(self) -> None:
        client = publisher.GhClient("publication-token", "")
        with mock.patch.object(publisher.subprocess, "run") as run:
            with self.assertRaisesRegex(publisher.BlockedEnvironment, "Administration:read"):
                client.immutable_releases_enabled(REPOSITORY)
        run.assert_not_called()


class GhClientDraftDiscoveryTests(unittest.TestCase):
    def test_graphql_discovers_draft_then_rest_by_id_loads_exact_inventory(self) -> None:
        discovery = publisher.subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"data":{"repository":{"release":{"databaseId":321}}}}', stderr="",
        )
        release = publisher.subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "tag_name": "v1.2.3", "draft": True, "immutable": False,
                "html_url": "https://github.com/example/biomem/releases/tag/v1.2.3",
                "body": "owned", "assets": [{
                    "name": "asset.zip", "size": 7, "digest": "sha256:" + "a" * 64,
                }],
            }), stderr="",
        )
        client = publisher.GhClient("publication-token", "administration-read-token")
        with mock.patch.object(
            publisher.subprocess, "run", side_effect=[discovery, release]
        ) as run:
            value = client.view(REPOSITORY, "v1.2.3")
        assert value is not None
        self.assertTrue(value.draft)
        self.assertEqual(value.assets[0].digest, "sha256:" + "a" * 64)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn("graphql", calls[0])
        self.assertIn(f"repos/{REPOSITORY}/releases/321", calls[1])
        self.assertFalse(any("releases/tags" in argument for call in calls for argument in call))

    def test_graphql_null_release_is_not_found_without_rest_tag_lookup(self) -> None:
        discovery = publisher.subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"data":{"repository":{"release":null}}}', stderr="",
        )
        client = publisher.GhClient("publication-token", "administration-read-token")
        with mock.patch.object(
            publisher.subprocess, "run", return_value=discovery
        ) as run:
            self.assertIsNone(client.view(REPOSITORY, "v1.2.3"))
        run.assert_called_once()
        self.assertIn("graphql", run.call_args.args[0])

    def test_repository_dispatch_payload_contains_only_exact_release_identity(self) -> None:
        captured: dict[str, object] = {}

        def run(arguments, **kwargs):
            captured["arguments"] = arguments
            payload_path = Path(arguments[arguments.index("--input") + 1])
            captured["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))
            return publisher.subprocess.CompletedProcess(
                args=arguments, returncode=0, stdout="", stderr="",
            )

        client = publisher.GhClient("publication-token", "administration-read-token")
        with mock.patch.object(publisher.subprocess, "run", side_effect=run):
            client.dispatch(REPOSITORY, "v1.2.3", SOURCE_SHA)
        self.assertEqual(captured["payload"], {
            "event_type": "canonical_release_published",
            "client_payload": {"tag": "v1.2.3", "source_sha": SOURCE_SHA},
        })
        self.assertIn(f"repos/{REPOSITORY}/dispatches", captured["arguments"])

    def test_official_immutable_release_endpoint_is_called_with_separate_token(self) -> None:
        completed = publisher.subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"enabled":true,"enforced_by_owner":false}',
            stderr="",
        )
        client = publisher.GhClient("publication-token", "administration-read-token")
        with mock.patch.object(publisher.subprocess, "run", return_value=completed) as run:
            self.assertTrue(client.immutable_releases_enabled(REPOSITORY))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], [
            "gh", "api", "-H", "X-GitHub-Api-Version: 2026-03-10",
        ])
        self.assertEqual(arguments[4], f"repos/{REPOSITORY}/immutable-releases")
        self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "administration-read-token")

    def test_disabled_or_denied_preflight_never_returns_success(self) -> None:
        client = publisher.GhClient("publication-token", "administration-read-token")
        disabled = publisher.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)",
        )
        denied = publisher.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gh: Forbidden (HTTP 403)",
        )
        with mock.patch.object(publisher.subprocess, "run", return_value=disabled):
            self.assertFalse(client.immutable_releases_enabled(REPOSITORY))
        with mock.patch.object(publisher.subprocess, "run", return_value=denied):
            with self.assertRaisesRegex(publisher.BlockedEnvironment, "unavailable"):
                client.immutable_releases_enabled(REPOSITORY)


if __name__ == "__main__":
    unittest.main()
