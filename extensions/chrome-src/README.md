# biomem for Chrome / Chromium

This source tree is the Chrome Web Store package source for the biomem memory
extension. The extension talks only to the local biomem service; it does not
require a provider login to connect locally. Site login is needed only when the
site itself requires it.

## Local development

Run `scripts/build_extensions.sh --no-safari`, then load
`dist/.stage/chrome` with **Developer mode > Load unpacked**. The popup's
`Connected locally` state proves only that the local biomem service is
reachable. It is not proof of prompt injection, memory retrieval, store
publication, or approval.

The generated artifacts have intentionally different trust classes:

- `chrome-biomem.zip` is the one tested Chrome Web Store upload input and may
  be kept on GitHub for inspection or managed/development deployment.
- `chrome-biomem.crx` and `dist/keys/*.pem` are development/managed-install
  material. They must never be attached to a normal public release. A stable
  private key is required to preserve the CRX identity; an ephemeral key means
  a different extension identity.

Normal Windows and macOS users install from the Chrome Web Store. Store upload
is disabled unless `chrome_web_store` is explicitly selected and the repository
variable `CWS_PUBLISH_ENABLED` is exactly `true`.

## Local service access

The default local HTTP endpoint is `127.0.0.1:8766`. Grant site access for each
supported LLM UI and refresh its tab. If the server enforces origin checks, add
the relevant site origins and, where required, the stable
`chrome-extension://<extension-id>` origin.

See [the browser channel guide](../../docs/channels/browser.md) for store
configuration, evidence statuses, and the dry-run command.
