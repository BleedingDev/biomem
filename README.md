# biomem — portable cognitive memory for LLM conversations

> **Alpha:** the first release line is `0.0.2`. Interfaces, packaging, and
> installation channels may change before `1.0.0`.

A **local-first memory engine** for LLM conversations: associative,
biologically-inspired persistent memory built around an STM/LTM two-layer
architecture, a 3D latent "cognitive terrain", RBF kernel attention, and
sleep-style consolidation.

The scientific foundation for this architecture is described by Michal Seidl
and OpenTechLab in [Persistent Memory for Decoder-Only Transformers](https://doi.org/10.5281/zenodo.18198327)
and [Implementation of Persistent Latent Memory for Decoder Transformers](https://doi.org/10.5281/zenodo.18267378).
BioMem applies those published concepts to a local text-memory service and
browser integrations; its implementation-specific coefficients and behavior
are documented and tested in this repository.

- **Pure Python 3.10+** (no Cython) — runs on **macOS (arm64/x86_64), Linux
  (arm64/x86_64) and Windows (x64)**.
- **Complete feature set, always active** — no lockouts or feature-mode switch,
  with telemetry disabled by default.
- **Browser plugins included** (Chromium / Firefox / Safari) for local memory
  capture without a biomem provider login.
- **MIT licensed** — memory databases remain on your machine. The browser
  extension communicates with the local biomem service and has no separate
  biomem/provider login. Recalled context reaches the already-open LLM web app
  only as part of the user-initiated prompt; the memory database is not synced.

## Algorithm summary

- Embedding: `paraphrase-multilingual-MiniLM-L12-v2`, 384-d, normalized.
- LTM: 4096 centers × 64-d keys, 128-d values, 4-d emotion (σ-read 0.5,
  σ-write 0.15, leak ≈ 2.66e-5, threshold 0.78).
- STM: 512 centers × 16-d keys (σ-read 0.4 / σ-write 0.2, leak 3.5e-3,
  threshold 0.5).
- Hybrid metric: cosine + Minkowski(p=0.5), weighted 0.7/0.3, candidate
  pre-selection 64.
- Terrain: 48³ latent grid + 4 emotion channels; Gaussian splat (σ=0.1),
  diffusion (6-neighbor Laplacian), leak/homeostasis, STM→LTM pour.
- Sleep-style consolidation: fatigue trigger, κ=0.8, top-128 STM, merge τ=0.95.
- Conversation protocol: `STPAM…MIDPAM…ENDPAM` memory-summary prompts with
  `|TITLE|` thread-title convention.

## Install and run

Download the matching standalone archive and browser extension from
[GitHub Releases](https://github.com/BleedingDev/biomem/releases). The
[GitHub Release installation guide](docs/install-from-github-releases.md)
covers artifact selection, checksum and provenance verification, the local
service, and manual Chrome, Edge, and Firefox installation.

The Python package is another supported path after the exact version appears
on PyPI:

```bash
uv tool install biomem-memory
biomem-server
```

For development or while an external channel is unavailable, run from a
source checkout:

```bash
cd src
python -m venv .venv && . .venv/bin/activate
pip install -e ".[gui]"
biomem-server                 # desktop dashboard + WS server 127.0.0.1:8765
biomem-server --no-gui        # headless
python ../tests/test_smoke.py
```

The low-cost distribution baseline is GitHub Releases plus the source tree,
PyPI for `uv`/`pipx`, a Homebrew formula, and WinGet/Scoop metadata. These
channels do not require paid Apple Developer or Windows Authenticode
certificates. Commands for package-manager installation become supported only
after the corresponding immutable release, package-index entry, tap, or
catalog receipt exists. Missing accounts, approvals, permissions, signing, or
manual enablement are `BLOCKED_ENVIRONMENT`, never PASS.

Paid Apple notarization and public Safari distribution, and optional Windows
publisher signing, are separate future UX improvements rather than `1.0.0`
release gates. See [the release-channel matrix](docs/release-channel-matrix.md)
for the current boundary of each channel.

## Building the browser plugins

```bash
scripts/build_extensions.sh --validate-only       # source/manifest checks only
scripts/build_extensions.sh --no-safari          # package Chrome and Firefox
scripts/build_extensions.sh --safari-mode=development  # local ad-hoc Safari build
```

Outputs to `dist/`:

| Artifact | Use |
|---|---|
| `chrome-biomem.zip` | Canonical public GitHub Release asset and exact Chrome Web Store upload input; unzip it for a developer-mode install |
| `chrome-biomem.crx` | Development/managed testing only; an ephemeral or self-signed CRX is never a public release asset |
| `firefox-biomem-unsigned.xpi` | CI-only AMO signing input; load temporarily from `about:debugging` for development |
| `firefox-biomem-<version>-amo-signed.xpi` | Public only when AMO signing and the exact signed-artifact handoff have both been verified |
| release-mode Safari ZIP | Optional paid Apple signing/notarization input; not public until that boundary succeeds |
| `safari-biomem-macos-development-adhoc.zip` | Local Safari development build; ad-hoc signed and explicitly non-distributable |

`scripts/crx3_sign.py` packs CRX3 files from a zip + RSA key (stdlib + `openssl`
only); the Firefox xpi deliberately excludes `META-INF/` stale signatures.
Safari uses the checked-in `safari-xcode/` project and canonical `safari-src`
resources. Development packaging does not establish Apple distribution trust;
see `extensions/safari-xcode/BDBM Memory Plugin/BUILDING.md` for development
instructions and the separate optional signing/notarization path.

## Structure

```
src/memory_module/   the full engine (pure Python) + assets
tests/               portable smoke + full-product access tests
extensions/          browser plugin source (chrome-src, firefox-src, safari-src, safari-xcode)
.github/workflows/   cross-platform CI and release-channel workflows
```

See `src/README.md` for the full architecture, algorithm details, and local
benchmarks.
