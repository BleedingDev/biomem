'''
Security management of the biomem module.

Responsibilities:
- Origin check for WebSocket connections
- Module state management (ACTIVE / DEACTIVATED / SUSPENDED)
'''
import json
import os
import platform
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

STATE_ACTIVE = 'ACTIVE'
STATE_DEACTIVATED = 'DEACTIVATED'
STATE_SUSPENDED = 'SUSPENDED'

_DEFAULT_ALLOWED_ORIGINS = (
    'http://localhost',
    'http://127.0.0.1',
)

_EXTENSION_ORIGIN_PREFIXES = (
    'chrome-extension://',
    'moz-extension://',
    'safari-web-extension://',
)

_LOCAL_ORIGIN_HOSTS = frozenset(('localhost', '127.0.0.1'))

_OPERATIONAL_COMMANDS = (
    'retrieve', 'store', 'store_record', 'search', 'list_memories',
    'backup', 'restore', 'clear_stm', 'clear_ltm',
)

def _is_local_http_origin(origin: 'str') -> 'bool':
    '''Returns whether an Origin is an explicit local HTTP origin.'''
    try:
        parsed = urlsplit(origin)
        # Reading ``port`` validates malformed values such as ``:not-a-port``.
        parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == 'http'
        and parsed.hostname in _LOCAL_ORIGIN_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ''
        and parsed.query == ''
        and parsed.fragment == ''
    )


def get_data_dir(custom_dir: 'str' = '') -> 'Path':
    '''Returns the path to the biomem data directory.

    Args:
        custom_dir: Custom path. If empty, the default is used.

    Returns:
        Path to the data directory.
    '''
    if custom_dir:
        return Path(custom_dir)
    if platform.system() == 'Windows':
        base = os.environ.get('LOCALAPPDATA', str(Path.home()))
        return Path(base) / 'BDBM'
    return Path.home() / '.bdbm'


class SecurityManager(object):
    '''
    Security manager of the biomem module.

    Provides:
    - Origin check for WebSocket
    - Module state control (active/deactivated/suspended)
    '''

    def __init__(self, data_dir: 'Optional[Path]' = None,
                 allowed_origins: 'Tuple[str, ...]' = _DEFAULT_ALLOWED_ORIGINS):
        '''Args:
        data_dir: Path to the biomem data directory
        allowed_origins: Allowed origins for WS connections
        '''
        self._data_dir = data_dir if data_dir is not None else get_data_dir()
        # Configuration from older releases may still contain public provider
        # pages. Both daemon transports now accept browser commands only from
        # extension background contexts, so never carry those page origins
        # into the runtime trust boundary.
        self._allowed_origins = {
            origin for origin in allowed_origins
            if _is_local_http_origin(origin)
        }
        self._allowed_origins.update(_DEFAULT_ALLOWED_ORIGINS)
        self._state = STATE_ACTIVE
        self._suspend_until = None  # type: Optional[float]

    @property
    def data_dir(self) -> 'Path':
        return self._data_dir

    @property
    def allowed_origins(self):
        return set(self._allowed_origins)

    @property
    def state(self) -> 'str':
        if self._suspend_until is not None:
            if time.time() < self._suspend_until:
                return STATE_SUSPENDED
            self._suspend_until = None
            self._state = STATE_ACTIVE
        return self._state

    @property
    def is_active(self) -> 'bool':
        return self.state == STATE_ACTIVE

    @property
    def is_deactivated(self) -> 'bool':
        return self.state == STATE_DEACTIVATED

    @property
    def is_suspended(self) -> 'bool':
        return self.state == STATE_SUSPENDED

    def is_allowed_origin(self, origin: 'Optional[str]') -> 'bool':
        '''Checks whether the origin is allowed for WS connections.

        Browser page origins are never trusted. Browser commands reach the
        daemon through an extension background context.

        Args:
            origin: HTTP Origin header from the WebSocket handshake.
                    None is allowed (local clients without an origin).

        Returns:
            True if the origin is allowed.
        '''
        if origin is None:
            return True
        if origin.startswith(_EXTENSION_ORIGIN_PREFIXES):
            return True
        return _is_local_http_origin(origin)

    def add_allowed_origin(self, origin: 'str') -> 'None':
        '''Adds a local origin; public page origins remain denied.'''
        if _is_local_http_origin(origin):
            self._allowed_origins.add(origin)

    def remove_allowed_origin(self, origin: 'str') -> 'None':
        '''Removes an allowed origin.'''
        self._allowed_origins.discard(origin)

    def is_operational_command(self, command: 'str') -> 'bool':
        '''Checks whether the command is operational (requires the ACTIVE state).

        Args:
            command: Command name

        Returns:
            True if the command requires the ACTIVE state.
        '''
        return command in _OPERATIONAL_COMMANDS

    def check_command_allowed(self, command: 'str') -> 'Optional[dict]':
        '''Checks whether the command is allowed in the current state.

        Args:
            command: Command name

        Returns:
            None if allowed, otherwise a dict with error information.
        '''
        if not self.is_operational_command(command):
            return None
        if self.state == STATE_SUSPENDED:
            info = self.get_suspend_info()
            return {
                'status': 'error',
                'code': STATE_SUSPENDED,
                'remaining_seconds': info['remaining_seconds'],
                'resume_at': info['resume_at'],
                'error': 'The module is temporarily suspended.',
            }
        if self.state == STATE_DEACTIVATED:
            return {
                'status': 'error',
                'code': STATE_DEACTIVATED,
                'error': "The module is deactivated. Use the 'activate' command.",
            }
        return None

    def activate(self) -> 'str':
        '''Activates the module.

        Note: Cannot override a SUSPENDED state (it has higher priority).

        Returns:
            The new module state.
        '''
        if self.state == STATE_SUSPENDED:
            return STATE_SUSPENDED
        self._state = STATE_ACTIVE
        return self._state

    def deactivate(self) -> 'str':
        '''Deactivates the module (manual).

        Returns:
            The new module state.
        '''
        if self.state == STATE_SUSPENDED:
            return STATE_SUSPENDED
        self._state = STATE_DEACTIVATED
        return self._state

    def force_resume(self) -> 'str':
        '''Immediately resumes the module from the SUSPENDED state.

        Unlike activate(), this method also overrides an active suspension.
        Used for administrative override (e.g. system administrator).

        Returns:
            The new module state (always ACTIVE).
        '''
        self._suspend_until = None
        self._state = STATE_ACTIVE
        return self._state

    def suspend_timed(self, duration_seconds: 'int') -> 'dict':
        '''Suspends the module for a given period.

        Args:
            duration_seconds: Suspension duration in seconds

        Returns:
            Dict with suspension information:
            {
                "state": "SUSPENDED",
                "duration": 3600,
                "resume_at": "2026-02-15T03:30:00Z",
                "remaining_seconds": 3600
            }
        '''
        self._suspend_until = time.time() + duration_seconds
        resume_dt = datetime.fromtimestamp(self._suspend_until, tz=timezone.utc)
        return {
            'state': STATE_SUSPENDED,
            'duration': duration_seconds,
            'resume_at': resume_dt.isoformat(),
            'remaining_seconds': int(self._suspend_until - time.time()),
        }

    def get_suspend_info(self) -> 'Optional[dict]':
        '''Returns information about the current suspension.

        Returns:
            Dict with remaining_seconds and resume_at, or None if not suspended.
        '''
        if self._suspend_until is None:
            return None
        remaining = int(self._suspend_until - time.time())
        if remaining <= 0:
            return None
        resume_dt = datetime.fromtimestamp(self._suspend_until, tz=timezone.utc)
        return {
            'remaining_seconds': remaining,
            'resume_at': resume_dt.isoformat(),
        }
