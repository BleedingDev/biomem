# biomem for Safari

This is the canonical Safari Web Extension resource tree consumed by the Xcode
host application. A local ad-hoc build is useful for testing but is not a
normal-user distributable.

## Development versus public distribution

`scripts/build_extensions.sh --safari-mode=development` produces
`safari-biomem-macos-development-adhoc.zip`. The archive contains
`DEVELOPMENT-ONLY.json` and is classified `ci_only`, `distributable: false`.
It must never be attached to a public GitHub Release.

The release-mode ZIP is still only an Apple signing/notarization input. Public
Safari output requires the explicitly enabled `safari_public` boundary to
verify the expected Developer ID team, both bundle IDs, secure timestamp,
accepted notarization, stapling, and Gatekeeper acceptance. Until that boundary
is implemented and configured, the truthful result is `blocked_environment`;
when the channel is not selected it is `skipped_not_configured`.

Launching the host app, manually enabling the extension in Safari Settings, or
seeing the local connection indicator does not prove public signing, store
distribution, or end-to-end memory behavior.

See [the browser channel guide](../../docs/channels/browser.md) and the Xcode
host's `BUILDING.md` for the separate development procedure.
