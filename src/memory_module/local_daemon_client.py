"""Bounded, loopback-only client for the biomem local HTTP transport.

This module deliberately knows nothing about ``TextMemory`` or the on-disk
container format.  Native adapters use it to address the already-running
daemon, which keeps every local client on one authoritative memory state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx


DEFAULT_HTTP_PORT = 8766
HTTP_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 30.0

_EXPOSED_DAEMON_CODES = frozenset({
    "DEADLINE_EXCEEDED",
    "INTERNAL_ERROR",
    "MODULE_INACTIVE",
    "PAYLOAD_TOO_LARGE",
    "PROTOCOL_MISMATCH",
    "SERVICE_UNAVAILABLE",
})


class DaemonError(RuntimeError):
    """A sanitized failure safe to expose through a local adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _validated_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("The biomem HTTP port must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("The biomem HTTP port must be between 1 and 65535.")
    return port


def configured_port(environ: Optional[Mapping[str, str]] = None) -> int:
    """Read only the port from configuration; the host is never configurable."""

    source = os.environ if environ is None else environ
    return _validated_port(source.get("BIOMEM_HTTP_PORT", DEFAULT_HTTP_PORT))


@dataclass(frozen=True)
class DaemonHealth:
    product: str
    version: str
    protocol_version: int
    ready: bool
    payload: dict[str, Any]


class LocalDaemonClient:
    """Strict client for ``http://127.0.0.1:<port>``.

    Environment proxy configuration and redirects are disabled.  Requests are
    attempted once; in particular, mutations are never retried.
    """

    def __init__(
        self,
        *,
        port: Optional[int] = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.port = _validated_port(configured_port() if port is None else port)
        self.max_request_bytes = int(max_request_bytes)
        self.max_response_bytes = int(max_response_bytes)
        if self.max_request_bytes <= 0 or self.max_response_bytes <= 0:
            raise ValueError("Request and response bounds must be positive.")
        timeout = httpx.Timeout(
            connect=float(connect_timeout),
            read=float(read_timeout),
            write=float(connect_timeout),
            pool=float(connect_timeout),
        )
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "LocalDaemonClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> DaemonHealth:
        payload = await self._request("GET", "/api/health")
        valid = (
            payload.get("status") == "success"
            and payload.get("product") == "biomem"
            and payload.get("protocol_version") == HTTP_PROTOCOL_VERSION
            and isinstance(payload.get("version"), str)
            and bool(payload.get("version"))
            and payload.get("ready") is True
            and payload.get("transport") == "http"
        )
        if not valid:
            raise DaemonError(
                "PROTOCOL_MISMATCH",
                "The local service did not identify itself as a ready biomem HTTP v1 daemon.",
            )
        return DaemonHealth(
            product="biomem",
            version=payload["version"],
            protocol_version=HTTP_PROTOCOL_VERSION,
            ready=True,
            payload=payload,
        )

    async def command(self, command: str, **arguments: Any) -> dict[str, Any]:
        """Validate daemon identity, then execute exactly one command attempt."""

        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        await self.health()
        body = {"command": command, **arguments}
        return await self._request("POST", "/api", body)

    async def _request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        content: Optional[bytes] = None
        headers: dict[str, str] = {}
        if body is not None:
            content = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(content) > self.max_request_bytes:
                raise DaemonError(
                    "PAYLOAD_TOO_LARGE", "The local request exceeds the configured size limit."
                )
            headers["Content-Type"] = "application/json"

        request = self._client.build_request(
            method, path, content=content, headers=headers
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise DaemonError(
                "DEADLINE_EXCEEDED", "The local biomem daemon did not respond in time."
            ) from exc
        except httpx.RequestError as exc:
            raise DaemonError(
                "SERVICE_UNAVAILABLE", "The local biomem daemon is unavailable."
            ) from exc

        try:
            if 300 <= response.status_code < 400:
                raise DaemonError(
                    "PROTOCOL_MISMATCH", "The local biomem daemon returned an unexpected redirect."
                )
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > self.max_response_bytes:
                        raise DaemonError(
                            "PAYLOAD_TOO_LARGE",
                            "The local daemon response exceeds the configured size limit.",
                        )
                except ValueError:
                    raise DaemonError(
                        "PROTOCOL_MISMATCH", "The local daemon returned an invalid content length."
                    )

            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > self.max_response_bytes:
                    raise DaemonError(
                        "PAYLOAD_TOO_LARGE",
                        "The local daemon response exceeds the configured size limit.",
                    )
        except httpx.TimeoutException as exc:
            raise DaemonError(
                "DEADLINE_EXCEEDED", "The local biomem daemon did not respond in time."
            ) from exc
        except httpx.RequestError as exc:
            raise DaemonError(
                "SERVICE_UNAVAILABLE", "The local biomem daemon is unavailable."
            ) from exc
        finally:
            await response.aclose()

        content_type = response.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise DaemonError(
                "PROTOCOL_MISMATCH", "The local biomem daemon returned a non-JSON response."
            )
        try:
            payload = json.loads(chunks.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DaemonError(
                "PROTOCOL_MISMATCH", "The local biomem daemon returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise DaemonError(
                "PROTOCOL_MISMATCH", "The local biomem daemon returned a non-object response."
            )

        if response.status_code >= 400 or payload.get("status") == "error":
            self._raise_daemon_error(response.status_code, payload)
        if response.status_code != 200 or payload.get("status") != "success":
            raise DaemonError(
                "PROTOCOL_MISMATCH", "The local biomem daemon returned an invalid success response."
            )
        return payload

    @staticmethod
    def _raise_daemon_error(status_code: int, payload: dict[str, Any]) -> None:
        raw_code = payload.get("code")
        code = raw_code if isinstance(raw_code, str) else ""
        if code not in _EXPOSED_DAEMON_CODES:
            if status_code == 413:
                code = "PAYLOAD_TOO_LARGE"
            elif status_code == 503:
                code = "SERVICE_UNAVAILABLE"
            elif status_code in (401, 403):
                code = "SERVICE_UNAVAILABLE"
            else:
                code = "INTERNAL_ERROR"
        safe_messages = {
            "DEADLINE_EXCEEDED": "The local biomem daemon did not respond in time.",
            "PROTOCOL_MISMATCH": "The local biomem daemon returned an incompatible response.",
            "MODULE_INACTIVE": "The local biomem module is inactive.",
            "PAYLOAD_TOO_LARGE": "The local request or response exceeds the allowed size.",
            "SERVICE_UNAVAILABLE": "The local biomem daemon is unavailable.",
            "INTERNAL_ERROR": "The local biomem daemon could not complete the request.",
        }
        raise DaemonError(code, safe_messages[code])
