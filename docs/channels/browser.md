# Browser release channels

Browser distribution is deliberately split into one low-cost normal-user path
per browser. A build or development-mode install is evidence about the tested
code, not evidence that a store or signing provider accepted it.

For `0.0.2`, every live store/signing result is `BLOCKED_ENVIRONMENT` until its
provider receipt exists. Local packaging and headed functional tests do not
change that status.

## Artifact boundary

`scripts/build_extensions.sh` writes `dist/browser-artifacts.json`. Release
assembly must obey its classification:

| Artifact | Class | Public release rule |
| --- | --- | --- |
| `chrome-biomem.zip` | `public_store_upload_input` | Always a canonical public GitHub Release asset and the exact CWS upload input. |
| `chrome-biomem.crx` | `development_or_managed_crx` | CI/development only; never a normal public download. |
| `firefox-biomem-unsigned.xpi` | `unsigned_store_signing_input` | CI-only input to AMO; never public. |
| `firefox-biomem-<version>-amo-signed.xpi` | verified AMO output | Conditional assembly input only after exact payload/ID/version and cryptographic Mozilla signature checks; public only after a finalizer verifies the hosted release asset. |
| `safari-biomem-macos-development-adhoc.zip` | `safari_development_adhoc` | CI-only; never public. |
| release-mode Safari ZIP | `apple_signing_notarization_input` | CI-only until the Apple boundary returns fully verified evidence. |

Private CRX keys under `dist/keys` are never uploaded. An ephemeral CRX key
changes the extension ID, so it cannot establish a stable public identity.

## Run the safe preflight

Use **Actions > Publish browser channels > Run workflow**, provide the exact
40-character commit SHA and matching `vMAJOR.MINOR.PATCH` tag, select any of
`chrome_web_store,firefox_amo,safari_public`, and leave `dry_run` enabled. A dry
run builds and validates packages but never calls Google, Mozilla, or Apple.
Selected channels are recorded as `blocked_environment` with
`not_attempted_dry_run`; unselected channels are `skipped_not_configured`.

## Chrome Web Store

Chrome Web Store distribution may require its one-time developer-account fee,
but it does not require a recurring signing service. Chrome uses the current
Web Store API v2. The workflow uploads the exact tested
ZIP and checks both the returned item ID and version before submitting it. A
2xx publish response is not treated as publication evidence: the workflow then
fetches store status again and re-verifies the exact item, version, 100 percent
deployment, warning state, and takedown state. A successful submission that is
still under review remains
`blocked_environment`; only a verified `PUBLISHED` store state becomes
`published`, and only when the exact version is deployed to 100 percent of the
public channel. A partial rollout, trusted-testers-only state, policy warning,
or taken-down item is not a normal-user release and remains
`blocked_environment`.

Retries first inspect the existing submitted/published version and never
overwrite a matching review. Asynchronous uploads are polled with `fetchStatus`;
if the API cannot prove the uploaded version, the workflow stops as
`blocked_environment` instead of publishing an unverified draft. Invalid
package/version responses are `failed`, while credentials, rate limits, review,
transport failures, and provider availability remain `blocked_environment`.

Repository variables:

- `CWS_PUBLISH_ENABLED=true` (explicit kill switch)
- `CWS_PUBLISHER_ID`
- `CWS_ITEM_ID`

The expected 32-character Chrome extension ID is not another mutable variable:
it must be committed as `package_identifiers.chrome` in
`release/release-policy.json`. Until the store assigns that ID and the contract
is committed, selected Chrome publishing remains `blocked_environment`.

Repository secrets:

- `CWS_CLIENT_ID`
- `CWS_CLIENT_SECRET`
- `CWS_REFRESH_TOKEN`

The developer must first register the store account, enable the API, complete
the listing/privacy forms, and perform any one-time dashboard enablement. The
official setup and API endpoints are documented by
[Chrome for Developers](https://developer.chrome.com/docs/webstore/using-api).
Account, permission, review, two-factor, or manual-dashboard obstacles are
`blocked_environment`; an identity mismatch is `failed`.

## Firefox AMO

AMO signing is free. Set:

- repository variable `AMO_PUBLISH_ENABLED=true`
- repository variable `AMO_CHANNEL=unlisted` for a signed GitHub-hosted XPI, or
  `listed` for an AMO listing/submission
- secrets `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`

The workflow uses exact `web-ext` 10.6.0 from the committed npm integrity lock
to unpack and sign the canonical tested XPI. A local readiness step receives
only a boolean credentials-present signal. Missing configuration stops there;
it does not run Node setup, npm, curl, or AMO. `npm ci --ignore-scripts` then
runs in a separate step without raw AMO secrets, and the signing step executes
only the already-installed local binary instead of fetching packages at
runtime.
Until the remaining upstream `image-size` parser advisories have a patched
release, preflight examines every archive member's bytes, regardless of its
filename, and rejects ICNS, JXL, HEIF, and HEIC magic. It accepts only bounded
PNG icons with a valid PNG signature. A hard timeout also contains the locked
linter if an upstream parser still stalls.
For unlisted output it accepts exactly one XPI containing the stable
`biomem@bleedingdev.github.io` ID and release version. Verification uses the
committed Mozilla AMO production root (with pinned PEM and DER hashes), disables
system trust paths, requires the CMS signer set to contain exactly one
certificate whose subject contains exactly one CN equal to that add-on ID, and
verifies the full CMS → signature file → manifest → every ZIP entry digest
chain. It also proves that every payload entry is byte-for-byte identical to
the tested unsigned input. It does not enforce additional signer EKU, KU, or CA
properties. The resulting Actions artifact contains
the signed XPI plus filename, signed SHA-256, size, version, and input SHA-256
metadata. It is only `ready` for release assembly: channel evidence stays
`blocked_environment` until a finalizer hosts it and verifies the remote asset.
Artifact names include the SHA-256 of the tested unsigned input, so a retry or
finalizer cannot accidentally consume a different build of the same version.
If a transient workflow artifact disappears, recovery must prove the same exact
version from a durable artifact or AMO itself and run the same cryptographic
and payload verification. A missing or deleted receipt is
`blocked_environment`; it never authorizes a blind second submission or an
unsigned fallback.
Listed submission also remains `blocked_environment` while AMO review is
pending. Mozilla's official signing instructions are in the
[Firefox Extension Workshop](https://extensionworkshop.com/documentation/develop/getting-started-with-web-ext/#package-sign-and-publish-your-extension).

## Safari

`safari_public` calls the optional Apple boundary only when explicitly selected
and `dry_run` is false. The current boundary intentionally performs readiness
and evidence validation, not credential import or public publication. It
therefore cannot emit a public Safari package yet. Missing Apple membership,
certificates, provisioning, permissions, notarization, or manual extension
enablement is `blocked_environment`, never PASS. Safari is not a v1 blocker.

## Functional evidence

Packaging/store state and extension behavior are separate. The existing
cross-browser contract suite proves local transport behavior. Previous headed
Chrome/Firefox/Safari evidence may be reused; do not spend provider prompts or
repeat unsafe account actions. If a live ChatGPT checkpoint is ever repeated,
use Instant and exactly one cautious prompt with no automatic retry.
