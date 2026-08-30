"""
WebSocket server for the biomem module (UZEL_B).

Local server on ws://127.0.0.1:8765 for communication with the web client.
Accepts JSON commands and returns JSON responses via the command protocol.

HTTP Fallback:
- Automatically starts an HTTP polling server on port +1 (default 8766)
- REST-like API at http://127.0.0.1:8766/api
- For clients that cannot use WebSocket

Security:
- Origin check (allowed domains only)
- Localhost only (127.0.0.1)
- Multi-tab support (multiple concurrent connections with a 20-minute idle timeout)
"""

import asyncio
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional, Set

from .config import DEFAULT_CONFIG, MemoryConfig
from .http_fallback import HTTPFallbackServer
from .protocol import CommandHandler
from .security import SecurityManager, get_data_dir
from .session_cache import SessionCache
from .text_memory import TextMemory

try:
    import websockets
    from websockets import serve
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

logger = logging.getLogger("bdbm.server")


class _HandshakeNoiseFilter(logging.Filter):
    """
    Silences websockets ERROR tracebacks for "empty" handshake attempts.

    Clients (browser plugin health-check probes, AV port scans) routinely open
    a TCP connection to 127.0.0.1:8765 and close it without sending data —
    websockets logs this as an ERROR with a full traceback (EOFError /
    InvalidMessage), which fills the log and confuses support (field log
    2026-08-24). Real handshake errors with sent data pass through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        exc = record.exc_info
        if exc is not None:
            seen = exc[1]
            if msg == "opening handshake failed" and \
                    "did not receive a valid HTTP request" in str(seen):
                return False
        return True


_ws_logger = logging.getLogger("websockets.server")
_ws_logger.addFilter(_HandshakeNoiseFilter())


class BDBMServer(object):
    """
    WebSocket server for the biomem module.

    Runs on ws://127.0.0.1:8765 and accepts JSON commands
    from the web client (UZEL_D).

    Usage:
        server = BDBMServer()
        await server.start()
    """

    def __init__(self, config: Optional[MemoryConfig] = None, host: Optional[str] = None,
                 port: Optional[int] = None, memory: Optional[TextMemory] = None,
                 state_file: Optional[str] = None, settings_manager=None):
        """
        Args:
            config: Memory configuration
            host: Host for the WS server (default: from config)
            port: Port for the WS server (default: from config)
            memory: Existing TextMemory instance (or a new one is created)
            state_file: Path to the memory state file
            settings_manager: Local application settings
        """
        self.config = config or DEFAULT_CONFIG
        self.host = host or self.config.ws_host
        self.port = port or self.config.ws_port
        self._data_dir = get_data_dir(self.config.data_dir)
        self._state_file = str(self._data_dir / (state_file or self.config.state_file))
        self.memory = memory or TextMemory(config=self.config, state_file=self._state_file)
        self.session_cache = SessionCache(ttl_seconds=self.config.session_ttl)
        self.security = SecurityManager(
            data_dir=self._data_dir,
            allowed_origins=self.config.ws_allowed_origins,
        )
        self.handler = CommandHandler(
            self.memory, self.session_cache, self.security, settings_manager
        )
        self._http_fallback = HTTPFallbackServer(
            handler=self.handler,
            security=self.security,
            host=self.host,
            port=self.port + 1,
        )
        self._server = None
        self._running = False
        self._active_connections = set()
        self._connection_activity = {}
        self._cleanup_task = None

    @property
    def is_running(self):
        return self._running

    @property
    def has_active_connection(self):
        return bool(self._active_connections)

    async def start(self) -> None:
        """
        Starts the WebSocket server and waits for connections.

        Blocking call – the server runs until it is stopped.
        """
        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        def console_ctrl_handler(ctrl_type):
            if ctrl_type == 2:  # CTRL_CLOSE_EVENT — window closed (via X button)
                logger.info("🛑 Window close detected (X button)...")
                try:
                    self.memory.save()
                    logger.info("💾 Memory state automatically saved to: %s", self._state_file)
                except Exception as e:
                    logger.error("❌ Save error: %s", e)
            return False

        async def keyboard_monitor():
            while self._running:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b"\x11", b"Q", b"q"):
                        logger.info("⏹️ User quit (Ctrl+Q)")
                        stop.set_result(None)
                        return
                await asyncio.sleep(0.1)

        logger.info("🧠 Starting biomem Memory Module on ws://%s:%s", self.host, self.port)
        logger.info("📁 Data: %s", self._data_dir)
        logger.info("💾 State: %s", self._state_file)
        logger.info("🔒 Allowed origins: %s", self.security.allowed_origins)
        if not HAS_WEBSOCKETS:
            raise RuntimeError("The 'websockets' library is not installed. "
                               "Install it with: pip install websockets>=12.0")
        try:
            self._http_fallback.update_event_loop(loop)
            self._http_fallback.start()
            async with serve(
                self._handle_connection,
                self.host,
                self.port,
                process_request=self._check_origin,
                ping_interval=30,
                ping_timeout=10,
            ) as server:
                self._server = server
                self._running = True
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
                logger.info("✅ biomem server is running on ws://%s:%s", self.host, self.port)
                logger.info("ℹ️  To stop the biomem server, use the ctrl+q keyboard shortcut")
                if sys.platform == "win32":
                    import ctypes
                    import msvcrt
                    import threading as _threading

                    self._console_ctrl_handler = ctypes.WINFUNCTYPE(
                        ctypes.c_bool, ctypes.c_uint)(console_ctrl_handler)
                    ctypes.windll.kernel32.SetConsoleCtrlHandler(
                        self._console_ctrl_handler, True)
                    if _threading.current_thread() is _threading.main_thread():
                        sig = signal.signal(signal.SIGINT, console_ctrl_handler)
                    self._kb_task = asyncio.create_task(keyboard_monitor())
                if threading.current_thread() is threading.main_thread():
                    try:
                        loop.add_signal_handler(signal.SIGINT, stop.set_result, None)
                        loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
                    except (NotImplementedError, RuntimeError) as exc:
                        logger.debug("Process signal handlers unavailable: %s", exc)
                await stop
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stops the server and saves the memory state."""
        logger.info("🛑 Stopping the biomem server...")
        self._running = False
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None and cleanup_task is not asyncio.current_task():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        for ws in list(self._active_connections):
            try:
                await ws.close(1001, "Server shutting down")
            except Exception:
                pass
        if self._http_fallback.is_running:
            self._http_fallback.stop()
        try:
            self.memory.save()
            logger.info("💾 Memory state saved to: %s", self._state_file)
        except Exception as e:
            logger.error("❌ Error saving state: %s", e)
        logger.info("✅ biomem server stopped.")
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._console_ctrl_handler, False)

    async def _handle_connection(self, websocket) -> None:
        """
        Handler for new WebSocket connections.

        Supports multiple concurrent connections (multi-tab/multi-window support).
        """
        remote = websocket.remote_address
        self._active_connections.add(websocket)
        self._connection_activity[websocket] = time.time()
        logger.info("🔌 New connection from: %s", remote)
        try:
            async for raw_message in websocket:
                self._connection_activity[websocket] = time.time()
                try:
                    message = json.loads(raw_message)
                    response = await self.handler.handle(message)
                    logger.debug("📨 Command: %s", message.get("command"))
                    await websocket.send(json.dumps(response))
                    logger.debug("📤 Response: status=%s", response.get("status"))
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "status": "error",
                        "code": "INVALID_JSON",
                        "error": "Invalid JSON.",
                    }))
        except websockets.exceptions.ConnectionClosed:
            logger.info("🔌 Connection closed: %s", remote)
        except Exception as e:
            logger.error("❌ Connection error: %s", e)
        finally:
            self._active_connections.discard(websocket)
            self._connection_activity.pop(websocket, None)
            logger.info("🔌 Connection released: %s", remote)

    async def _check_origin(self, connection, request):
        """
        Origin header check during the WebSocket handshake.

        Called automatically by the websockets library. Warnings are
        rate-limited per origin (max 1× per 5 minutes) so the log is not
        flooded with repeated rejections of the same origin.
        """
        origin = request.headers.get("Origin")
        if not self.security.is_allowed_origin(origin):
            _t = time.time()
            key = origin
            try:
                last = self._rejected_origin_last_warned.get(key)
            except AttributeError:
                self._rejected_origin_last_warned = {}
                last = None
            now = last is None or (_t - last) > 300.0
            if now:
                logger.warning("🚫 Rejected origin: %s", origin)
                self._rejected_origin_last_warned[key] = _t
            else:
                logger.debug("Rejected origin (rate-limited): %s", origin)
            return connection.respond(403, "Forbidden: disallowed origin.")

    async def _periodic_cleanup(self) -> None:
        """Periodic cleanup of expired session records and inactive connections (20 min idle timeout)."""
        while self._running:
            await asyncio.sleep(self.config.session_cleanup_interval)
            try:
                removed = self.session_cache.cleanup_expired()
                if removed:
                    logger.debug("🧹 Cleaned up %d expired sessions", removed)
                now = time.time()
                idle_conns = [
                    ws for ws in self._active_connections
                    if now - self._connection_activity.get(ws, now) > 1200
                ]
                for ws in idle_conns:
                    logger.info("💤 Sleeping inactive connection %s (20 min idle)",
                                ws.remote_address)
                    try:
                        await ws.close(4000, "Idle timeout")
                    except Exception as e:
                        logger.debug("Error closing idle connection: %s", e)
            except Exception as e:
                logger.error("Cleanup error: %s", e)
