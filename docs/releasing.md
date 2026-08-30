# Release automation

GitHub Actions builds from an immutable semantic-version tag and publishes one
canonical GitHub Release only after its exact asset inventory, checksums, and
local evidence pass. `0.0.2` is an alpha release; `1.0.0` will mean the project
has reached its own stability bar, not that every optional paid channel exists.

## Cost and failure policy

The zero-cost baseline consists of:

- versioned GitHub Release downloads and the public source tree;
- PyPI, installed into an isolated environment with `uv` or `pipx`;
- a Homebrew formula in a tap; and
- WinGet and Scoop manifests that reference the exact GitHub-hosted Windows
  archive.

Apple Developer ID/notarization and public Safari distribution are optional
paid UX paths. Windows Authenticode is optional; SignPath Foundation may be a
zero-cost route if the project is accepted, while commercial alternatives may
cost money. None is a blanket `1.0.0` gate.

Every selected channel uses one of four statuses: `published`,
`skipped_not_configured`, `blocked_environment`, or `failed`. Missing accounts,
logins, permissions, signing identities, provider approvals, store reviews,
catalog acceptance, or manual extension enablement are
`blocked_environment`—never PASS. A blocked optional channel does not suppress
unrelated canonical GitHub assets.

## Public GitHub Release boundary

The canonical public set contains the exact versioned Python wheel and source
archive, five platform CLI archives, `chrome-biomem.zip`, `SHA256SUMS.txt`, and
`release-manifest.json`. A verified AMO-signed XPI is added only when the
signing workflow hands the exact version, source SHA, unsigned-input digest,
signed digest, and signed bytes to the finalizer.

The public set never contains:

- `firefox-biomem-unsigned.xpi`;
- a development, self-signed, or ephemeral-identity CRX;
- an ad-hoc Safari development build; or
- a Safari/Apple output that has not completed its paid signing and
  notarization boundary.

Chrome Web Store, Firefox AMO, PyPI, Homebrew, WinGet, and Scoop are independent
distribution channels. Store or catalog acceptance is not inferred from a
successful package build.

## Repository and organization secrets

Reusable workflows receive secrets from their caller. Configure the following
as **repository secrets**, or as organization secrets explicitly shared with
this repository—not as a `release` environment secret that the reusable
workflow cannot inherit automatically:

```sh
gh secret set IMMUTABLE_RELEASES_TOKEN < token.txt
gh secret list
```

`IMMUTABLE_RELEASES_TOKEN` is required for a non-dry GitHub publication. Use a
narrow fine-grained token for this repository with repository
**Administration: read** access; the normal workflow token remains responsible
for release-content writes. The extra token is used only to verify the
repository's immutable-releases setting.

PyPI uses GitHub OIDC Trusted Publishing and therefore has no PyPI password or
API-token secret. Its `pypi` environment is an approval/identity boundary, not
a place to store a package-index token.

### Chrome Web Store

Repository variables:

- `CWS_PUBLISH_ENABLED=true` — explicit live-publication switch;
- `CWS_PUBLISHER_ID` — the Web Store publisher ID; and
- `CWS_ITEM_ID` — the store item ID, which must match the committed immutable
  Chrome ID in `release/release-policy.json`.

Repository or shared organization secrets:

- `CWS_CLIENT_ID`
- `CWS_CLIENT_SECRET`
- `CWS_REFRESH_TOKEN`

No private CRX key is a release secret. The canonical public Chrome asset is a
ZIP; a locally generated CRX identity is a development detail and is never
substituted for Web Store identity.

### Firefox AMO

Repository variables:

- `AMO_PUBLISH_ENABLED=true` — explicit provider-contact switch; and
- `AMO_CHANNEL=unlisted` for a GitHub-hosted AMO-signed XPI, or `listed` for a
  submission that remains blocked while Mozilla review is pending.

Repository or shared organization secrets:

- `AMO_JWT_ISSUER`
- `AMO_JWT_SECRET`

Create them in the Mozilla Add-ons Developer Hub API credentials page. AMO
signing is free, but the first live signing/replay and any review or permission
requirement remain `BLOCKED_ENVIRONMENT` until verified. A retry must recover
the exact existing AMO version or durable signed artifact; it must not create a
second version submission.

Apple and Windows signing configuration is intentionally isolated from this
baseline. See [optional signing channels](channels/signing.md) before enabling
either path.

## Dry-run contract

A dry run builds, packages, hashes, and performs local policy checks without
external publication writes. It does not contact CWS, AMO, PyPI, external
catalogs, or signing/notarization providers. It also cannot produce GitHub's
live OIDC-backed build attestation, because that attestation is created only in
the non-dry publication path. Any claim that those live provider or attestation
steps passed from a dry run is invalid.

Run the no-write rehearsal against the exact tag commit:

```sh
gh workflow run release.yml \
  --ref v0.0.2 \
  -f tag=v0.0.2 \
  -f dry_run=true \
  -f channels=none
```

See [the release rehearsal](release-rehearsal.md) for the evidence checklist.

## Creating `v0.0.2`

1. Confirm CI passes on the exact commit intended for release.
2. Create and push an annotated alpha tag:

   ```sh
   git tag -a v0.0.2 -m "biomem 0.0.2 alpha"
   git push origin v0.0.2
   ```

3. For explicitly selected channels, dispatch the workflow from that same tag
   with a comma-separated channel list. Do not enable unavailable channels to
   make a dashboard look green.
4. Verify the immutable GitHub Release inventory, `SHA256SUMS.txt`, manifest,
   and release URL before using any downstream package-manager metadata.
5. Treat PyPI, store, catalog, live attestation, Safari signing, notarization,
   and Authenticode as `BLOCKED_ENVIRONMENT` until their own live receipt or
   native verification exists.

The workflow refuses to overwrite an already published release. Retry the
failed downstream notification or provider recovery path rather than starting
a second publication of the same version.
