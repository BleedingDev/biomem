# Scoop metadata

`generate_windows_channels.py` emits `scoop/biomem.json` beside the WinGet
manifests. It references the same immutable GitHub Release ZIP and exact
SHA-256, and Scoop owns the command shim and standard uninstall cleanup.

The generated manifest can be installed directly for verification:

```powershell
scoop install ./windows-metadata/scoop/biomem.json
biomem --version
scoop uninstall biomem
```

Publishing to an external bucket requires explicit authorization. Missing
bucket credentials or review is `BLOCKED_ENVIRONMENT`, never `PASS`.
