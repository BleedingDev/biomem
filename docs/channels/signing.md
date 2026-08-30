# Optional signing channels

Signing is an optional transformation of an already tested canonical artifact.
It is not a `v1` core-release gate. Linux, macOS, and Windows core archives can
be published without signing credentials; the manifest records unselected
signing channels as `skipped_not_configured`.

For `0.0.2`, no live Windows provider adapter or Apple signing/notarization
receipt exists. Those live lanes are `BLOCKED_ENVIRONMENT`; readiness and local
evidence-validation tests are not signing PASS results.

Selecting `windows_signed`, `safari_public`, or
`macos_notarized_installer` is explicit. A selected channel that lacks a
login, permission, provider acceptance, credential, input artifact, or manual
enablement records `blocked_environment`. It must never publish its optional
output or silently substitute an unsigned or ad-hoc artifact. The unrelated
canonical GitHub release may still publish according to the core policy.

## Artifact-in, evidence-out contract

The signing boundary freezes the SHA-256 of the exact artifact produced by the
core build before any provider sees it. A signing adapter must accept only that
artifact and digest. It may emit a replacement only after native platform
verification and `scripts/release/verify_signing_evidence.py verify` succeed.

Successful signing evidence has this shape:

```json
{
  "schema_version": 1,
  "channel": "windows_signed",
  "status": "verified",
  "channel_status": "ready_for_assembly",
  "publication_claimed": false,
  "provider": "signpath_foundation",
  "input": {"name": "artifact.zip", "sha256": "<frozen digest>"},
  "output": {"name": "artifact-signed.zip", "sha256": "<new digest>", "size": 1},
  "verified_identity": {"signer": "<exact expected subject>"},
  "timestamp": {"status": "verified", "value": "<timezone-aware ISO-8601>"},
  "provider_receipt": "https://<verified request receipt>"
}
```

`ready_for_assembly` is deliberately not a publication claim. A later release
assembly step must match the input digest, replace the unsigned artifact with
the verified output, regenerate checksums and provenance subjects, and only
then publish. A stale digest, unchanged output digest, invalid signature,
wrong signer, absent or invalid timestamp, or unverifiable receipt fails the
boundary.

Apple evidence additionally requires the exact team and bundle identifiers,
an accepted notarization request, a validated stapled ticket, and an accepted
Gatekeeper assessment. The canonical identifiers are:

- Safari host: `com.bleedingdev.biomem.safari`
- Safari extension: `com.bleedingdev.biomem.safari.Extension`
- macOS CLI: `com.bleedingdev.biomem.cli`

The expected Windows signer subject and Apple Developer ID identities/team are
repository variables, not values inferred from whatever certificate happens
to be installed. Verification compares them exactly.

## Windows: SignPath Foundation readiness

SignPath Foundation is the preferred Windows path. The project has not yet
recorded Foundation acceptance, a SignPath organization/project, or an
approved signing adapter. `.github/workflows/release-sign-windows.yml`
therefore performs readiness and digest-boundary checks only. Even when every
setting exists, it truthfully records
`blocked_environment/provider_adapter_not_enabled`; it does not submit a
signing request.

Do not restore an exportable Windows certificate/private-key secret. The core
contract has no PFX gate. Do not represent a self-signed certificate as public
Authenticode trust. Azure Artifact Signing and Certum are unimplemented later
options, not automatic fallbacks.

Before applying to or enabling SignPath Foundation:

1. Confirm the project satisfies the [Foundation OSS conditions](https://signpath.org/terms.html): OSI-approved licensing, no proprietary components, active maintenance, an existing documented release, project ownership, MFA, review, and signing-approval roles.
2. Publish a repository/home-page section titled **Code signing policy**. It must credit “Free code signing provided by SignPath.io, certificate by SignPath Foundation,” name committers/reviewers/approvers, and link the privacy policy.
3. Publish a privacy policy covering every networked service. The minimum Foundation wording is not enough if application features contact additional services.
4. Review dependencies and packaged binaries. Only project-owned binaries may receive the project signature; upstream binaries remain separately attributable.
5. Configure the SignPath project, `release-signing` policy, manual approval, GitHub trusted-build-system/origin verification, and a versioned artifact configuration that restricts product name/version metadata. SignPath recommends checking the artifact-configuration slug into source control.
6. Install the SignPath GitHub App with repository access. The [official GitHub connector](https://docs.signpath.io/trusted-build-systems/github) requires the unsigned input to be uploaded as a GitHub artifact before submission and verifies GitHub build provenance.
7. After acceptance, set `SIGNPATH_FOUNDATION_ACCEPTED=true` and
   `WINDOWS_SIGNING_ENABLED=true`, plus `SIGNPATH_ORGANIZATION_ID`,
   `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_SIGNING_POLICY_SLUG`,
   `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG`, and the exact
   `SIGNPATH_EXPECTED_SIGNER_SUBJECT`. Set the repository variable
   `SIGNPATH_CREDENTIALS_CONFIGURED=true` only after the future provider token
   has actually been prepared. This is a nonsecret presence declaration, not
   the token and not signing evidence.
8. Pin the official SignPath submission action to a reviewed commit, wait for completion, retain its request URL, verify Authenticode policy and the RFC 3161 timestamp on Windows, generate native evidence, and invoke the verifier.

The exact insertion point is the comment in the “Evaluate SignPath
configuration without signing” step: after the input digest is frozen and
before channel evidence is written. No signing action should be added before
Foundation acceptance and an artifact configuration are confirmed.

The current workflow declares no raw SignPath secret. Do not upload a token
yet. When the submission adapter is implemented, its secret name and permission
scope must be introduced together, and the raw token must be exposed only to
the exact pinned submission step—not readiness or reusable-workflow forwarding.

## Apple: paid, explicitly enabled lanes

`safari_public` and `macos_notarized_installer` require paid Apple Developer
Program access and `APPLE_SIGNING_ENABLED=true`. They are isolated in
`.github/workflows/release-sign-apple.yml`; normal releases never import Apple
credentials or run notarization. The readiness workflow intentionally does no
credentialed transformation today.

### Current Apple repository variables

The current workflows consume only nonsecret repository/organization
variables. Configure expected identities exactly; do not infer them from an
arbitrary certificate installed on a runner:

- `APPLE_EXPECTED_TEAM_ID` — the Apple Developer team ID.
- `APPLE_EXPECTED_DEVELOPER_ID_APPLICATION` — the complete expected Developer
  ID Application identity, ending in ` (<APPLE_EXPECTED_TEAM_ID>)`.
- `APPLE_EXPECTED_DEVELOPER_ID_INSTALLER` — the complete expected Developer ID
  Installer identity, also ending in the configured team ID; required by
  `macos_notarized_installer`.
- `APPLE_SIGNING_ENABLED=true` — deliberate manual selection switch. Keep it
  false until the future adapter is ready for an authorized attempt.

The following variables are boolean presence declarations. Their exact value
must be `true`; they never contain credential bytes:

- `APPLE_COMMON_CREDENTIALS_CONFIGURED` — asserts that the team ID, Developer
  ID Application certificate/private key and its P12 password, plus App Store
  Connect notarization key, key ID, and issuer ID have been prepared.
- `APPLE_INSTALLER_CREDENTIALS_CONFIGURED` — additionally asserts that the
  Developer ID Installer certificate/private key and its P12 password have
  been prepared for `macos_notarized_installer`.
- `APPLE_SAFARI_CREDENTIALS_CONFIGURED` — additionally asserts that the Safari
  host and extension Developer ID provisioning profiles have been prepared for
  `safari_public`.

These booleans prove only an operator declaration. They expose no secret,
verify no certificate or profile, and cannot make readiness PASS. The adapter
is hard-disabled today, so a selected Apple lane remains
`BLOCKED_ENVIRONMENT/provider_adapter_not_enabled` even when every variable is
set.

### Future Apple credential material

Obtain the application and installer certificates from Apple Certificates,
Identifiers & Profiles under the paid team, install them with their private
keys, then export each identity as a password-protected P12. Create separate
Developer ID provisioning profiles for the canonical Safari host and extension
bundle identifiers. Create the notarization API key in App Store Connect under
Users and Access / Integrations, and retain the downloaded `.p8`, its key ID,
and issuer ID; the private key is available for download only once.

The current workflows declare **no raw Apple secrets**, so operators must not
upload P12 files, passwords, profiles, or notary keys to GitHub yet. When a real
adapter is implemented, the secret names and scopes must land atomically with
the credential-import/notarization steps. Binary material will need
single-line base64 encoding, raw values must be exposed only to the exact step
that imports an ephemeral keychain or submits notarization, and cleanup must
run on every outcome. Readiness steps and reusable forwarding must continue to
receive only the boolean declarations above.

The future Safari adapter must use the checked-in Xcode project and
`scripts/build_extensions.sh --no-crx --safari-mode=release`, then require all
of the following before returning an artifact:

1. the exact configured Developer ID Application identity and team;
2. exact host and extension provisioning-profile team/bundle identifiers;
3. strict deep `codesign` verification of the app and extension;
4. `notarytool` status `Accepted` and a retained request ID;
5. successful staple validation and Gatekeeper execution assessment; and
6. a re-packed artifact whose new digest passes the signing evidence verifier.

The future macOS installer adapter must separately verify the nested CLI's
Developer ID Application identity and `com.bleedingdev.biomem.cli` identifier,
the package's exact Developer ID Installer identity, accepted notarization,
stapling, and Gatekeeper installation assessment.

Missing credentials remain `blocked_environment`; an ad-hoc development build
is never a public Safari or notarized-installer output.
