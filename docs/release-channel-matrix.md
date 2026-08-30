# Release-channel matrix

This matrix separates a tested implementation from a live distribution
receipt. The target version is `0.0.2` alpha. Until the first immutable release
and each external provider interaction exist, the corresponding live result is
`BLOCKED_ENVIRONMENT`, even when local tests pass.

| Channel | Cost baseline | User-facing output | Current live status | What changes the status |
| --- | --- | --- | --- | --- |
| GitHub Release/source | Zero | Versioned source plus canonical wheel, sdist, platform archives, Chrome ZIP, checksums, and manifest | `BLOCKED_ENVIRONMENT` before the first immutable `v0.0.2` publication | Exact tag-bound workflow publishes and re-verifies the immutable asset inventory |
| PyPI (`uv`/`pipx`) | Zero | Exact canonical wheel and sdist | `BLOCKED_ENVIRONMENT` until project ownership, Trusted Publisher, OIDC, upload, provenance, and install receipt exist | PyPI reports the exact two files/digests and valid publisher provenance; exact-version install smoke succeeds |
| Homebrew formula | Zero | Formula in `BleedingDev/homebrew-tap` referencing the two macOS release archives | `BLOCKED_ENVIRONMENT` until the release, tap, and both macOS runner receipts exist | Hash-pinned formula passes install, upgrade, test, and uninstall on Intel and Apple silicon |
| WinGet | Zero | Portable user-scope manifest referencing the canonical Windows ZIP | `BLOCKED_ENVIRONMENT` until catalog review and a Windows lifecycle receipt exist | Public catalog returns the exact package/version and clean-runner install/upgrade/uninstall succeeds |
| Scoop | Zero | Manifest referencing the canonical Windows ZIP | `BLOCKED_ENVIRONMENT` until an approved bucket and Windows lifecycle receipt exist | Approved bucket exposes the exact manifest and clean-runner lifecycle succeeds |
| Firefox AMO unlisted | Zero | Verified AMO-signed XPI attached to the same GitHub Release | `BLOCKED_ENVIRONMENT` until API credentials/provider output and the exact final release-asset handoff are verified | AMO signing or durable replay returns exact bytes; Mozilla signature/payload checks pass; finalizer hosts and re-verifies them |
| Chrome Web Store | Low one-time account cost | Public store listing built from canonical `chrome-biomem.zip` | `BLOCKED_ENVIRONMENT` until account/API setup, item identity, submission, review, and 100% public deployment are verified | Store status proves the exact version is public, unwarned, not taken down, and deployed to the public channel at 100% |
| Public Safari | Paid Apple path | Developer ID signed and notarized Safari app/extension | `BLOCKED_ENVIRONMENT`; readiness checks do not publish an app | Apple identities/profiles, notarization, stapling, Gatekeeper, and public distribution are all verified |
| Notarized macOS installer | Paid Apple path | Developer ID signed/notarized CLI installer | `BLOCKED_ENVIRONMENT`; zero-cost archives remain available independently | Nested app and installer identities, notarization, stapling, and Gatekeeper installation are verified |
| Windows Authenticode | Conditional | Signed replacement for the exact Windows payload | `BLOCKED_ENVIRONMENT`; no provider adapter is enabled | SignPath Foundation accepts the project and verified adapter/evidence complete, or an explicitly chosen commercial provider does so |

## Public browser-asset rule

`chrome-biomem.zip` is always a canonical public GitHub Release asset. A
verified AMO-signed XPI is conditional and public only after its exact handoff
to the finalizer. The unsigned Firefox input, development/ephemeral CRX, and
ad-hoc Safari output are CI-only and must never appear in a public release.

## Interpretation

- `PASS` means a named test or verification actually ran successfully.
- `published` means the exact version can be obtained by a normal user from
  that channel and the workflow retained evidence of that state.
- `BLOCKED_ENVIRONMENT` covers missing logins, secrets, permissions, provider
  review, signing identity, external runner, catalog acceptance, or manual
  enablement. It must not be converted to PASS because local generation worked.
- Optional paid signing improves first-run trust but does not gate the free
  GitHub/Python/Homebrew/Windows-package-manager baseline or `1.0.0`.
