# Windows distribution channels

The production payload is the one canonical
`biomem-windows-x86_64.zip` from GitHub Releases. It contains
`biomem.exe`, `LICENSE`, `VERSION`, and `VERIFY.txt`. WinGet and Scoop
metadata reference those exact bytes; neither channel rebuilds or repackages
the executable.

For `0.0.2`, the immutable release, public WinGet/Scoop entries, and clean
Windows lifecycle receipts do not yet exist. Their current live status is
`BLOCKED_ENVIRONMENT`, not PASS.

## Zero-cost install paths after the named asset or catalog exists

After the immutable GitHub Release exists, but before catalog acceptance,
download the ZIP and checksum file from that exact version and verify them:

```powershell
$Version = '0.0.2'
$Tag = "v$Version"
$ReleaseDir = Join-Path $PWD "release-$Tag"
gh release download $Tag --repo BleedingDev/biomem `
  --pattern 'biomem-windows-x86_64.zip' `
  --pattern 'SHA256SUMS.txt' `
  --dir $ReleaseDir
$ChecksumLine = Get-Content (Join-Path $ReleaseDir 'SHA256SUMS.txt') |
  Where-Object { $_ -match '^[0-9a-f]{64}  biomem-windows-x86_64\.zip$' }
if (@($ChecksumLine).Count -ne 1) { throw 'Missing or duplicate canonical checksum' }
$Expected = ($ChecksumLine -split '\s+')[0]
$Archive = Join-Path $ReleaseDir 'biomem-windows-x86_64.zip'
if ((Get-FileHash $Archive -Algorithm SHA256).Hash.ToLower() -ne $Expected) {
  throw 'SHA-256 mismatch'
}
$InstallDir = Join-Path $PWD "biomem-$Version"
Expand-Archive -LiteralPath $Archive -DestinationPath $InstallDir
& (Join-Path $InstallDir 'biomem.exe') --version
```

After the exact entries appear in their public catalog/bucket, the supported
package-manager lifecycle becomes:

```powershell
winget install --id BleedingDev.biomem --exact --version 0.0.2 --scope user
biomem --version
winget uninstall --id BleedingDev.biomem --exact --scope user

# Run only after the approved bucket and manifest URL are documented.
scoop bucket add bleedingdev <approved-bucket-git-url>
scoop install biomem
biomem --version
scoop uninstall biomem
```

An upgrade receipt requires two different published versions. After a later
version exists, install the earlier exact version first, then use `winget
upgrade --id BleedingDev.biomem --exact --scope user` or `scoop update biomem`
and verify the new version before uninstalling. `0.0.2` alone cannot PASS an
upgrade lifecycle.

The WinGet package is a ZIP with `NestedInstallerType: portable` and user
scope. The Scoop package creates a `biomem` shim and Scoop's normal uninstall
removes both the version directory and shim. Neither format requires MSI,
MSIX, Microsoft Store enrollment, Authenticode, or administrator scope.
Optional SignPath signing may strengthen publisher identity later, but it is
not a prerequisite for either manifest or the canonical GitHub Release.

Windows security policy still applies. Defender, Smart App Control,
SmartScreen, WDAC, AppLocker, or enterprise controls may prevent execution.
That outcome is `BLOCKED_ENVIRONMENT`; package-manager installation must not
be presented as bypassing it.

## Generate and test exact metadata

Generate both channels only after the immutable release assets and checksum
file exist:

```powershell
$Version = '0.0.2'
$Tag = "v$Version"
$ReleaseDir = Join-Path $PWD "release-$Tag"
$Archive = Join-Path $ReleaseDir 'biomem-windows-x86_64.zip'
$ChecksumLine = Get-Content (Join-Path $ReleaseDir 'SHA256SUMS.txt') |
  Where-Object { $_ -match '^[0-9a-f]{64}  biomem-windows-x86_64\.zip$' }
if (@($ChecksumLine).Count -ne 1) { throw 'Missing or duplicate canonical checksum' }
$Expected = ($ChecksumLine -split '\s+')[0]
python scripts/release_policy.py policy `
  --tag $Tag `
  --dry-run false `
  --channels 'winget,scoop' `
  --output release-policy.json
python scripts/release/generate_windows_channels.py `
  --policy release-policy.json `
  --repository BleedingDev/biomem `
  --archive $Archive `
  --expected-sha256 $Expected `
  --output-dir windows-metadata
winget validate --manifest "windows-metadata\winget\$Version"
scoop install ".\windows-metadata\scoop\biomem.json"
biomem --version
scoop uninstall biomem
```

The generator derives the official versioned GitHub asset URL itself,
requires the stable identifiers from `release/release-policy.json`, checks
the ZIP's exact member and version contract, and refuses a checksum mismatch,
noncanonical filename, mutable URL-shaped repository input, or stale output
directory.

Run the **Windows package-manager lifecycle** workflow with a published tag
and an earlier published tag. On a clean Windows runner it validates the
manifests, proves a bad hash fails, then installs, invokes, upgrades, and
uninstalls both packages in user scope. The WinGet lifecycle uses the public
catalog so it does not need the administrator-only local-manifest setting.
Missing assets, catalog acceptance, security-policy permission, or Scoop
bootstrap is reported as `BLOCKED_ENVIRONMENT`, never a pass.

The local implementation verification on macOS does not constitute that
Windows lifecycle result: `winget`, Scoop, and PowerShell are absent, and a
real upgrade needs two published release tags. Until the Windows workflow has
run with those immutable releases, the runtime lifecycle evidence is
`BLOCKED_ENVIRONMENT`, never `PASS`.

## Optional post-release catalog submission

The workflow uploads validated metadata but deliberately does not open a PR
or write to an external catalog. After an explicit approval, an operator may
use `wingetcreate submit` with a narrowly scoped GitHub token to propose the
generated three-file WinGet directory to `microsoft/winget-pkgs`. For Scoop,
copy `biomem.json` to an approved bucket and open the bucket's normal review
request. A project-owned bucket is also a free immediate option.

Catalog accounts, external repository permissions, identifier availability,
and manual review cannot be proven by this repository. Until the catalog
receipt exists, each selected publication is `BLOCKED_ENVIRONMENT`, never
`PASS` or `published`; unrelated core GitHub publication remains unaffected.
