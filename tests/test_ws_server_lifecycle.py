"""Regression tests for WebSocket server signal registration and shutdown."""

from __future__ import annotations

import asyncio
import queue
import signal
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_module import ws_server


class _FallbackStub:
    def __init__(self):
        self.is_running = False

    def update_event_loop(self, _loop):
        pass

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False


class _ServeContext:
    def __init__(self, *, cancel_on_enter: bool):
        self.cancel_on_enter = cancel_on_enter

    async def __aenter__(self):
        if self.cancel_on_enter:
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel)
        return object()

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


def _server():
    """Build only the public lifecycle state needed by BDBMServer.start()."""
    server = ws_server.BDBMServer.__new__(ws_server.BDBMServer)
    server.host = "127.0.0.1"
    server.port = 8765
    server._data_dir = Path("/tmp/biomem-test")
    server._state_file = "/tmp/biomem-test/memory.bdbm"
    server.memory = SimpleNamespace(save=lambda: None)
    server.security = SimpleNamespace(allowed_origins=[])
    server._http_fallback = _FallbackStub()
    server._server = None
    server._running = False
    server._active_connections = set()
    server._connection_activity = {}
    server._cleanup_task = None

    async def cleanup():
        return None

    server._periodic_cleanup = cleanup
    return server


class WebSocketServerLifecycleTests(unittest.TestCase):
    def test_cancelled_start_reaps_cleanup_task_and_repeated_stop_is_safe(self):
        async def scenario():
            server = _server()
            cleanup_started = asyncio.Event()
            cleanup_finalized = asyncio.Event()

            async def cleanup():
                cleanup_started.set()
                try:
                    await asyncio.Future()
                finally:
                    cleanup_finalized.set()

            server._periodic_cleanup = cleanup
            loop = asyncio.get_running_loop()

            with (
                patch.object(loop, "add_signal_handler", lambda *_args: None),
                patch.object(ws_server, "HAS_WEBSOCKETS", True),
                patch.object(ws_server.sys, "platform", "linux"),
                patch.object(
                    ws_server,
                    "serve",
                    side_effect=lambda *_args, **_kwargs: _ServeContext(
                        cancel_on_enter=False
                    ),
                ),
            ):
                start_task = asyncio.create_task(server.start())
                await asyncio.wait_for(cleanup_started.wait(), timeout=1)
                cleanup_task = server._cleanup_task
                start_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await start_task

                try:
                    self.assertTrue(cleanup_finalized.is_set())
                    self.assertTrue(cleanup_task.done())
                    self.assertIsNone(server._cleanup_task)
                    await server.stop()
                    await server.stop()
                    self.assertIsNone(server._cleanup_task)
                finally:
                    if not cleanup_task.done():
                        cleanup_task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await cleanup_task

        asyncio.run(scenario())

    def test_main_thread_registers_supported_process_signals(self):
        registered = []

        async def scenario():
            loop = asyncio.get_running_loop()

            def add_signal_handler(sig, callback, *args):
                registered.append(sig)
                if sig == signal.SIGTERM:
                    callback(*args)

            with (
                patch.object(loop, "add_signal_handler", add_signal_handler),
                patch.object(ws_server, "HAS_WEBSOCKETS", True),
                patch.object(ws_server.sys, "platform", "linux"),
                patch.object(
                    ws_server,
                    "serve",
                    side_effect=lambda *_args, **_kwargs: _ServeContext(
                        cancel_on_enter=False
                    ),
                ),
            ):
                await _server().start()

        asyncio.run(scenario())

        self.assertEqual(registered, [signal.SIGINT, signal.SIGTERM])

    def test_unsupported_main_thread_signal_api_does_not_abort_startup(self):
        async def scenario():
            loop = asyncio.get_running_loop()

            def unsupported(*_args):
                raise NotImplementedError("event loop has no signal support")

            with (
                patch.object(loop, "add_signal_handler", unsupported),
                patch.object(ws_server, "HAS_WEBSOCKETS", True),
                patch.object(ws_server.sys, "platform", "linux"),
                patch.object(
                    ws_server,
                    "serve",
                    side_effect=lambda *_args, **_kwargs: _ServeContext(
                        cancel_on_enter=True
                    ),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await _server().start()

        asyncio.run(scenario())

    def test_worker_thread_skips_process_signal_registration(self):
        outcome = queue.Queue()

        def run_in_worker():
            async def scenario():
                loop = asyncio.get_running_loop()

                def forbidden(*_args):
                    raise AssertionError("worker thread registered a process signal")

                with (
                    patch.object(loop, "add_signal_handler", forbidden),
                    patch.object(ws_server, "HAS_WEBSOCKETS", True),
                    patch.object(ws_server.sys, "platform", "linux"),
                    patch.object(
                        ws_server,
                        "serve",
                        side_effect=lambda *_args, **_kwargs: _ServeContext(
                            cancel_on_enter=True
                        ),
                    ),
                ):
                    try:
                        await _server().start()
                    except BaseException as exc:
                        return exc
                return None

            outcome.put(asyncio.run(scenario()))

        worker = threading.Thread(target=run_in_worker)
        worker.start()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive(), "worker-thread startup hung")
        self.assertIsInstance(outcome.get_nowait(), asyncio.CancelledError)


if __name__ == "__main__":
    unittest.main()
