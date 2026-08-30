# biomem for Firefox

This source tree has the stable Firefox add-on ID
`biomem@bleedingdev.github.io`. The build produces
`firefox-biomem-unsigned.xpi`, which is an AMO signing input and a temporary
development package only. It is deliberately classified `ci_only` and must
never be attached to a normal public release.

## Local development

Run `scripts/build_extensions.sh --no-crx --no-safari`, open
`about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on**, and
select the unsigned XPI. Temporary loading is not evidence of AMO signing,
store availability, automatic updates, or end-to-end memory behavior.

Normal users receive either:

- an AMO-listed version installed from addons.mozilla.org; or
- an AMO-signed **unlisted** XPI that may be hosted on GitHub.

The browser publication workflow signs the exact tested unsigned input through
Mozilla's free AMO service. It accepts a public artifact only after the returned
XPI has the exact add-on ID and version, a signature chain anchored to the
pinned Mozilla production root, the expected code-signing leaf identity, a
valid signature-to-manifest-to-entry digest chain, and payload bytes identical
to the tested input. Missing AMO credentials, review, permissions, CAPTCHA, or
other manual steps are `blocked_environment`, never PASS.

See [the browser channel guide](../../docs/channels/browser.md) for setup and
dry-run behavior.
