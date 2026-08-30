import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from memory_module.local_daemon_client import (
    DaemonError,
    LocalDaemonClient,
    configured_port,
)


HEALTH = {
    "status": "success",
    "product": "biomem",
    "version": "0.0.2",
    "protocol_version": 1,
    "transport": "http",
    "ready": True,
}


class LocalDaemonClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    def make_client(self, handler, **kwargs):
        self.client = LocalDaemonClient(
            port=8766, transport=httpx.MockTransport(handler), **kwargs
        )
        return self.client

    async def test_health_requires_every_frozen_marker(self):
        for field, bad_value in (
            ("status", "pending"),
            ("product", "other"),
            ("version", ""),
            ("protocol_version", 2),
            ("transport", "websocket"),
            ("ready", False),
        ):
            with self.subTest(field=field):
                payload = {**HEALTH, field: bad_value}
                client = LocalDaemonClient(
                    port=8766,
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=payload)
                    ),
                )
                try:
                    with self.assertRaises(DaemonError) as raised:
                        await client.health()
                    self.assertEqual(raised.exception.code, "PROTOCOL_MISMATCH")
                finally:
                    await client.aclose()

    async def test_command_uses_loopback_and_one_mutation_attempt(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.host, "127.0.0.1")
            if request.method == "GET":
                return httpx.Response(200, json=HEALTH)
            body = json.loads(request.content)
            self.assertNotIn("token", body)
            self.assertEqual(body["command"], "store_record")
            return httpx.Response(
                503,
                json={
                    "status": "error",
                    "code": "SERVICE_UNAVAILABLE",
                    "error": "private daemon detail",
                },
            )

        client = self.make_client(handler)
        with self.assertRaises(DaemonError) as raised:
            await client.command("store_record", key="k", value="v")
        self.assertEqual(raised.exception.code, "SERVICE_UNAVAILABLE")
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(sum(request.method == "POST" for request in requests), 1)

    async def test_redirect_is_rejected_and_not_followed(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "http://example.com/api"})

        client = self.make_client(handler)
        with self.assertRaises(DaemonError) as raised:
            await client.health()
        self.assertEqual(raised.exception.code, "PROTOCOL_MISMATCH")
        self.assertEqual(len(requests), 1)

    async def test_response_size_is_bounded_before_json_decode(self):
        client = self.make_client(
            lambda request: httpx.Response(
                200,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "1000",
                },
            ),
            max_response_bytes=10,
        )
        with self.assertRaises(DaemonError) as raised:
            await client.health()
        self.assertEqual(raised.exception.code, "PAYLOAD_TOO_LARGE")

    async def test_request_size_is_bounded_before_transport(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=HEALTH)

        client = self.make_client(handler, max_request_bytes=32)
        with self.assertRaises(DaemonError) as raised:
            await client.command("store_record", key="x" * 100, value="v")
        self.assertEqual(raised.exception.code, "PAYLOAD_TOO_LARGE")
        self.assertEqual(calls, 1, "Only the health request may precede local size rejection")

    async def test_timeout_maps_to_sanitized_deadline(self):
        def handler(request):
            raise httpx.ReadTimeout("contains sensitive URL", request=request)

        client = self.make_client(handler)
        with self.assertRaises(DaemonError) as raised:
            await client.health()
        self.assertEqual(raised.exception.code, "DEADLINE_EXCEEDED")
        self.assertNotIn("sensitive", str(raised.exception))

    async def test_missing_daemon_maps_to_service_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("private connection detail", request=request)

        client = self.make_client(handler)
        with self.assertRaises(DaemonError) as raised:
            await client.health()
        self.assertEqual(raised.exception.code, "SERVICE_UNAVAILABLE")
        self.assertNotIn("private", str(raised.exception))

    async def test_cancellation_is_not_swallowed_and_client_closes(self):
        started = asyncio.Event()

        async def handler(_request):
            started.set()
            await asyncio.sleep(30)
            return httpx.Response(200, json=HEALTH)

        client = self.make_client(handler)
        request = asyncio.create_task(client.health())
        await asyncio.wait_for(started.wait(), timeout=1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        await client.aclose()

    async def test_proxy_environment_is_ignored(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=HEALTH)

        with patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://203.0.113.1:9999", "NO_PROXY": ""},
            clear=False,
        ):
            client = self.make_client(handler)
            self.assertFalse(client._client._trust_env)
            self.assertFalse(client._client.follow_redirects)
            await client.health()
        self.assertEqual(seen, ["http://127.0.0.1:8766/api/health"])

    def test_port_configuration_cannot_change_host(self):
        self.assertEqual(configured_port({}), 8766)
        self.assertEqual(configured_port({"BIOMEM_HTTP_PORT": "9001"}), 9001)
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_port({"BIOMEM_HTTP_PORT": value})


class MCPToolSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import mcp  # noqa: F401
        except ModuleNotFoundError:
            raise unittest.SkipTest("official mcp==2.1.1 is not installed")

    def test_exact_six_tool_contract_and_annotations(self):
        from memory_module.mcp_server import create_server

        class FakeClient:
            async def health(self):  # pragma: no cover - schemas only
                raise AssertionError

            async def command(self, command, **arguments):  # pragma: no cover
                raise AssertionError

            async def aclose(self):
                pass

        server = create_server(FakeClient())
        tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "biomem_status",
                "biomem_store",
                "biomem_retrieve",
                "biomem_search",
                "biomem_list",
                "biomem_graph",
            },
        )
        for name, tool in tools.items():
            self.assertFalse(tool.parameters.get("additionalProperties", True), name)
            annotations = tool.annotations
            self.assertFalse(annotations.destructive_hint, name)
            self.assertFalse(annotations.open_world_hint, name)
            if name == "biomem_store":
                self.assertFalse(annotations.read_only_hint)
                self.assertFalse(annotations.idempotent_hint)
            else:
                self.assertTrue(annotations.read_only_hint)

        self.assertEqual(tools["biomem_store"].parameters["properties"]["key"]["maxLength"], 16384)
        self.assertEqual(tools["biomem_store"].parameters["properties"]["value"]["maxLength"], 32768)
        self.assertEqual(tools["biomem_retrieve"].parameters["properties"]["top_k"]["maximum"], 20)
        self.assertEqual(tools["biomem_search"].parameters["properties"]["top_k"]["maximum"], 50)
        self.assertEqual(tools["biomem_list"].parameters["properties"]["limit"]["maximum"], 100)
        self.assertEqual(tools["biomem_graph"].parameters["properties"]["max_nodes"]["maximum"], 250)


class MCPToolMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_map_to_one_client_and_one_process_session(self):
        try:
            from mcp.client import Client
        except ModuleNotFoundError:
            self.skipTest("official mcp==2.1.1 is not installed")
        from memory_module.local_daemon_client import DaemonHealth
        from memory_module.mcp_server import create_server

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.closed = False

            async def health(self):
                return DaemonHealth(
                    "biomem", "0.0.2", 1, True, dict(HEALTH)
                )

            async def command(self, command, **arguments):
                self.calls.append((command, arguments))
                return {"status": "success", "command": command}

            async def aclose(self):
                self.closed = True

        fake = FakeClient()
        server = create_server(fake)
        async with Client(server, mode="legacy") as client:
            await client.call_tool("biomem_status", {})
            await client.call_tool("biomem_store", {"key": "k", "value": "v"})
            await client.call_tool("biomem_retrieve", {"query": "q", "top_k": 7})
            await client.call_tool(
                "biomem_search", {"query": "q", "top_k": 11, "layer": "stm"}
            )
            await client.call_tool("biomem_list", {"layer": "ltm", "limit": 9})
            await client.call_tool(
                "biomem_graph",
                {"layer": "stm", "threshold": 0.7, "max_nodes": 25},
            )

        self.assertTrue(fake.closed)
        self.assertEqual(
            [command for command, _arguments in fake.calls],
            ["store_record", "retrieve", "search", "list_memories", "get_memory_graph"],
        )
        store = fake.calls[0][1]
        retrieve = fake.calls[1][1]
        self.assertEqual(store["provenance"]["source_class"], "mcp")
        self.assertEqual(store["provenance"]["origin"], "local-mcp-stdio")
        self.assertEqual(
            store["provenance"]["session_id"], retrieve["session_id"]
        )
        self.assertRegex(retrieve["session_id"], r"^mcp:[0-9a-f-]{36}$")
        self.assertEqual(fake.calls[2][1], {"query": "q", "top_k": 11, "layer": "stm"})
        self.assertEqual(fake.calls[3][1], {"layer": "ltm", "limit": 9})
        self.assertEqual(
            fake.calls[4][1],
            {"layer": "stm", "threshold": 0.7, "max_nodes": 25},
        )


if __name__ == "__main__":
    unittest.main()
