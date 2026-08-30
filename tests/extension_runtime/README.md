# Local extension runtime fixture

Run the bounded, account-free fixture with:

```sh
.venv-mac/bin/python tests/extension_runtime/run_fixture.py
```

The command launches Chrome for Testing with a fresh temporary profile and two
fresh loopback daemon data roots. It uses only synthetic provider turns and
removes all temporary profiles and daemon data after a successful or failed run.
It never opens ChatGPT, uses provider credentials, or accesses the normal browser
profile. The embedding model is forced into offline/cache-only mode. Exit code
`0` is `PASS`, `1` is a functional `FAIL`, and `2` is
`BLOCKED_ENVIRONMENT`. Successful rounds remove their temporary daemon data;
functional failures retain a named temporary evidence directory containing the
daemon log/state, captured daemon stdout, and a `pre-restart-evidence.json`
snapshot. Browser-scenario failures additionally retain
`browser-failure-state.json`, including provider-submit and raw-user-ACK
observations. The child daemon's outbound HTTP(S) is routed to a closed loopback
proxy, and Chrome resolves no non-loopback hosts, so the fixture accepts only
its owned loopback page and daemon traffic.

`BLOCKED_ENVIRONMENT` is reserved for a positively identified precondition such
as a missing/non-executable interpreter or Chrome binary, permission denial, or
failure to reserve an owned loopback port. Once a daemon or browser process has
started, its crash, health/debug timeout, malformed response, or unexpected
runtime exception is a functional `FAIL` and retains the evidence root.
