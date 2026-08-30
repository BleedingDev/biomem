# Release rehearsal

Use this checklist to rehearse `0.0.2` without confusing local evidence with a
live publication. The dry run is intentionally no-write.

## 1. Freeze one source identity

Confirm that the annotated tag exists, resolves to the intended commit, and is
contained in `main`:

```sh
git rev-parse 'v0.0.2^{commit}'
git merge-base --is-ancestor 'v0.0.2^{commit}' origin/main
```

Record the resulting 40-character source SHA. Do not rehearse one commit and
publish another.

## 2. Run the no-write workflow

```sh
gh workflow run release.yml \
  --ref v0.0.2 \
  -f tag=v0.0.2 \
  -f dry_run=true \
  -f channels=none
```

Optionally select a channel to exercise its local preflight, but expect the
selected external channel to report
`BLOCKED_ENVIRONMENT/not_attempted_dry_run`. A dry run must not call stores,
PyPI, catalogs, signing providers, or notarization services, must not create or
edit a GitHub Release, and must not claim a live OIDC-backed attestation.

## 3. Inspect local evidence

The run is suitable preparation only when all of the following hold:

- package builds, source/version binding, archive allowlists, and checksums
  pass for the exact tag SHA;
- the canonical public set includes `chrome-biomem.zip`;
- the public set excludes unsigned Firefox, development/ephemeral CRX, and
  ad-hoc Safari artifacts;
- any AMO-signed XPI is absent unless exact signed-output metadata and bytes
  were recovered and verified for this version/input/source identity;
- every selected external provider remains `BLOCKED_ENVIRONMENT` in dry-run
  evidence rather than PASS or `published`; and
- no mutable `latest` URL, branch ref, or reconstructed artifact is used.

Dry-run success proves local assembly and policy only. Live GitHub immutable
release configuration, GitHub attestation, PyPI Trusted Publishing, store
accounts/review, external catalogs, Apple signing/notarization, SignPath, and
clean external runner lifecycles remain `BLOCKED_ENVIRONMENT` until exercised.

## 4. Configure only the channels being attempted

For the canonical GitHub publication, configure the repository/organization
secret `IMMUTABLE_RELEASES_TOKEN` with repository Administration: read. For an
explicit browser attempt, configure only the variables and secrets listed in
[release automation](releasing.md). PyPI uses its Trusted Publisher and `pypi`
environment approval; it has no stored PyPI token.

Do not configure Apple or Windows signing merely to satisfy a general release
check. Those optional lanes stay unselected until their provider is actually
available.

## 5. Publish from the exact tag

For the core release with no optional channels, pushing the tag starts the
release workflow. To explicitly select available channels, dispatch from the
tag itself:

```sh
gh workflow run release.yml \
  --ref v0.0.2 \
  -f tag=v0.0.2 \
  -f dry_run=false \
  -f channels=pypi
```

Replace `pypi` only with channels intentionally being attempted. A missing
login, permission, review, signing identity, catalog, or manual enablement is
`BLOCKED_ENVIRONMENT`; do not rerun aggressively or relabel it as successful.
Blocked optional channels must not remove unrelated core assets.

## 6. Verify receipts before advertising commands

After publication, verify the immutable GitHub Release and download
`SHA256SUMS.txt` plus `release-manifest.json`. Then require the channel-specific
receipt from the [release-channel matrix](release-channel-matrix.md): exact
PyPI files and provenance, exact store version/status, exact tap/catalog entry,
or native signing/notarization evidence.

Only after that receipt exists should README or channel documentation present
the corresponding package-manager command as currently available. Until then,
the supported path is a source checkout or the already verified canonical
GitHub asset—not a promised package-manager listing.
