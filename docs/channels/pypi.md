# PyPI, uv, and pipx

PyPI is the zero-cost cross-platform package channel for the Python build of
biomem. The release workflow publishes the exact wheel and source archive that
were already built, installed, and smoke-tested by the canonical GitHub Release
workflow. It never rebuilds either file.

For `0.0.2`, live project ownership, Trusted Publisher/OIDC, upload,
attestation, exact-version install, and uninstall evidence do not yet exist.
The current PyPI channel status is `BLOCKED_ENVIRONMENT`, not PASS.

## Install after `0.0.2` is present on PyPI

Use [uv](https://docs.astral.sh/uv/guides/tools/) as the primary installer:

```console
uv tool install biomem-memory
biomem --help
```

The installed distribution also provides `biomem-server` and `biomem-mcp`.
Upgrade or remove the same isolated tool environment with:

```console
uv tool upgrade biomem-memory
uv tool uninstall biomem-memory
```

[pipx](https://pipx.pypa.io/) is the fallback when uv is unavailable:

```console
pipx install biomem-memory
pipx upgrade biomem-memory
pipx uninstall biomem-memory
```

Do not use a global `pip install` as the normal installation path. Both uv and
pipx keep biomem and its dependency tree out of the system Python environment.
Until the PyPI JSON API exposes the exact version and digests, use the
source-checkout quick start instead of presenting these commands as currently
available.

## Download and disk expectations

The first operation that needs embeddings downloads
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` into the local
Hugging Face cache. A measured macOS development installation used about
458 MB for that model cache and 1.3 GB for the complete Python environment.
Torch wheels and caches vary by OS, CPU/GPU support, and package-manager cache,
so allow roughly 2 GB of free disk space. The model is reused from the local
cache on later runs; biomem does not upload memory data to obtain embeddings.

`biomem --help` is the release smoke command because it is non-destructive: it
does not create or modify a memory state file.

## One-time Trusted Publisher setup

No PyPI API-token secret is used. A PyPI project owner performs one account
setup step for `biomem-memory`:

1. In the PyPI project publishing settings, add a GitHub Trusted Publisher.
2. Set the GitHub owner to `BleedingDev`, repository to `biomem`, workflow to
   `publish-pypi.yml`, and environment to `pypi`.
3. In the GitHub repository, create the `pypi` environment. Optional required
   reviewers can protect production publication without adding a secret.

For the first release of a project name, PyPI's pending-publisher flow can make
the same association before the project exists. If the project, Trusted
Publisher, GitHub environment approval, or OIDC permission is unavailable, the
workflow reports `BLOCKED_ENVIRONMENT`; that condition is never a pass.

## Publication and verification contract

The top-level `Publish canonical Python distributions to PyPI` workflow runs
from the canonical publisher's `canonical_release_published` repository
dispatch, from a separately generated GitHub `release: published` event, or
manually against an existing published tag. The explicit repository dispatch is
required for the normal automated path because events caused by a repository's
`GITHUB_TOKEN` do not recursively start most other workflows. Its payload must
contain exactly the release tag and 40-character source SHA; both are checked
against the tag checkout and release manifest. Publication occurs only when the
release manifest explicitly selected the `pypi` channel.

Before requesting an OIDC credential, it verifies all of the following:

- the GitHub Release is published and the checked-out tag resolves to the
  source SHA in `release-manifest.json`;
- exactly one canonical wheel and one canonical source archive are present;
- their names, sizes, and SHA-256 digests match both `release-manifest.json`
  and `SHA256SUMS.txt`;
- GitHub's build-provenance attestations verify separately for each exact byte
  sequence and are bound to this repository, `release-publish.yml`, and the
  release source digest; and
- `twine check` accepts both distributions.

The separate publishing job has only `id-token: write`, calls the pinned PyPA
Trusted Publishing action once, and sends no username, password, or stored
token. PyPI publish attestations are enabled by default. A dry run performs the
full read-only preflight and cannot enter that job; it therefore produces no
external OIDC-backed PyPI or GitHub attestation.

After publication, the workflow requires the PyPI JSON API to report the same
two filenames and SHA-256 digests. For each file it cryptographically verifies
the Integrity API provenance with `pypi-attestations` and requires the publisher
identity to be `BleedingDev/biomem`, workflow `publish-pypi.yml`, environment
`pypi`. It then installs the exact version with `uv tool install`, runs
`biomem --help`, and uninstalls it. A retry skips the upload only when PyPI
already holds that exact two-file set with verified provenance; a digest, name,
publisher, workflow, or attestation mismatch fails closed.
