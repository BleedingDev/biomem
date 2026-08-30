import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _DaemonHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, _format, *_args):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.requests.append(("GET", self.path, None))
        self._json({
            "status": "success",
            "product": "biomem",
            "version": "0.0.2",
            "protocol_version": 1,
            "transport": "http",
            "ready": True,
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.requests.append(("POST", self.path, payload))
        self._json({"status": "success", "echo": payload["command"]})


class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from mcp.client import Client  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("official mcp==2.1.1 is not installed")
        _DaemonHandler.requests = []
        self.daemon = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
        self.thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        if hasattr(self, "daemon"):
            self.daemon.shutdown()
            self.daemon.server_close()
            self.thread.join(timeout=2)

    async def test_official_client_negotiates_lists_and_calls_over_stdio(self):
        from mcp import StdioServerParameters
        from mcp.client import Client

        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["BIOMEM_HTTP_PORT"] = str(self.daemon.server_address[1])
        existing = env.get("PYTHONPATH")
        paths = [str(project_root / "src")]
        if existing:
            paths.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(paths)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "memory_module.mcp_server"],
            env=env,
        )
        async with Client(params, mode="legacy", read_timeout_seconds=10) as client:
            listed = await client.list_tools()
            self.assertEqual(len(listed.tools), 6)
            rejected = await client.call_tool("biomem_status", {"unknown": True})
            self.assertTrue(rejected.is_error)
            result = await client.call_tool("biomem_status", {})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["product"], "biomem")

        self.assertEqual(_DaemonHandler.requests, [("GET", "/api/health", None)])


if __name__ == "__main__":
    unittest.main()
