# biomem local HTTP transport v1

The biomem daemon exposes a loopback-only HTTP transport for browser-extension
background contexts and native local clients. It is not a network service: the
server refuses non-loopback bind addresses and non-loopback peers.

## Endpoints

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/api/health` | Product identity and readiness probe | `200` JSON with `product: "biomem"`, `protocol_version: 1`, and `ready: true` |
| `GET` | `/api/status` | Backward-compatible detailed daemon status | Same versioned markers plus module status |
| `POST` | `/api` | Execute one command using the WebSocket command schema | `200` with the command handler's JSON response |
| `OPTIONS` | Any endpoint above | CORS preflight | `204` for an allowed origin |

Health is valid only when all of these checks pass: HTTP status is `200`, the
content type is JSON, `status` is `success`, `product` is `biomem`,
`protocol_version` is `1`, `version` is a non-empty string, and `ready` is
`true`. A resolved fetch, opaque response, 404, or error-shaped JSON is not a
successful health check.

## Command and error contract

`POST /api` requires `Content-Type: application/json`, a JSON object body, and
a non-empty string `command`. The maximum body size is 1 MiB. Transport
validation failures use HTTP `4xx` with `{status: "error", code, error}`. A
missing/unready command handler uses HTTP `503` and `SERVICE_UNAVAILABLE`.

Once a valid command reaches `CommandHandler`, its response is returned without
rewriting it. This preserves parity with the WebSocket protocol: command-level
errors such as `AUTH_REQUIRED`, `AUTH_FAILED`, or `UNKNOWN_COMMAND` remain JSON
responses with HTTP `200`. Tokens stay in the command body under `token`; the
HTTP layer does not promote `X-API-Key` into a token or bypass protocol auth.

## Origin and privacy policy

Requests without an `Origin` header are accepted only from a loopback peer.
Browser origins are checked by `SecurityManager.is_allowed_origin`. Extension
origins (`chrome-extension://`, `moz-extension://`, and
`safari-web-extension://`) and explicit local HTTP origins are echoed in
`Access-Control-Allow-Origin`. Public web-page origins, including supported LLM
sites, are rejected because content scripts communicate
through their extension background context. Rejected origins receive `403
FORBIDDEN` and do not reach the command handler. Responses use `Cache-Control:
no-store` and `Vary: Origin`. The transport performs no relay, telemetry, or
network egress.

Browser content scripts should not connect to this endpoint directly. They send
commands to their extension background/service-worker context, which owns the
loopback fetch and validates the complete health contract above.

Native adapters must connect to an explicit loopback URL, ignore environment
proxy configuration, use finite connect/read timeouts, and reject redirects.
The server does not emit redirects. A native loopback client may omit `Origin`;
that does not relax the loopback-peer check.

This origin boundary is not process authentication: another local process can
omit `Origin`, and any installed extension can present an extension-scheme
origin. Command-level authentication remains responsible for authorizing
operations.
