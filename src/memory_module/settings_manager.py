"""Tamper-resistant local settings storage.

Stores provider configuration, UI preferences, memory thresholds, and local
cryptographic material used by the desktop application.

Implementation: AES-256-GCM with a key derived from the HW Fingerprint (HKDF).
The file is a binary blob: nonce(12) || ciphertext || tag(16).
Inside the ciphertext there is JSON with an HMAC checksum → double protection.

Access control for the local daemon lives in memory_module.security.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    HAS_AESGCM = True
except ImportError:  # pragma: no cover
    AESGCM = None
    HKDF = None
    hashes = None
    HAS_AESGCM = False

from .config import DEFAULT_CONFIG
from .utils.hw_fingerprint import get_hw_fingerprint

logger = logging.getLogger("bdbm.settings")

_HKDF_INFO = b'biomem-settings-v1'
_HKDF_SALT = b'bdbm-tamper-resistant-2026'
_HMAC_KEY_INFO = b'bdbm-hmac-integrity-v1'
_NONCE_SIZE = 12
_STATE_FILE_NAME = 'biomem_settings.dat'


class SettingsManager(object):
    """Encrypted local application settings bound to this machine."""

    _DEFAULT_CONTEXT_LIMIT = 250
    _DEFAULT_MAX_ASSOC = 5
    _DEFAULT_OLLAMA_TIMEOUT_MIN = 7
    _MAX_ASSOC_MAX = 10
    _MAX_ASSOC_MIN = 3
    _MAX_OLLAMA_TIMEOUT_MIN = 60
    _MEM_THRESHOLD_MAX = 0.85
    _MEM_THRESHOLD_MIN = 0.25
    _MIN_OLLAMA_TIMEOUT_MIN = 7
    _VALID_LLM_MODELS = {'claude', 'gemini', 'chatgpt', 'ollama'}

    def __init__(self, data_dir: 'Path'):
        """
        Args:
            data_dir: Path to the biomem data directory (e.g. ~/.bdbm/ or %LOCALAPPDATA%/BDBM/).
        """
        self._data_dir = Path(data_dir)
        self._file_path = self._data_dir / _STATE_FILE_NAME
        self._hw_fp = get_hw_fingerprint()
        self._aes_key = self._derive_key(self._hw_fp)
        self._hmac_key = self._derive_key(self._hw_fp, _HMAC_KEY_INFO)
        self._loaded = False
        self._state = self._load_from_disk()
        self._loaded = True

    # ------------------------------------------------------------------ #
    # static helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _current_module_version():
        """Current settings schema version."""
        return (2, 4, 0)

    @staticmethod
    def _default_ltm_threshold():
        """Returns the default ltm_new_center_threshold value from config.py."""
        return DEFAULT_CONFIG.ltm_new_center_threshold

    @staticmethod
    def _default_stm_threshold():
        """Returns the default stm_new_center_threshold value from config.py."""
        return DEFAULT_CONFIG.stm_new_center_threshold

    @staticmethod
    def _default_state() -> 'Dict[str, Any]':
        """Default state for a new user."""
        return {
            'module_version': SettingsManager._current_module_version(),
            'last_news_id': '',
            'pt_import_locked': False,
            'ui_language': 'en',
            'llm_keys': {},
            'llm_model_names': {},
            'llm_personalisations': {},
            'llm_context_limits': {},
            'ltm_new_center_threshold': None,
            'stm_new_center_threshold': None,
            'max_associations': SettingsManager._DEFAULT_MAX_ASSOC,
            'ollama_timeout_min': SettingsManager._DEFAULT_OLLAMA_TIMEOUT_MIN,
            'telemetry_session_hash': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _derive_key(hw_fp: 'bytes', info: 'bytes' = _HKDF_INFO) -> 'bytes':
        """Derives a 256-bit key from the HW fingerprint using HKDF."""
        if HAS_AESGCM:
            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_HKDF_SALT,
                info=info,
            )
            extended_key = hkdf.derive(hw_fp)
        else:  # pragma: no cover — fallback without cryptography
            # NOTE: exact PBKDF2 fallback parameters
            # (rounds, length) are not part of the API contract.
            salt = _HKDF_SALT + info
            rounds = 100_000
            extended_key = hashlib.pbkdf2_hmac('sha256', hw_fp, salt, rounds, dklen=32)
        return extended_key

    @staticmethod
    def _xor_obfuscate(data: 'bytes', key: 'bytes') -> 'bytes':
        """Simple XOR obfuscation (fallback without cryptography)."""
        return bytes(
            a ^ b
            for a, b in zip(data, key * (len(data) // len(key) + 1))
        )

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def _load_from_disk(self) -> 'Dict[str, Any]':
        """Loads and decrypts the state from disk."""
        defaults = self._default_state()
        if not self._file_path.exists():
            return defaults
        try:
            blob = self._file_path.read_bytes()
            if HAS_AESGCM:
                if len(blob) < (_NONCE_SIZE + 16):
                    logger.warning('Corrupted settings file (too short)')
                    return defaults
                nonce = blob[:_NONCE_SIZE]
                ciphertext = blob[_NONCE_SIZE:]
                aesgcm = AESGCM(self._aes_key)
                plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            else:  # pragma: no cover — fallback bez cryptography
                plaintext = self._xor_obfuscate(blob, self._aes_key)
            payload = json.loads(plaintext.decode('utf-8'))
            data = payload.get('data', {})
            stored_mac = payload.get('mac')
            expected_mac = hmac.new(
                self._hmac_key,
                json.dumps(data, ensure_ascii=False).encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()
            if not stored_mac or not hmac.compare_digest(str(stored_mac), expected_mac):
                logger.warning('HMAC verification failed — the file was modified!')
                return defaults
            state = dict(defaults)
            state.update(data)
            logger.info('Settings loaded from disk')
            return state
        except Exception as exc:
            # NOTE: attempt to migrate from a v1.0 file (plain JSON)
            # is a guess — the evidence indicates "legacy_state" and notes about
            # v1 → v2 migration; the exact legacy file shape is not captured.
            try:
                legacy = json.loads(blob.decode('utf-8'))
                if isinstance(legacy, dict):
                    state = dict(defaults)
                    state.update(legacy)
                    logger.info('Settings loaded from a plain JSON file')
                    return state
            except Exception:
                pass
            logger.warning('Settings cannot be loaded (corrupted/different HW?): %s', exc)
            return defaults

    def _persist(self) -> 'None':
        """Encrypts and writes the state to disk."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            state_json = json.dumps(self._state, ensure_ascii=False).encode('utf-8')
            mac = hmac.new(self._hmac_key, state_json, hashlib.sha256).hexdigest()
            payload = json.dumps(
                {'data': self._state, 'mac': mac}, ensure_ascii=False
            ).encode('utf-8')
            if HAS_AESGCM:
                nonce = os.urandom(_NONCE_SIZE)
                aesgcm = AESGCM(self._aes_key)
                blob = nonce + aesgcm.encrypt(nonce, payload, None)
            else:  # pragma: no cover — fallback bez cryptography
                blob = self._xor_obfuscate(payload, self._aes_key)
            self._file_path.write_bytes(blob)
        except Exception as exc:
            logger.error('Settings persist failed: %s', exc)

    # ------------------------------------------------------------------ #
    # properties
    # ------------------------------------------------------------------ #

    @property
    def last_news_id(self):
        return self._state.get('last_news_id', '')

    @property
    def pt_import_locked(self):
        """Is the import of legacy .pt files already permanently locked?"""
        return bool(self._state.get('pt_import_locked'))

    def lock_pt_import(self) -> 'None':
        """Permanently locks .pt import (after the first import or first write)."""
        if self._state.get('pt_import_locked'):
            return
        self._state['pt_import_locked'] = True
        logger.info('Settings: pt_import_locked = True (.pt import permanently locked)')
        self._persist()

    def set_last_news_id(self, news_id: 'str') -> 'None':
        """Stores the ID of the last read news item."""
        self._state['last_news_id'] = str(news_id)
        self._persist()

    # ------------------------------------------------------------------ #
    # settings (per-model + general)
    # ------------------------------------------------------------------ #

    def set_llm_key(self, model: 'str', key: 'str') -> 'None':
        """Stores the API key (or Ollama URL) for the given model."""
        self._state['llm_keys'][model] = key
        self._persist()

    def get_llm_key(self, model: 'str') -> 'str':
        """Returns the API key (or Ollama URL) for the given model."""
        return self._state.get('llm_keys', {}).get(model, '')

    def set_llm_model_name(self, model: 'str', name: 'str') -> 'None':
        """Stores a custom model name."""
        self._state['llm_model_names'][model] = name
        self._persist()

    def get_llm_model_name(self, model: 'str') -> 'str':
        """Returns the custom model name (or an empty string to use the default)."""
        return self._state.get('llm_model_names', {}).get(model, '')

    def set_personalisation(self, model: 'str', text: 'str') -> 'None':
        """Stores the personalisation system prompt."""
        self._state['llm_personalisations'][model] = text
        self._persist()

    def get_personalisation(self, model: 'str') -> 'str':
        """Returns the personalisation system prompt for the given model."""
        return self._state.get('llm_personalisations', {}).get(model, '')

    def set_context_limit(self, model: 'str', limit: 'int') -> 'None':
        """Stores the context window limit."""
        self._state['llm_context_limits'][model] = int(limit)
        self._persist()

    def get_context_limit(self, model: 'str') -> 'int':
        """Returns the word limit for the conversation history for the given model."""
        return int(
            self._state.get('llm_context_limits', {}).get(
                model, self._DEFAULT_CONTEXT_LIMIT
            )
        )

    def set_ltm_threshold(self, value: 'float') -> 'None':
        """Stores the LTM new center threshold (clamped to 0.25–0.85)."""
        clamped = max(self._MEM_THRESHOLD_MIN, min(self._MEM_THRESHOLD_MAX, float(value)))
        self._state['ltm_new_center_threshold'] = clamped
        self._persist()

    def get_ltm_threshold(self) -> 'float':
        """Returns the LTM new center threshold (0.25–0.85). None = use the config.py default."""
        value = self._state.get('ltm_new_center_threshold')
        if value is None:
            value = self._default_ltm_threshold()
        if value is None:
            return None
        return float(value)

    def set_stm_threshold(self, value: 'float') -> 'None':
        """Stores the STM new center threshold (clamped to 0.25–0.85)."""
        clamped = max(self._MEM_THRESHOLD_MIN, min(self._MEM_THRESHOLD_MAX, float(value)))
        self._state['stm_new_center_threshold'] = clamped
        self._persist()

    def get_stm_threshold(self) -> 'float':
        """Returns the STM new center threshold (0.25–0.85). None = use the config.py default."""
        value = self._state.get('stm_new_center_threshold')
        if value is None:
            value = self._default_stm_threshold()
        if value is None:
            return None
        return float(value)

    def set_max_associations(self, value: 'int') -> 'None':
        """Stores the maximum number of associations (clamped to 3–10)."""
        clamped = max(self._MAX_ASSOC_MIN, min(self._MAX_ASSOC_MAX, int(value)))
        self._state['max_associations'] = clamped
        self._persist()

    def get_max_associations(self) -> 'int':
        """Returns the maximum number of associations for the model (min 3, max 10, default 5)."""
        value = self._state.get('max_associations', self._DEFAULT_MAX_ASSOC)
        return int(max(self._MAX_ASSOC_MIN, min(self._MAX_ASSOC_MAX, value)))

    def set_ollama_timeout_min(self, minutes: 'int') -> 'None':
        """Stores the Ollama HTTP timeout in minutes (clamped to 7–60)."""
        clamped = max(self._MIN_OLLAMA_TIMEOUT_MIN, min(self._MAX_OLLAMA_TIMEOUT_MIN, int(minutes)))
        self._state['ollama_timeout_min'] = clamped
        self._persist()

    def get_ollama_timeout_min(self) -> 'int':
        """Returns the Ollama HTTP timeout in minutes (7–60)."""
        value = self._state.get('ollama_timeout_min', self._DEFAULT_OLLAMA_TIMEOUT_MIN)
        return int(max(self._MIN_OLLAMA_TIMEOUT_MIN, min(self._MAX_OLLAMA_TIMEOUT_MIN, value)))

    def set_ui_language(self, lang: 'str') -> 'None':
        """Stores the preferred UI language."""
        self._state['ui_language'] = lang
        self._persist()

    def get_ui_language(self) -> 'str':
        """Returns the preferred UI language (default 'en')."""
        return self._state.get('ui_language', 'en')

    def get_session_hash(self) -> 'str':
        """Returns a 40-char SHA-1 session hash (for telemetry). Generated on first use."""
        session_hash = self._state.setdefault('telemetry_session_hash', None)
        if not session_hash:
            # NOTE: the randomness source (secrets vs. os.urandom)
            # and the fact that the hash is generic (not derived from credentials)
            # is not unambiguous; secrets chosen.
            session_hash = hashlib.sha1(secrets.token_bytes(20)).hexdigest()
            self._state['telemetry_session_hash'] = session_hash
            self._persist()
        return session_hash
