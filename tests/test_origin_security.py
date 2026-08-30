"""Origin-boundary regression tests for the local daemon transports."""

import asyncio
import http.client
import json
import unittest
from types import SimpleNamespace

from memory_module.http_fallback import HTTPFallbackServer
from memory_module.security import SecurityManager
from memory_module.ws_server import BDBMServer


PUBLIC_PAGE_ORIGINS = (
    "https://gemini.google.com",
    "https://notebooklm.google.com",
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://claude.ai",
    "https://www.perplexity.ai",
    "https://perplexity.ai",
    "https://grok.com",
    "https://www.grok.com",
)

EXTENSION_ORIGINS = (
    "chrome-extension://biomem-test",
    "moz-extension://biomem-test",
    "safari-web-extension://biomem-test",
)


class _RecordingHandler:
    def __init__(self):
        self.messages = []

    async def handle(self, message):
        self.messages.append(dict(message))
        return {"status": "success"}


class _Connection:
    def __init__(self):
        self.responses = []

    def respond(self, status, message):
        response = (status, message)
        self.responses.append(response)
        return response


class OriginSecurityTests(unittest.TestCase):
    def setUp(self):
        # Simulate the legacy config still passed by BDBMServer. SecurityManager
        # must narrow it at the shared HTTP/WS boundary.
        self.security = SecurityManager(allowed_origins=PUBLIC_PAGE_ORIGINS)

    def test_public_pages_are_denied(self):
        for origin in PUBLIC_PAGE_ORIGINS:
            with self.subTest(origin=origin):
                self.assertFalse(self.security.is_allowed_origin(origin))

        self.security.add_allowed_origin("https://chatgpt.com")
        self.assertFalse(self.security.is_allowed_origin("https://chatgpt.com"))
        self.assertNotIn("https://chatgpt.com", self.security.allowed_origins)

    def test_native_local_and_extension_origins_remain_allowed(self):
        self.assertTrue(self.security.is_allowed_origin(None))
        for origin in (
            "http://localhost",
            "http://localhost:8766",
            "http://127.0.0.1",
            "http://127.0.0.1:8766",
            *EXTENSION_ORIGINS,
        ):
            with self.subTest(origin=origin):
                self.assertTrue(self.security.is_allowed_origin(origin))

        for malformed in (
            "https://localhost",
            "http://localhost.invalid",
            "http://localhost:8766/path",
            "http://user@localhost:8766",
            "http://127.0.0.1:invalid",
        ):
            with self.subTest(origin=malformed):
                self.assertFalse(self.security.is_allowed_origin(malformed))

    def test_denied_http_preflight_and_post_never_dispatch(self):
        handler = _RecordingHandler()
        server = HTTPFallbackServer(
            handler=handler,
            security=self.security,
            host="127.0.0.1",
            port=0,
        )
        server.start()
        try:
            for method, body, extra_headers in (
                (
                    "OPTIONS",
                    None,
                    {"Access-Control-Request-Method": "POST"},
                ),
                (
                    "POST",
                    json.dumps({"command": "status"}),
                    {"Content-Type": "application/json"},
                ),
            ):
                for origin in PUBLIC_PAGE_ORIGINS:
                    with self.subTest(method=method, origin=origin):
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.bound_port, timeout=2
                        )
                        headers = {"Origin": origin, **extra_headers}
                        connection.request(method, "/api", body=body, headers=headers)
                        response = connection.getresponse()
                        payload = json.loads(response.read().decode("utf-8"))
                        connection.close()
                        self.assertEqual(response.status, 403)
                        self.assertEqual(payload["code"], "FORBIDDEN")
                        self.assertEqual(handler.messages, [])
        finally:
            server.stop()

    def test_websocket_guard_rejects_public_pages(self):
        server = BDBMServer.__new__(BDBMServer)
        server.security = self.security
        server._rejected_origin_last_warned = {}

        for origin in PUBLIC_PAGE_ORIGINS:
            with self.subTest(origin=origin):
                connection = _Connection()
                request = SimpleNamespace(headers={"Origin": origin})
                result = asyncio.run(server._check_origin(connection, request))
                self.assertEqual(result, (403, "Forbidden: disallowed origin."))
                self.assertEqual(connection.responses, [result])

        connection = _Connection()
        request = SimpleNamespace(headers={"Origin": EXTENSION_ORIGINS[0]})
        self.assertIsNone(asyncio.run(server._check_origin(connection, request)))
        self.assertEqual(connection.responses, [])


if __name__ == "__main__":
    unittest.main()
