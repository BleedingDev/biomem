# Extension transport contract tests

This suite turns E2E-002 and E2E-003 into one cross-browser contract for the
Chromium, Firefox, and Safari source trees.

The contract is deliberately stricter than the broken implementation:

- content scripts, options, and popup pages send `localCommand` messages;
- only the extension background performs loopback HTTP requests;
- `GET /api/health` is healthy only when a readable HTTP 200 JSON response has
  `product: "biomem"`, `status: "success"`, numeric `protocol_version: 1`, a
  non-empty `version`, `ready: true`, and `transport: "http"`;
- opaque responses, wrong products, non-ready statuses, HTTP failures, and
  network failures are never reported as healthy;
- normal commands are sent as JSON with `POST /api` and return parsed JSON in
  the background response's `data` field;
- service-unavailable failures use the stable `SERVICE_UNAVAILABLE` code;
- transport-critical extension files remain identical across browser copies;
- the manifest grants loopback HTTP access, but no longer grants unused direct
  WebSocket access.

Run from the repository root:

```sh
python3 -m unittest discover -s tests/extension_contract -v
```

The JavaScript behavior checks use Node.js and are skipped if Node is not
installed. The manifest and source-contract checks use only Python's standard
library. Until the implementation fixes land, failures are expected and name
the violated browser/scenario contract rather than incidental UI behavior.
