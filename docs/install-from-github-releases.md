# Install from GitHub Releases

GitHub Releases are the canonical direct-download channel for biomem. A
release remains usable even when an optional browser store, package catalog,
or paid signing channel is unavailable.

The `0.0.x` line is alpha software. Back up important memory data before an
upgrade and read the release notes before replacing an existing installation.

## 1. Choose the platform archive

Open [BleedingDev/biomem Releases](https://github.com/BleedingDev/biomem/releases),
select the required version, and download exactly one CLI archive:

| Platform | Asset |
| --- | --- |
| macOS on Apple silicon | `biomem-macos-arm64.tar.gz` |
| macOS on Intel | `biomem-macos-x86_64.tar.gz` |
| Linux x86-64 | `biomem-linux-x86_64.tar.gz` |
| Linux ARM64 | `biomem-linux-aarch64.tar.gz` |
| Windows x86-64 | `biomem-windows-x86_64.zip` |

Also download `SHA256SUMS.txt`. The release contains a Python wheel and source
archive for Python-based installation, plus the browser artifacts described
below.

The same files can be downloaded with GitHub CLI. Replace the tag and asset
name when installing a later version:

```bash
tag=v0.0.2
asset=biomem-macos-arm64.tar.gz
gh release download "$tag" \
  --repo BleedingDev/biomem \
  --pattern "$asset" \
  --pattern SHA256SUMS.txt
```

## 2. Verify the download

Verify both the immutable release asset and its GitHub Actions provenance:

```bash
gh release verify-asset v0.0.2 biomem-macos-arm64.tar.gz \
  --repo BleedingDev/biomem
gh attestation verify biomem-macos-arm64.tar.gz \
  --repo BleedingDev/biomem
```

Then compare the file's SHA-256 with its exact entry in `SHA256SUMS.txt`. On
macOS:

```bash
grep '  biomem-macos-arm64.tar.gz$' SHA256SUMS.txt | shasum -a 256 -c -
```

On Linux, replace `shasum -a 256` with `sha256sum`. On Windows, compare
`Get-FileHash -Algorithm SHA256 <asset>` with the corresponding checksum entry.
Do not install a file if any verification fails.

## 3. Start the local biomem service

On macOS or Linux:

```bash
tar -xzf biomem-macos-arm64.tar.gz
./biomem --version
./biomem
```

On Windows PowerShell:

```powershell
Expand-Archive .\biomem-windows-x86_64.zip -DestinationPath .\biomem
.\biomem\biomem.exe --version
.\biomem\biomem.exe
```

Running the executable starts the local dashboard and browser-extension
service. Memory and embeddings remain on the machine. There is no biomem
account or provider login.

The zero-cost macOS archive is not Developer ID notarized. Gatekeeper or local
organization policy may therefore block a direct browser download. Do not
bypass the machine's security policy; use the Python/source installation path
or wait for the separately verified notarized channel.

## 4. Install a browser extension

Start biomem before checking extension connectivity.

### Chrome and Chromium

1. Download `chrome-biomem.zip` from the same release and verify it.
2. Extract it into a permanent directory; do not delete that directory while
   the extension is installed.
3. Open `chrome://extensions`.
4. Enable **Developer mode**.
5. Select **Load unpacked** and choose the extracted directory.
6. Open the extension popup and confirm that it reports **Connected locally**.

### Microsoft Edge

Use the same `chrome-biomem.zip`, but open `edge://extensions`, enable
**Developer mode**, and select **Load unpacked**.

### Firefox

Install only `firefox-biomem-<version>-amo-signed.xpi`. This asset appears only
when Mozilla signing and the release handoff both succeed.

1. Open `about:addons`.
2. Open the gear menu and select **Install Add-on From File**.
3. Choose the signed XPI and approve its permissions.
4. Open the extension popup and confirm **Connected locally**.

Never install `firefox-biomem-unsigned.xpi`; it is a CI signing input and is
intentionally excluded from public releases.

### Safari

GitHub Releases do not publish an ad-hoc Safari development build. Normal
Safari distribution requires the separately verified Apple signing,
notarization, and App Store boundary. Use the repository's development build
instructions only for local development.

## Updates and removal

Developer-mode Chrome/Edge installations and manually downloaded CLI archives
do not update themselves. Follow the repository's Releases page, verify the
new version, stop biomem, and replace the extracted files. Store-installed
extensions follow their store's update process.

Removing the executable or extension does not delete the local memory database.
Delete user data only through an explicit backup/reset workflow.
