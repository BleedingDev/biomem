'''
HTTP Polling Fallback server for the biomem module.

Provides a REST-like API at http://127.0.0.1:8766 as a fallback
when a WebSocket connection is not possible (e.g. corporate proxy, blocked WS).

Endpoints:
    POST /api          - Accepts JSON commands (same format as WS)
    GET  /api/health   - Versioned product/readiness check
    GET  /api/status   - Backward-compatible detailed status
    OPTIONS /api*      - CORS preflight

Commands are processed through the same CommandHandler as the WS server.
'''
import asyncio
import ipaddress
import json
import logging
import socket
import time as _time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from threading import Thread
from typing import Any, Awaitable, Callable, Dict, Optional

from .protocol import CommandHandler
from .security import SecurityManager

logger = logging.getLogger('bdbm.http')

_MAX_BODY_SIZE = 1024 * 1024  # 1MB
_PRODUCT_ID = 'biomem'
_HTTP_PROTOCOL_VERSION = 1
_SUPPORTED_PATHS = frozenset(('/api', '/api/health', '/api/status'))


def _is_loopback_address(address: 'str') -> 'bool':
    '''Returns whether an IP address or the localhost name is loopback-only.'''
    if address.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(address.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


class HTTPFallbackServer(object):
    '''
    HTTP Fallback server for the biomem module.

    Runs on a separate thread alongside the WebSocket server
    and provides a REST-like API for clients that cannot
    use WebSocket (proxy, blocker, etc.).

    Usage:
        http_server = HTTPFallbackServer(
            handler=command_handler,
            security=security_manager,
            host="127.0.0.1",
            port=8766,
        )
        http_server.start()    # Starts in the background
        ...
        http_server.stop()     # Stops
    '''

    def __init__(self, handler: 'CommandHandler', security: 'SecurityManager',
                 host: 'str' = '127.0.0.1', port: 'int' = 8766,
                 event_loop: 'Optional[asyncio.AbstractEventLoop]' = None):
        if not _is_loopback_address(host):
            raise ValueError(
                'HTTP fallback must bind to a loopback address '
                '(127.0.0.1, ::1, or localhost).'
            )
        self.host = host
        self.port = port
        self._handler = handler
        self._security = security
        self._loop = event_loop  # type: Optional[asyncio.AbstractEventLoop]
        self._server = None  # type: Optional[ThreadingHTTPServer]
        self._thread = None  # type: Optional[Thread]
        # Each server gets an isolated handler subclass. Class-level attributes are
        # required by BaseHTTPRequestHandler, but sharing the base class would let a
        # second server silently replace the first server's command/security objects.
        self._request_handler = type(
            'ConfiguredBDBMHTTPHandler',
            (BDBMHTTPHandler,),
            {
                'command_handler': handler,
                'security': security,
                '_loop': event_loop,
                '_rejected_origin_last_warned': {},
            },
        )

    @property
    def is_running(self) -> 'bool':
        '''Is the server active?'''
        return self._thread is not None and self._thread.is_alive()

    @property
    def bound_port(self) -> 'int':
        '''Returns the actual bound port (useful when port=0 selects a free port).'''
        if self._server is not None:
            return int(self._server.server_address[1])
        return int(self.port)

    def start(self):
        '''Starts the HTTP server in a background thread.'''
        if self.is_running:
            return
        server_class = ThreadingHTTPServer
        try:
            if ipaddress.ip_address(self.host.split('%', 1)[0]).version == 6:
                server_class = ThreadingHTTPServerV6
        except ValueError:
            pass
        self._server = server_class((self.host, self.port), self._request_handler)
        self._thread = Thread(target=partial(self._server.serve_forever),
                              daemon=True, name='bdbm-http-fallback')
        self._thread.start()
        logger.info(f'🌐 HTTP fallback server running at http://{self.host}:{self.port}/api')

    def stop(self):
        '''Stops the HTTP server.'''
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        logger.info('🛑 HTTP fallback server stopped.')

    def update_event_loop(self, loop: 'asyncio.AbstractEventLoop'):
        '''Updates the reference to the asyncio event loop.'''
        self._loop = loop
        self._request_handler._loop = loop


class BDBMHTTPHandler(BaseHTTPRequestHandler):
    '''
    HTTP handler for biomem commands.

    Handles POST requests to /api with the same JSON commands
    as the WebSocket server.
    '''
    command_handler = None  # type: Optional[CommandHandler]
    security = None  # type: Optional[SecurityManager]
    _loop = None  # type: Optional[asyncio.AbstractEventLoop]
    _rejected_origin_last_warned = {}  # type: Dict[str, float]
    _REJECT_LOG_INTERVAL_S = 300.0  # type: float

    def _request_path(self) -> 'str':
        '''Returns the request path without a query string.'''
        return self.path.partition('?')[0]

    def _check_client(self) -> 'bool':
        '''Rejects any request whose peer address is not loopback.'''
        client_address = str(self.client_address[0])
        if _is_loopback_address(client_address):
            return True
        logger.warning('HTTP: Rejected non-loopback client: %s', client_address)
        self._send_json(HTTPStatus.FORBIDDEN, {
            'status': 'error',
            'code': 'LOOPBACK_REQUIRED',
            'error': 'The local transport accepts loopback clients only.',
        })
        return False

    def _set_cors_headers(self):
        '''Sets CORS headers for cross-origin requests.'''
        origin = self.headers.get('Origin')
        if self.security is not None and not self.security.is_allowed_origin(origin):
            origin = 'null'
        self.send_header('Access-Control-Allow-Origin', origin or '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-API-Key')
        self.send_header('Access-Control-Max-Age', '3600')
        self.send_header('Vary', 'Origin')

    def _send_json(self, status_code: 'int', data: 'dict'):
        '''Sends a JSON response.'''
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _check_origin(self) -> 'bool':
        '''Origin header check. Warnings are rate-limited per origin.'''
        origin = self.headers.get('Origin')
        if self.security is not None and not self.security.is_allowed_origin(origin):
            now = _time.time()
            last = self._rejected_origin_last_warned.get(origin)
            if last is None or now - last > self._REJECT_LOG_INTERVAL_S:
                logger.warning(f'🚫 HTTP: Rejected origin: {origin}')
                self._rejected_origin_last_warned[origin] = now
            else:
                logger.debug(f'Rejected origin (rate-limited): {origin}')
            self._send_json(HTTPStatus.FORBIDDEN, {
                'status': 'error',
                'code': 'FORBIDDEN',
                'error': f'Disallowed origin: {origin}',
            })
            return False
        return True

    def _submit_command(self, message: 'Dict[str, Any]') -> 'Any':
        '''Processes the command via CommandHandler (synchronously from the HTTP thread).'''
        if self.command_handler is None:
            raise RuntimeError('The biomem command handler is unavailable.')
        if self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                self.command_handler.handle(message), self._loop)
            return future.result()
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.command_handler.handle(message))
        finally:
            loop.close()

    def _status_payload(self, result: 'Dict[str, Any]', ready: 'bool') -> 'Dict[str, Any]':
        '''Adds stable product identity and protocol markers to a status result.'''
        payload = dict(result)
        from . import __version__
        payload.update({
            'product': _PRODUCT_ID,
            'version': str(__version__),
            'protocol_version': _HTTP_PROTOCOL_VERSION,
            'transport': 'http',
            'ready': ready,
        })
        return payload

    def _handle_quick_status(self):
        '''Returns status plus stable product/readiness markers.'''
        try:
            result = self._submit_command({'command': 'status'})
            if not isinstance(result, dict):
                raise RuntimeError('The status command returned a non-object response.')
            ready = result.get('status') == 'success'
            status_code = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status_code, self._status_payload(result, ready))
        except Exception as e:
            logger.error(f'HTTP handler error: {e}')
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, self._status_payload({
                'status': 'error',
                'code': 'SERVICE_UNAVAILABLE',
                'error': 'The biomem service is not ready.',
            }, False))

    def do_GET(self):
        '''GET endpoints.'''
        if not self._check_client():
            return
        path = self._request_path()
        if path not in ('/api/health', '/api/status'):
            self._send_json(HTTPStatus.NOT_FOUND, {
                'status': 'error',
                'code': 'NOT_FOUND',
                'error': f"Endpoint '{path}' does not exist. Use GET /api/health or POST /api.",
            })
            return
        if not self._check_origin():
            return
        self._handle_quick_status()

    def do_POST(self):
        '''POST endpoint for commands.'''
        if not self._check_client():
            return
        if self._request_path() != '/api':
            self._send_json(HTTPStatus.NOT_FOUND, {
                'status': 'error',
                'code': 'NOT_FOUND',
                'error': 'Use POST /api for commands.',
            })
            return
        if not self._check_origin():
            return
        try:
            content_type = self.headers.get_content_type()
            if content_type != 'application/json':
                self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {
                    'status': 'error',
                    'code': 'UNSUPPORTED_MEDIA_TYPE',
                    'error': 'Content-Type must be application/json.',
                })
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                length = -1
            if length < 0:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'INVALID_CONTENT_LENGTH',
                    'error': 'Content-Length must be a non-negative integer.',
                })
                return
            if length > _MAX_BODY_SIZE:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                    'status': 'error',
                    'code': 'PAYLOAD_TOO_LARGE',
                    'error': 'The request body is too large (max 1MB).',
                })
                return
            raw = self.rfile.read(length)
            if not raw:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'EMPTY_BODY',
                    'error': 'Empty request body. Send a JSON with a command.',
                })
                return
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'INVALID_ENCODING',
                    'error': 'Invalid encoding. Use UTF-8.',
                })
                return
            try:
                msg = json.loads(text)
            except json.JSONDecodeError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'INVALID_JSON',
                    'error': f'Invalid JSON: {e}',
                })
                return
            if not isinstance(msg, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'INVALID_REQUEST',
                    'error': 'The JSON request body must be an object.',
                })
                return
            command = msg.get('command')
            if not isinstance(command, str) or not command.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    'status': 'error',
                    'code': 'INVALID_COMMAND',
                    'error': "A non-empty string 'command' field is required.",
                })
                return
            result = self._submit_command(msg)
            if not isinstance(result, dict):
                raise RuntimeError('The command handler returned a non-object response.')
            self._send_json(HTTPStatus.OK, result)
        except Exception as e:
            logger.error(f'HTTP handler error: {e}')
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                'status': 'error',
                'code': 'SERVICE_UNAVAILABLE',
                'error': 'The biomem service is not ready.',
            })

    def do_OPTIONS(self):
        '''CORS preflight handler.'''
        if not self._check_client():
            return
        if self._request_path() not in _SUPPORTED_PATHS:
            self._send_json(HTTPStatus.NOT_FOUND, {
                'status': 'error',
                'code': 'NOT_FOUND',
                'error': 'Unknown local transport endpoint.',
            })
            return
        if not self._check_origin():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def log_message(self, format, *args):
        '''Redirects logs to Python logging.'''
        logger.debug('HTTP: ' + (format % args))


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    '''Threaded HTTP server for parallel request processing.'''
    daemon_threads = True


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    '''IPv6 variant used when explicitly binding to the ::1 loopback address.'''
    address_family = socket.AF_INET6
