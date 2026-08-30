"""
Telemetry for the biomem desktop app.

Periodically sends session statistics via Socket.IO (websocket transport).
The message format matches the web client variant,
the interval is 30 minutes.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger('bdbm.telemetry')

INTERVAL_SECONDS = 1800
MESSAGE_PREFIX = 'bdbma'

# Telemetry has no configured remote endpoint and is opt-in only.
SERVER_URL = ''


class TelemetryClient(object):
    """Socket.IO client for sending periodic telemetry.

    Args:
        get_stats_fn:        callable() -> dict — {"stm": int, "ltm": int, "model": str, "bdbm_status": str}
        get_session_hash_fn: callable() -> str  — persistent 40-char SHA-1
        interval_seconds:    interval between sends (default 30 min)
    """

    def __init__(self, get_stats_fn: Callable[[], dict],
                 get_session_hash_fn: Callable[[], str],
                 interval_seconds: int = INTERVAL_SECONDS):
        self._get_stats_fn = get_stats_fn
        self._get_session_hash_fn = get_session_hash_fn
        self._interval = interval_seconds
        self._sio = None
        self._http_session = None
        self._stop_task = None

    async def start(self) -> None:
        """Connects the Socket.IO client and starts the periodic loop.

        Telemetry is disabled unless explicitly enabled with BDBM_TELEMETRY=1.
        """
        import os as _os
        if _os.environ.get('BDBM_TELEMETRY', '0') != '1':
            logger.info('Telemetry: disabled')
            return
        if not SERVER_URL:
            logger.info('Telemetry: no endpoint configured')
            return
        try:
            import socketio
        except ImportError:
            logger.warning('python-socketio not installed; telemetry disabled.')
            return
        try:
            import aiohttp
        except ImportError:
            logger.warning('python-socketio not installed; telemetry disabled.')
            return

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            logger=False,
            engineio_logger=False,
        )

        @self._sio.event
        async def connect():
            logger.info('Telemetry: socket.io connected (sid=%s)', self._sio.sid)
            logger.info('Telemetry: connected to %s', SERVER_URL)

        @self._sio.event
        async def disconnect():
            logger.info('Telemetry: socket.io disconnected')

        @self._sio.on('connect_error')
        async def connect_error(data):
            logger.warning('Telemetry: connect_error: %s', data)

        # Custom SSL context (certifi + Windows ROOT) — see net.py
        try:
            from .net import build_ssl_context
            ssl_ctx = build_ssl_context()
        except Exception as e:
            logger.warning('Telemetry: custom SSL context unavailable (%s); using default.', e)
            ssl_ctx = None

        self._http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_ctx),
        )
        try:
            await self._sio.connect(
                SERVER_URL,
                transports=['websocket'],
                http_session=self._http_session,
                auth={'s_redis_hash': 'tele||' + self._get_session_hash_fn()},
            )
        except Exception as e:
            logger.warning('Telemetry: connect failed (%s)', e)
            await self._http_session.close()
            self._http_session = None
            return

        self._stop_task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """Stops the loop and closes the connection."""
        if self._stop_task is not None:
            self._stop_task.cancel()
            try:
                await self._stop_task
            except asyncio.CancelledError:
                pass
            self._stop_task = None
        if self._sio is not None:
            await self._sio.disconnect()
            self._sio = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _loop(self) -> None:
        """Periodic telemetry loop."""
        try:
            while True:
                await self._send_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass

    async def _send_once(self) -> None:
        """Sends one telemetry message."""
        try:
            if self._sio is None:
                return
            if not self._sio.connected:
                logger.debug('Telemetry: skip send (not connected)')
                return
            stats = self._get_stats_fn() or {}
            session_hash = self._get_session_hash_fn()
            # NOTE: exact payload structure (same keys as the web client).
            payload = {
                'message': 'telemetry',
                'session_hash': session_hash,
                's_redis_hash': 'tele||' + session_hash,
                'timestamp': time.time(),
                'stm': stats.get('stm', 0),
                'ltm': stats.get('ltm', 0),
                'model': stats.get('model', ''),
                'bdbm_status': stats.get('bdbm_status', ''),
            }
            await self._sio.emit(MESSAGE_PREFIX, payload)
            logger.debug('Telemetry: sent %s', payload)
        except Exception as e:
            logger.warning('Telemetry send failed: %s', e)
