# macOS installation channels

The zero-cost release path does not require an Apple Developer account. Each
completed release attaches canonical Intel and Apple-silicon command-line
archives to a versioned GitHub Release, covers them with `SHA256SUMS.txt`, and
tests the released bytes from Terminal. They are not a notarized `.pkg`, `.dmg`,
or Homebrew cask.

For `0.0.2`, the live GitHub assets, PyPI entry, tap, and dual-architecture
runner receipt do not yet exist. Their current status is
`BLOCKED_ENVIRONMENT`, not PASS.

## Install after the named channel is available

After the exact version is published to PyPI, and until
`BleedingDev/homebrew-tap` exists and its formula has passed the released
archive workflow on both macOS architectures, the isolated Python-package path
is:

```sh
brew install uv
uv tool install biomem-memory
biomem --version
```

After the separate tap and exact formula version are validated, the preferred
one-command route becomes:

```sh
brew install BleedingDev/tap/biomem
biomem --version
```

Homebrew verifies the architecture-specific SHA-256 before installing the
standalone `biomem` executable. `brew uninstall biomem` removes the Homebrew keg
and command link only. The formula has no uninstall hook and does not delete the
memory database or configuration under the user's home directory.

Before either external channel is available, use the source-checkout quick
start in the repository README. Do not advertise the `uv` or `brew` command as
currently available merely because its metadata generator passed locally.

## Generate an exact tap update

Generate the formula only after the immutable GitHub Release and its checksum
file exist. Replace the example tag with the release being synchronized:

```sh
tag=v0.0.2
gh release download "$tag" \
  --repo BleedingDev/biomem \
  --pattern SHA256SUMS.txt \
  --dir release-metadata
python3 scripts/release_policy.py policy \
  --tag "$tag" \
  --dry-run false \
  --channels none \
  --output release-policy.json
python3 scripts/release/generate_homebrew_formula.py \
  --policy release-policy.json \
  --checksums release-metadata/SHA256SUMS.txt \
  --repository BleedingDev/biomem \
  --output Formula/biomem.rb
```

The generator requires the complete canonical checksum allowlist, embeds exact
`/releases/download/vMAJOR.MINOR.PATCH/` URLs for Intel and Apple silicon, and
rejects missing, extra, duplicate, malformed, or case-colliding entries. It
never points at `latest`.

Review the generated diff, run the macOS channel workflow for the same tag, and
then copy `Formula/biomem.rb` into `BleedingDev/homebrew-tap` in a deliberate
commit. That may be a manual clone/commit/push, or a `workflow_dispatch` job
defined in the tap repository using that repository's own narrowly scoped
`contents: write` token. The core biomem release workflow must not hold a
cross-repository tap token and does not mutate the tap.

## Gatekeeper and test status

Current released-byte evidence is `BLOCKED_ENVIRONMENT`: no immutable `v0.0.2`
GitHub Release assets and no successful linked workflow run covering both the
clean `macos-15` arm64 and `macos-15-intel` jobs exist yet. Local generator,
formula-lint, and contract-test success does not change that status. Record the
channel as `PASS` only after both jobs finish against the same immutable tag and
their run URL is retained as the receipt.

The standalone archives use ad-hoc code signing supplied by the macOS build
toolchain and cryptographic provenance/checksums supplied by GitHub. Ad-hoc
signing is not Developer ID identity and is not Apple notarization. A direct
browser download can therefore show a Gatekeeper warning or be blocked by local
policy. Do not remove quarantine attributes to bypass that policy; install the
Python package instead, or wait for the separately selected paid notarized
channel.

Run **Test zero-cost macOS channels** manually with the immutable release tag.
It downloads the published bytes, checks their checksum, archive allowlist,
machine architecture, ad-hoc signature and version, then exercises a temporary
tap through install, upgrade, formula test, and uninstall while preserving a
memory sentinel. A missing release, unreadable assets, missing Intel runner, or
unavailable manual tap authorization is `BLOCKED_ENVIRONMENT`, never `PASS`.
Gatekeeper/Developer ID/notarization remains unverified unless the independent
signed installer channel provides its own evidence.
