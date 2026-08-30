# Safari host builds

The macOS host contains the Safari Web Extension and reports both extension enablement and the health of the local Biomem service. The app and extension use these identifiers:

- host: `com.bleedingdev.biomem.safari`
- extension: `com.bleedingdev.biomem.safari.Extension`

Both targets share release version `0.0.2` and build number `1`.

## Extension resource source

`extensions/safari-src` is the only canonical web-extension resource tree. The Xcode project references it directly for both extension targets. `scripts/build_extensions.sh --validate-only` verifies those references and rejects an existing generated `extensions/safari-xcode/Dist` app when its packaged resources differ byte-for-byte. A normal Safari build invalidates the old generated `Dist` before Xcode runs and verifies the newly built extension resources before packaging.

## Local development build

Debug builds are deliberately ad-hoc signed. They are suitable only for local development and are not distributable artifacts:

```sh
scripts/build_extensions.sh --safari-mode=development --no-crx
```

This produces `dist/safari-biomem-macos-development-adhoc.zip`. Its filename and embedded `DEVELOPMENT-ONLY.json` both identify it as non-distributable. The equivalent direct Xcode command is:

```sh
xcodebuild \
  -project "BDBM Memory Plugin.xcodeproj" \
  -scheme "BDBM Memory Plugin (macOS)" \
  -configuration Debug \
  -derivedDataPath /tmp/biomem-safari-derived-data \
  build
```

The built app is at `/tmp/biomem-safari-derived-data/Build/Products/Debug/BDBM Memory Plugin.app`.

## Optional distribution build

Public Safari distribution is an explicitly selected, paid channel. It is not
part of the core release and missing Apple access must not block unrelated
artifacts. The reusable Apple workflow currently checks readiness only; it does
not import credentials, sign, notarize, or publish. See
[`docs/channels/signing.md`](../../../docs/channels/signing.md) for the channel
contract and enablement state.

For an approved signing environment, release is the packaging script's default
Safari mode. It fails before packaging unless the exact installed `Developer
ID Application` identity and its matching team are provided:

```sh
BIOMEM_APPLE_TEAM_ID="XXXXXXXXXX" \
BIOMEM_DEVELOPER_ID_APPLICATION="Developer ID Application: Approved Name (XXXXXXXXXX)" \
scripts/build_extensions.sh --no-crx
```

Do not store those values, credentials, provisioning profiles, or private keys
in this repository. In automation, secrets must be scoped only to the
credential-import/notarization steps and an ephemeral signing keychain must be
deleted on every outcome. The expected identity and team are explicit
configuration: never accept the first installed identity as authoritative.

The host app and extension have distinct bundle identifiers, so the Xcode
signing environment must make the correct manual provisioning profile
available for each target. Each profile must resolve to team
`BIOMEM_APPLE_TEAM_ID` and, respectively,
`com.bleedingdev.biomem.safari` or
`com.bleedingdev.biomem.safari.Extension`. The script deliberately does not
force one global profile onto both targets. The equivalent direct archive
command is:

```sh
xcodebuild archive \
  -project "BDBM Memory Plugin.xcodeproj" \
  -scheme "BDBM Memory Plugin (macOS)" \
  -configuration Release \
  -archivePath /tmp/biomem-safari.xcarchive \
  DEVELOPMENT_TEAM="$BIOMEM_APPLE_TEAM_ID" \
  CODE_SIGN_IDENTITY="$BIOMEM_DEVELOPER_ID_APPLICATION"
```

After export, submit the app with `notarytool`, retain the request ID, require
the exact `Accepted` result, staple the ticket, then verify the final artifact:

```sh
codesign --verify --deep --strict --verbose=2 "BDBM Memory Plugin.app"
spctl --assess --type execute --verbose=4 "BDBM Memory Plugin.app"
xcrun stapler validate "BDBM Memory Plugin.app"
```

Record the SHA-256 before the credentialed transformation and require a new
digest afterward. The final evidence must name the exact signer, team, both
bundle identifiers, secure timestamp, notarization request, staple result, and
Gatekeeper result. `scripts/release/verify_signing_evidence.py verify` enforces
that evidence boundary.

An unsigned or ad-hoc signed build must never be described as Gatekeeper-ready
or substituted for `safari_public`. Missing login, permission, credential,
profile, provider access, or explicit `APPLE_SIGNING_ENABLED=true` is
`blocked_environment`, never PASS. Safari Settings discovery must be checked
with the signed, notarized app installed in `/Applications` on a clean macOS
account.
