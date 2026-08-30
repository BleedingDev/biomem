"""Contract tests for the loopback-only biomem HTTP transport."""

import http.client
import json
import unittest

from memory_module.http_fallback import HTTPFallbackServer


ALLOWED_ORIGIN = "chrome-extension://biomem-test"
DISALLOWED_ORIGIN = "https://attacker.invalid"


class _Security:
    def is_allowed_origin(self, origin):
        return origin is None or origin == ALLOWED_ORIGIN


class _Handler:
    def __init__(self):
        self.messages = []

    async def handle(self, message):
        self.messages.append(dict(message))
        command = message.get("command")
        if command == "status":
            return {"status": "success", "state": "ACTIVE"}
        if command == "auth_probe":
            return {
                "status": "error",
                "code": "AUTH_REQUIRED",
                "error": "Missing authorization token.",
            }
        return {"status": "success", "echo": message}


class _UnavailableHandler:
    async def handle(self, message):
        raise RuntimeError("offline")


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.handler = _Handler()
        self.server = HTTPFallbackServer(
            handler=self.handler,
            security=_Security(),
            host="127.0.0.1",
            port=0,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.bound_port, timeout=2
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            data = json.loads(raw.decode("utf-8")) if raw else None
            return response, data
        finally:
            connection.close()

    def test_health_requires_versioned_product_and_readiness_markers(self):
        response, data = self.request(
            "GET", "/api/health", headers={"Origin": ALLOWED_ORIGIN}
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["product"], "biomem")
        self.assertEqual(data["protocol_version"], 1)
        self.assertIsInstance(data["version"], str)
        self.assertTrue(data["version"])
        self.assertIs(data["ready"], True)
        self.assertEqual(data["transport"], "http")
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_legacy_status_path_includes_the_same_contract_markers(self):
        response, data = self.request("GET", "/api/status")

        self.assertEqual(response.status, 200)
        self.assertEqual(data["state"], "ACTIVE")
        self.assertEqual(data["product"], "biomem")
        self.assertEqual(data["protocol_version"], 1)
        self.assertIs(data["ready"], True)

    def test_allowed_preflight_is_explicit_and_cache_safe(self):
        response, data = self.request(
            "OPTIONS",
            "/api",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status, 204)
        self.assertIsNone(data)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertIn("POST", response.getheader("Access-Control-Allow-Methods"))
        self.assertIn("Content-Type", response.getheader("Access-Control-Allow-Headers"))
        self.assertEqual(response.getheader("Vary"), "Origin")

    def test_disallowed_origin_is_rejected_before_dispatch(self):
        before = list(self.handler.messages)
        response, data = self.request(
            "POST",
            "/api",
            body=json.dumps({"command": "echo"}),
            headers={"Content-Type": "application/json", "Origin": DISALLOWED_ORIGIN},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(data["code"], "FORBIDDEN")
        self.assertEqual(self.handler.messages, before)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "null")

    def test_disallowed_preflight_is_rejected(self):
        response, data = self.request(
            "OPTIONS", "/api", headers={"Origin": DISALLOWED_ORIGIN}
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(data["code"], "FORBIDDEN")

    def test_valid_command_preserves_websocket_response_shape(self):
        message = {"command": "echo", "token": "local", "value": 3}
        response, data = self.request(
            "POST",
            "/api",
            body=json.dumps(message),
            headers={"Content-Type": "application/json", "Origin": ALLOWED_ORIGIN},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(data, {"status": "success", "echo": message})
        self.assertEqual(self.handler.messages[-1], message)

    def test_http_header_cannot_bypass_protocol_auth(self):
        response, data = self.request(
            "POST",
            "/api",
            body=json.dumps({"command": "auth_probe"}),
            headers={"Content-Type": "application/json", "X-API-Key": "not-a-token"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(data["code"], "AUTH_REQUIRED")
        self.assertNotIn("token", self.handler.messages[-1])

    def test_malformed_requests_are_rejected_before_dispatch(self):
        cases = (
            ({"Content-Type": "text/plain"}, "{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
            ({"Content-Type": "application/json"}, "{", 400, "INVALID_JSON"),
            ({"Content-Type": "application/json"}, "[]", 400, "INVALID_REQUEST"),
            ({"Content-Type": "application/json"}, "{}", 400, "INVALID_COMMAND"),
        )
        for headers, body, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                before = list(self.handler.messages)
                response, data = self.request("POST", "/api", body=body, headers=headers)
                self.assertEqual(response.status, expected_status)
                self.assertEqual(data["code"], expected_code)
                self.assertEqual(self.handler.messages, before)

    def test_oversized_request_is_rejected_without_reading_body(self):
        response, data = self.request(
            "POST",
            "/api",
            body=b"",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(1024 * 1024 + 1),
            },
        )

        self.assertEqual(response.status, 413)
        self.assertEqual(data["code"], "PAYLOAD_TOO_LARGE")

    def test_unavailable_service_fails_health_and_commands_closed(self):
        unavailable = HTTPFallbackServer(
            handler=_UnavailableHandler(),
            security=_Security(),
            host="127.0.0.1",
            port=0,
        )
        unavailable.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", unavailable.bound_port, timeout=2
            )
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(data["product"], "biomem")
            self.assertEqual(data["protocol_version"], 1)
            self.assertIs(data["ready"], False)

            connection = http.client.HTTPConnection(
                "127.0.0.1", unavailable.bound_port, timeout=2
            )
            connection.request(
                "POST",
                "/api",
                body=json.dumps({"command": "echo"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(data["code"], "SERVICE_UNAVAILABLE")
        finally:
            unavailable.stop()

    def test_unknown_endpoint_is_not_a_health_success(self):
        response, data = self.request("GET", "/not-health")
        self.assertEqual(response.status, 404)
        self.assertEqual(data["code"], "NOT_FOUND")

    def test_non_loopback_bind_is_refused(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            HTTPFallbackServer(
                handler=self.handler,
                security=_Security(),
                host="0.0.0.0",
                port=0,
            )


if __name__ == "__main__":
    unittest.main()
