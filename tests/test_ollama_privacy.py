"""Privacy and local-only contracts for the Ollama integrations."""

import asyncio
import json
import os
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_module.llm_client import (  # noqa: E402
    LLMClient,
    LLMError,
    normalize_ollama_base_url,
)
from memory_module.protocol import CommandHandler  # noqa: E402


class _Memory:
    def __init__(self):
        self.step_calls = 0
        self.store_error = None

    def step(self):
        self.step_calls += 1

    def store(self, _key, _value):
        if self.store_error is not None:
            raise self.store_error

    @staticmethod
    def save():
        return None


class _SessionCache:
    @staticmethod
    def retrieve(_session_id):
        return None


class _Security:
    pass


class _OllamaHandler(BaseHTTPRequestHandler):
    request_path = None
    request_body = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_path = self.path
        type(self).request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(
            {"message": {"role": "assistant", "content": "local answer"}}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class _RedirectHandler(BaseHTTPRequestHandler):
    destination = None

    def do_POST(self):  # noqa: N802
        self.send_response(307)
        self.send_header("Location", type(self).destination)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


class _RedirectReceiver(BaseHTTPRequestHandler):
    request_count = 0

    def do_POST(self):  # noqa: N802
        type(self).request_count += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


class OllamaURLTests(unittest.TestCase):
    def test_only_explicit_http_loopback_origins_are_accepted(self):
        self.assertEqual(
            normalize_ollama_base_url("http://127.0.0.1:11434"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            normalize_ollama_base_url("http://localhost:11434/"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            normalize_ollama_base_url("http://[::1]:11434"),
            "http://[::1]:11434",
        )

        invalid_urls = (
            "https://127.0.0.1:11434",
            "http://example.com:11434",
            "http://127.0.0.2:11434",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://user@127.0.0.1:11434",
            "http://user:secret@127.0.0.1:11434",
            "http://127.0.0.1:11434?query=secret",
            "http://127.0.0.1:11434?",
            "http://127.0.0.1:11434#fragment",
            "http://127.0.0.1:11434#",
            "http://127.0.0.1:11434/api",
            "http://127.0.0.1:11434/api/chat",
            "http://127.0.0.1:11434/%2e%2e/api/chat",
            " http://127.0.0.1:11434",
            "http://127.0.0.1:11434\n",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_ollama_base_url(url)


class OllamaClientPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_performs_a_real_loopback_post(self):
        server = HTTPServer(("127.0.0.1", 0), _OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = LLMClient(
            lambda _model: f"http://localhost:{server.server_port}",
            get_model_fn=lambda _model: "local-model",
        )
        try:
            response = await client.send_prompt("local client prompt", "ollama")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(response, "local answer")
        self.assertEqual(_OllamaHandler.request_path, "/api/chat")
        self.assertEqual(_OllamaHandler.request_body["model"], "local-model")
        self.assertEqual(
            _OllamaHandler.request_body["messages"],
            [{"role": "user", "content": "local client prompt"}],
        )

    async def test_rejected_target_never_constructs_an_http_client(self):
        client = LLMClient(lambda _model: "http://remote.invalid:11434")
        with patch("httpx.AsyncClient") as async_client:
            with self.assertRaises(LLMError) as raised:
                await client.send_prompt("PROMPT_SECRET", "ollama")

        self.assertEqual(raised.exception.code, "INVALID_LOCAL_URL")
        self.assertNotIn("PROMPT_SECRET", str(raised.exception))
        self.assertNotIn("remote.invalid", str(raised.exception))
        async_client.assert_not_called()

    async def test_configuration_callback_error_is_sanitized(self):
        def _raise_secret(_model):
            raise RuntimeError("URL_SECRET")

        client = LLMClient(_raise_secret)
        with self.assertRaises(LLMError) as raised:
            await client.send_prompt("PROMPT_SECRET", "ollama")

        self.assertEqual(raised.exception.code, "INVALID_CONFIG")
        self.assertNotIn("PROMPT_SECRET", str(raised.exception))
        self.assertNotIn("URL_SECRET", str(raised.exception))

    async def test_direct_ollama_sender_revalidates_before_post(self):
        post = AsyncMock()
        http_client = Mock(post=post)
        client = LLMClient(lambda _model: "")

        with self.assertRaises(LLMError) as raised:
            await client._send_to_ollama(
                http_client,
                "PROMPT_SECRET",
                "http://user:URL_SECRET@127.0.0.1:11434",
            )

        self.assertEqual(raised.exception.code, "INVALID_LOCAL_URL")
        self.assertNotIn("PROMPT_SECRET", str(raised.exception))
        self.assertNotIn("URL_SECRET", str(raised.exception))
        post.assert_not_awaited()

    async def test_server_error_body_cannot_escape_into_client_error(self):
        response = Mock(status_code=500)
        response.json.return_value = {
            "error": "PROMPT_SECRET MEMORY_SECRET URL_SECRET"
        }
        http_client = Mock(post=AsyncMock(return_value=response))
        client = LLMClient(lambda _model: "")

        with self.assertRaises(LLMError) as raised:
            await client._send_to_ollama(
                http_client,
                "PROMPT_SECRET",
                "http://127.0.0.1:11434",
            )

        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "API_ERROR")
        self.assertNotIn("PROMPT_SECRET", message)
        self.assertNotIn("MEMORY_SECRET", message)
        self.assertNotIn("URL_SECRET", message)


class ProtocolOllamaPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = _Memory()
        self.handler = CommandHandler(
            self.memory,
            session_cache=_SessionCache(),
            security=_Security(),
        )

    async def test_rejected_protocol_target_never_runs_memory_or_network(self):
        network = AsyncMock()
        self.handler._call_ollama_api = network
        prompt = "PROMPT_SECRET"
        url = "http://user:URL_SECRET@127.0.0.1:11434"

        result = self.handler._handle_ollama_chat(
            {
                "url": url,
                "model_name": "local-model",
                "prompt": prompt,
                "pre_enriched": False,
                "memories": [{"user": "MEMORY_SECRET", "model": "value"}],
            }
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "INVALID_PARAMS")
        self.assertEqual(self.memory.step_calls, 0)
        network.assert_not_awaited()
        serialized = json.dumps(result)
        self.assertNotIn("PROMPT_SECRET", serialized)
        self.assertNotIn("URL_SECRET", serialized)
        self.assertNotIn("MEMORY_SECRET", serialized)

    async def test_legacy_flow_uses_local_api_without_unbound_prompt(self):
        network = AsyncMock(return_value="local answer")
        self.handler._call_ollama_api = network

        result = await self.handler._handle_ollama_chat(
            {
                "url": "http://localhost:11434/",
                "model_name": "local-model",
                "prompt": "original question",
                "pre_enriched": False,
                "memories": [
                    {
                        "user": "remembered question",
                        "model": "remembered answer",
                        "confidence": 0.9,
                        "turn_distance": 1,
                    }
                ],
            }
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["display_text"], "local answer")
        self.assertEqual(self.memory.step_calls, 1)
        args = network.await_args.args
        self.assertEqual(args[0], "http://127.0.0.1:11434")
        self.assertEqual(args[1], "local-model")
        self.assertIn("original question", args[2])
        self.assertIn("remembered answer", args[2])

    async def test_prompt_build_failure_cannot_log_or_return_memory(self):
        secret = "PROMPT_SECRET MEMORY_SECRET"
        self.handler._build_ollama_prompt = Mock(
            side_effect=RuntimeError(secret)
        )
        network = AsyncMock()
        self.handler._call_ollama_api = network

        with self.assertLogs("bdbm.protocol", level="ERROR") as logs:
            result = await self.handler._handle_ollama_chat(
                {
                    "url": "http://127.0.0.1:11434",
                    "model_name": "local-model",
                    "prompt": "PROMPT_SECRET",
                    "pre_enriched": False,
                    "memories": [{"user": "MEMORY_SECRET"}],
                }
            )

        exposed = json.dumps(result) + "\n".join(logs.output)
        self.assertEqual(result["code"], "OLLAMA_ERROR")
        self.assertNotIn("PROMPT_SECRET", exposed)
        self.assertNotIn("MEMORY_SECRET", exposed)
        network.assert_not_awaited()

    async def test_store_failure_cannot_log_or_return_memory(self):
        secret = "PROMPT_SECRET MEMORY_SECRET"
        self.memory.store_error = RuntimeError(secret)
        self.handler._call_ollama_api = AsyncMock(
            return_value=(
                "answer |STPAM| user summary |MIDPAM| model summary |ENDPAM|"
            )
        )

        with self.assertLogs("bdbm.protocol", level="ERROR") as logs:
            result = await self.handler._handle_ollama_chat(
                {
                    "url": "http://127.0.0.1:11434",
                    "model_name": "local-model",
                    "prompt": "PROMPT_SECRET",
                    "pre_enriched": False,
                    "memories": [],
                }
            )

        exposed = json.dumps(result) + "\n".join(logs.output)
        self.assertEqual(result["code"], "OLLAMA_ERROR")
        self.assertNotIn("PROMPT_SECRET", exposed)
        self.assertNotIn("MEMORY_SECRET", exposed)

    async def test_protocol_performs_a_real_loopback_post(self):
        server = HTTPServer(("127.0.0.1", 0), _OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = await self.handler._call_ollama_api(
                f"http://127.0.0.1:{server.server_port}",
                "local-model",
                "local prompt",
                timeout=2,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(response, "local answer")
        self.assertEqual(_OllamaHandler.request_path, "/api/chat")
        self.assertEqual(_OllamaHandler.request_body["model"], "local-model")
        self.assertEqual(
            _OllamaHandler.request_body["messages"],
            [{"role": "user", "content": "local prompt"}],
        )

    async def test_protocol_does_not_follow_redirects(self):
        receiver = HTTPServer(("127.0.0.1", 0), _RedirectReceiver)
        receiver_thread = threading.Thread(
            target=receiver.serve_forever, daemon=True
        )
        receiver_thread.start()
        redirector = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        _RedirectHandler.destination = (
            f"http://127.0.0.1:{receiver.server_port}/api/chat"
        )
        _RedirectReceiver.request_count = 0
        redirect_thread = threading.Thread(
            target=redirector.serve_forever, daemon=True
        )
        redirect_thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                await self.handler._call_ollama_api(
                    f"http://127.0.0.1:{redirector.server_port}",
                    "local-model",
                    "must not be redirected",
                    timeout=2,
                )
            raised.exception.close()
        finally:
            redirector.shutdown()
            redirector.server_close()
            redirect_thread.join(timeout=2)
            receiver.shutdown()
            receiver.server_close()
            receiver_thread.join(timeout=2)

        self.assertEqual(_RedirectReceiver.request_count, 0)

    async def test_protocol_exception_text_is_not_logged_or_returned(self):
        secret = "PROMPT_SECRET MEMORY_SECRET URL_SECRET"
        self.handler._call_ollama_api = AsyncMock(
            side_effect=RuntimeError(secret)
        )

        with self.assertLogs("bdbm.protocol", level="ERROR") as logs:
            result = await self.handler._handle_ollama_chat(
                {
                    "url": "http://127.0.0.1:11434",
                    "model_name": "local-model",
                    "prompt": "PROMPT_SECRET",
                }
            )

        exposed = json.dumps(result) + "\n".join(logs.output)
        self.assertEqual(result["code"], "OLLAMA_ERROR")
        self.assertNotIn("PROMPT_SECRET", exposed)
        self.assertNotIn("MEMORY_SECRET", exposed)
        self.assertNotIn("URL_SECRET", exposed)


if __name__ == "__main__":
    unittest.main()
