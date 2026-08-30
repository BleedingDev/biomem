# WinGet metadata

Versioned multi-file manifests are generated from the canonical Windows ZIP
after a GitHub Release exists. No placeholder checksum is committed here.

```powershell
python scripts/release/generate_windows_channels.py `
  --policy release-policy.json `
  --repository OWNER/REPOSITORY `
  --archive biomem-windows-x86_64.zip `
  --expected-sha256 <SHA256SUMS.txt value> `
  --output-dir windows-metadata
winget validate --manifest windows-metadata/winget/<VERSION>
```

Catalog submission is an explicitly authorized post-release operation. A
missing catalog account, permission, or manual review is
`BLOCKED_ENVIRONMENT`, never a successful publication.
