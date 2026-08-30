# -*- coding: utf-8 -*-
"""Command protocol of the biomem module.

Handles JSON commands received over WebSocket and returns JSON responses.

Commands:
    retrieve  - Recall from memory (semantic search)
    store     - Store into memory (pairing via session cache)
    store_record - Store one explicit record with stable identity and provenance
    search    - Administrative semantic search without recall side effects
    list_memories - Deterministic stable-ID record pagination
    ollama_chat - Full conversation cycle via a local Ollama model
    backup    - Backup of the memory state
    restore   - Restore from a backup
    activate  - Activate the module
    deactivate - Deactivate the module
    suspend_timed - Temporary suspension (rate limiting)
    clear_stm - Clear STM
    clear_ltm - Clear the entire memory
    set_mem_thresholds - Set stm/ltm_new_center_threshold (Advanced Settings)
    status    - Module status
    get_dendrogram - Compute a hierarchical dendrogram of LTM centers (Ward, scipy)
    get_memory_graph - Compute nodes and semantic edges (cosine similarities of embeddings)
"""

import asyncio
import base64
import binascii
import inspect
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import urllib.error
import urllib.request

from .cognitive_audit import CognitiveAuditAnalyzer, CognitiveReportPDFGenerator
from .llm_client import normalize_ollama_base_url
from .security import SecurityManager
from .session_cache import SessionCache
from .text_memory import TextMemory

try:
    from scipy.cluster import hierarchy  # type: ignore
except ImportError:  # pragma: no cover
    hierarchy = None

logger = logging.getLogger('bdbm.protocol')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLLAMA_TIMEOUT = 600  # 10 minutes, synced with the frontend timeout in bdbm-client.js

DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'

# Response error codes
COMPUTE_ERROR = 'COMPUTE_ERROR'
DELETE_ERROR = 'DELETE_ERROR'
DUPLICATE_MEMORY_ID = 'DUPLICATE_MEMORY_ID'
EXPORT_ERROR = 'EXPORT_ERROR'
FILE_NOT_FOUND = 'FILE_NOT_FOUND'
INTERNAL_ERROR = 'INTERNAL_ERROR'
INVALID_COMMAND = 'INVALID_COMMAND'
INVALID_PARAMS = 'INVALID_PARAMS'
MEMORY_ACCESS_ERROR = 'MEMORY_ACCESS_ERROR'
MISSING_INDEX = 'MISSING_INDEX'
NOT_ENOUGH_DATA = 'NOT_ENOUGH_DATA'
OLLAMA_ERROR = 'OLLAMA_ERROR'
OLLAMA_TIMEOUT = 'OLLAMA_TIMEOUT'
OLLAMA_UNAVAILABLE = 'OLLAMA_UNAVAILABLE'
OUT_OF_RANGE = 'OUT_OF_RANGE'
PT_IMPORT_LOCKED = 'PT_IMPORT_LOCKED'
READ_ERROR = 'READ_ERROR'
SAVE_FAILED = 'SAVE_FAILED'
SCIPY_MISSING = 'SCIPY_MISSING'
SESSION_EXPIRED = 'SESSION_EXPIRED'
UNKNOWN_COMMAND = 'UNKNOWN_COMMAND'
WRITE_ERROR = 'WRITE_ERROR'

# Values come from the protocol docstrings:
# "every CONSOLIDATE_EVERY writes", "writes from an old conversation have lower intensity".
CONSOLIDATE_EVERY = 100
IMPORT_INTENSITY = 0.3

MAX_KEY_CHARS = 16_384
MAX_VALUE_CHARS = 32_768
MAX_SESSION_ID_CHARS = 512
MAX_CURSOR_CHARS = 512
MAX_PROVENANCE_CHARS = 4_096
_LAYERS = frozenset({'both', 'stm', 'ltm'})
_PROVENANCE_LIMITS = {
    'source_class': 64,
    'origin': 512,
    'session_id': 512,
    'created_at': 64,
    'updated_at': 64,
}

# ---------------------------------------------------------------------------
# Prompt constants (mirror _buildEnrichedPrompt from conversation.js on the frontend)
# ---------------------------------------------------------------------------

# <System - CRITICAL INSTRUCTION> – instructions for the PAM tokens (|STPAM| |MIDPAM| |ENDPAM|)
MEMORY_SUMMARY_PROMPT = (
    "\n\n\n\n\n<System - CRITICAL INSTRUCTION>\n"
    "First, answer the user's query normally and comprehensively.\n"
    "Then, AT THE VERY END of your ENTIRE response, you MUST append TWO concise summaries "
    "for memory storage using EXACTLY this format:\n"
    "|STPAM| [brief summary of what user asked] |MIDPAM| [brief summary of your answer] |ENDPAM|\n"
    "DO NOT write introductory phrases like 'Summary of the user's query' or 'Here is a summary' "
    "before the tokens. NEVER output these tokens in the middle of your response. ONLY output "
    "the tokens as the absolute final sentence in your message.\n"
    "</System>"
)

# Fragments assembled in _build_ollama_prompt. The prompt grammar is
# composed of these exact literal parts:
#   "\n<System - Recent Conversation History (last ~" + str(limit) + " words max)>"
#   "\n</System - Recent Conversation History>"
#   "\n\n\n<System - New Conversation thread> Since ... Do not wrap it in any brackets."
#   "\n<System - associated memory context (use only if relevant):"
_RECENT_HISTORY_OPEN = "\n<System - Recent Conversation History (last ~"
_RECENT_HISTORY_CLOSE = "\n</System - Recent Conversation History>"
_RECENT_HISTORY_SUFFIX = " words max)>"
_NEW_THREAD_PROMPT = (
    "\n\n\n<System - New Conversation thread> Since this is a new conversation thread, "
    "suggest a brief 2-5 word title for this thread based on the user's first query. "
    "This title will be used to identify the thread in UI. Place this title at the very end "
    "of your response, after the |ENDPAM| token, following the exact format: "
    "|TITLE| Your suggested title. Do not wrap it in any brackets."
)
_MEMORY_CONTEXT_OPEN = "\n<System - associated memory context (use only if relevant):"
_MEMORY_FORMAT_USER = "User: %s"
_MEMORY_FORMAT_MODEL = "Model: %s"
_MEMORY_FORMAT_CONFIDENCE = "Confidence: %.3f"
_MEMORY_FORMAT_TURN = "Turn distance: %d"
_MEMORY_FORMAT_PRE_RESTORE = "(pre-restore backup: %s)"


class CommandHandler(object):
    """
    Processes commands received over WebSocket.

    Responsibilities:
    - Command validation
    - Module state checks (active/deactivated/suspended)
    - TextMemory API calls
    - Session cache management
    - Response formatting
    """

    # Word-count threshold of the query used for keying (_handle_store:
    # "If the original query is <= 20 words → key = original query").
    QUERY_WORD_THRESHOLD = 20

    def __init__(self, memory: TextMemory, session_cache: SessionCache, security: SecurityManager, settings_manager=None):
        self.memory = memory
        self.session_cache = session_cache
        self.security = security
        self.settings_manager = settings_manager
        self._lock = asyncio.Lock()
        self._refactor_progress_callback = None

        self.handlers = {
            'retrieve': self._handle_retrieve,
            'store': self._handle_store,
            'store_record': self._handle_store_record,
            'search': self._handle_search,
            'list_memories': self._handle_list_memories,
            'ollama_chat': self._handle_ollama_chat,
            'backup': self._handle_backup,
            'restore': self._handle_restore,
            'activate': self._handle_activate,
            'deactivate': self._handle_deactivate,
            'suspend_timed': self._handle_suspend_timed,
            'force_resume': self._handle_force_resume,
            'clear_stm': self._handle_clear_stm,
            'clear_ltm': self._handle_clear_ltm,
            'set_mem_thresholds': self._handle_set_mem_thresholds,
            'status': self._handle_status,
            'get_dendrogram': self._handle_get_dendrogram,
            'get_temporal_evolution_map': self._handle_get_temporal_evolution_map,
            'get_memory_graph': self._handle_get_memory_graph,
            'get_center': self._handle_get_center,
            'update_center': self._handle_update_center,
            'delete_center': self._handle_delete_center,
            'batch_import': self._handle_batch_import,
            'export_product': self._handle_export_product,
            'generate_cognitive_report': self._handle_generate_cognitive_report,
            'refactor_memory': self._handle_refactor_memory,
        }

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def set_refactor_progress_callback(self, callback):
        """Sets the callback for reporting progress of the cognitive terrain rebuild."""
        self._refactor_progress_callback = callback

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def handle(self, message: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Main entry point – processes an incoming JSON command.

        Args:
            message: Deserialized JSON from the client.

        Returns:
            JSON response for the client.
        """
        # Calls from the dashboard:
        #   handle(msg)                              – full message dict
        #   handle(command='get_center', ...)        – kwargs only
        #   handle('get_dendrogram', command=..., ...) – positional + kwargs
        if isinstance(message, dict):
            msg = dict(message)
            msg.update(kwargs)
        elif message is None:
            msg = dict(kwargs)
        else:
            msg = dict(kwargs)
            msg['command'] = str(message)

        command = msg.get('command')
        if not isinstance(command, str) or not command:
            return self._error_response(INVALID_PARAMS, "Missing 'command' field.")

        # Module state check (active/deactivated/suspended)
        if self.security.is_operational_command(command):
            blocked = self.security.check_command_allowed(command)
            if blocked is not None:
                return {
                    'status': 'error',
                    'code': blocked.get('code', INTERNAL_ERROR),
                    'error': blocked.get('error', ''),
                }

        # Dispatch
        handler = self.handlers.get(command)
        if handler is None:
            logger.warning("Unknown command: '%s'", command)
            return self._error_response(UNKNOWN_COMMAND, f"Unknown command: '{command}'")

        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(msg)
            else:
                result = handler(msg)
            if result is None:
                result = self._error_response(INTERNAL_ERROR, 'Empty response')
            # the async helper might have returned a Future/coroutine
            while asyncio.isfuture(result) or asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception:
            logger.error("Command processing failed")
            return self._error_response(INTERNAL_ERROR, "Command processing failed.")

    # ------------------------------------------------------------------
    # Validation and helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _error_response(code: str, message: str) -> Dict[str, Any]:
        return {'status': 'error', 'code': code, 'error': message}

    @staticmethod
    def _success_response(**payload) -> Dict[str, Any]:
        result = {'status': 'success'}
        result.update(payload)
        return result

    @staticmethod
    def _text_param(
        msg: Dict[str, Any], field: str, max_chars: int, *, required: bool = True
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        value = msg.get(field)
        if value is None and not required:
            return None, None
        if not isinstance(value, str) or not value.strip():
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' must be a non-empty string."
            )
        if len(value) > max_chars:
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' exceeds the {max_chars} character limit."
            )
        return value, None

    @staticmethod
    def _integer_param(
        msg: Dict[str, Any], field: str, default: int, minimum: int, maximum: int
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        value = msg.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' must be an integer from {minimum} to {maximum}."
            )
        if value < minimum or value > maximum:
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' must be from {minimum} to {maximum}."
            )
        return value, None

    @staticmethod
    def _number_param(
        msg: Dict[str, Any], field: str, default: float, minimum: float, maximum: float
    ) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        value = msg.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' must be a number from {minimum} to {maximum}."
            )
        number = float(value)
        if not math.isfinite(number) or number < minimum or number > maximum:
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'{field}' must be from {minimum} to {maximum}."
            )
        return number, None

    @staticmethod
    def _layer_param(
        msg: Dict[str, Any], *, default: str = 'both', allow_both: bool = True
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        layer_value = msg.get('layer')
        source_value = msg.get('source')
        if layer_value is not None and source_value is not None:
            if str(layer_value).lower() != str(source_value).lower():
                return None, CommandHandler._error_response(
                    INVALID_PARAMS, "'layer' and 'source' must match when both are provided."
                )
        value = layer_value if layer_value is not None else source_value
        layer = default if value is None else str(value).lower()
        allowed = _LAYERS if allow_both else frozenset({'stm', 'ltm'})
        if layer not in allowed:
            choices = "'both', 'stm', or 'ltm'" if allow_both else "'stm' or 'ltm'"
            return None, CommandHandler._error_response(
                INVALID_PARAMS, f"'layer' must be {choices}."
            )
        return layer, None

    @staticmethod
    def _provenance_param(
        msg: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        provenance = msg.get('provenance')
        if provenance is None:
            return {}, None
        if not isinstance(provenance, dict):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'provenance' must be an object."
            )
        unexpected = sorted(set(provenance) - set(_PROVENANCE_LIMITS))
        if unexpected:
            return None, CommandHandler._error_response(
                INVALID_PARAMS,
                "'provenance' contains unsupported fields: " + ', '.join(unexpected),
            )
        try:
            encoded = json.dumps(provenance, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'provenance' must contain JSON values."
            )
        if len(encoded) > MAX_PROVENANCE_CHARS:
            return None, CommandHandler._error_response(
                INVALID_PARAMS,
                f"'provenance' exceeds the {MAX_PROVENANCE_CHARS} character limit.",
            )
        for field, limit in _PROVENANCE_LIMITS.items():
            value = provenance.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return None, CommandHandler._error_response(
                    INVALID_PARAMS,
                    f"'provenance.{field}' must be a non-empty string up to {limit} characters.",
                )
        return dict(provenance), None

    @staticmethod
    def _encode_cursor(layer: str, after: Tuple[str, str]) -> str:
        payload = json.dumps(
            {'version': 1, 'layer': layer, 'after': list(after)},
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        return base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

    @staticmethod
    def _decode_cursor(
        cursor: str, layer: str
    ) -> Tuple[Optional[Tuple[str, str]], Optional[Dict[str, Any]]]:
        try:
            padded = cursor + ('=' * (-len(cursor) % 4))
            raw = base64.b64decode(padded, altchars=b'-_', validate=True)
            decoded = json.loads(raw.decode('utf-8'))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'cursor' is invalid."
            )
        if not isinstance(decoded, dict) or decoded.get('version') != 1:
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'cursor' is invalid."
            )
        if decoded.get('layer') != layer:
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'cursor' does not match the requested layer."
            )
        after = decoded.get('after')
        if (
            not isinstance(after, list)
            or len(after) != 2
            or not all(isinstance(value, str) for value in after)
        ):
            return None, CommandHandler._error_response(
                INVALID_PARAMS, "'cursor' is invalid."
            )
        return (after[0], after[1]), None

    @staticmethod
    def _normalise_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """Returns a client-neutral record without exposing a center index as identity."""
        memory_id = record.get('memory_id')
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError('record is missing a stable memory_id')
        key = record.get('key')
        if key is None:
            key = record.get('key_text', '')
        value = record.get('value')
        if value is None:
            value = record.get('value_text', '')
        layer = str(record.get('layer') or record.get('source') or '').lower()
        if layer not in ('stm', 'ltm'):
            raise ValueError('record is missing a valid layer')
        result: Dict[str, Any] = {
            'memory_id': memory_id,
            'key': key or '',
            'value': value or '',
            'key_text': key or '',
            'value_text': value or '',
            'layer': layer,
            'source': layer,
            'provenance': dict(record.get('provenance') or {}),
        }
        if 'similarity' in record:
            result['similarity'] = float(record.get('similarity') or 0.0)
        if 'age' in record:
            result['age'] = int(record.get('age') or 0)
        if 'usage' in record:
            result['usage'] = int(record.get('usage') or 0)
        if 'intensity' in record or 'h' in record:
            result['intensity'] = float(
                record.get('intensity', record.get('h', 0.0)) or 0.0
            )
        if 'trusted' in record:
            result['trusted'] = bool(record.get('trusted'))
        return result

    # ------------------------------------------------------------------
    # retrieve / store (session cache flow)
    # ------------------------------------------------------------------

    def _handle_retrieve(self, msg: Dict) -> Dict:
        """Recall from memory.

        Flow:
        1. Take the user query from msg
        2. Store the query in the session cache under session_id
        3. Call TextMemory for semantic search
        4. Return the results as JSON
        """
        query, error = self._text_param(msg, 'query', MAX_KEY_CHARS)
        if error:
            return error
        session_id, error = self._text_param(
            msg, 'session_id', MAX_SESSION_ID_CHARS
        )
        if error:
            return error
        top_k, error = self._integer_param(msg, 'top_k', 5, 1, 20)
        if error:
            return error

        # Store the query in the session cache under session_id
        metadata: Dict[str, Any] = {}
        if msg.get('emotion') is not None:
            metadata['emotion'] = msg.get('emotion')
        self.session_cache.store(session_id, query, metadata=metadata)

        memories = self._retrieve_memories(query, top_k=top_k, increment_stats=True)

        return self._success_response(
            session_id=session_id,
            query=query,
            memories=memories,
            count=len(memories),
        )

    def _handle_store(self, msg: Dict) -> Dict:
        """Store into memory with the two-summary format.

        New format (|STPAM| user_summary |MIDPAM| model_summary |ENDPAM|):
            - user_summary: Semantic reduction of the user's query (keywords, intent)
            - model_summary: Summary of the model's response (key points, actions)

        Lossless browser format:
            - response_text: Cleaned visible model response without PAM tokens
            - key = original query from the session cache
            - value = response_text

        Summary-only keying logic (backward compatibility):
            - If the original query is <= 20 words → key = original query (more precise)
            - If the original query is > 20 words → key = user_summary from the LLM (reduction)
            - value = model_summary

        Backward compatibility:
            - The old format with a `summary` field still works (as model_summary)
        """
        session_id = msg.get('session_id')
        if not session_id:
            return self._error_response(INVALID_PARAMS, "Missing 'session_id'.")

        # model_summary (new format) or legacy 'summary'
        model_summary = msg.get('model_summary') or msg.get('summary')
        if not model_summary:
            return self._error_response(
                INVALID_PARAMS, "Missing 'model_summary' (or legacy 'summary')."
            )
        user_summary = msg.get('user_summary')
        response_text, error = self._text_param(
            msg, 'response_text', MAX_VALUE_CHARS, required=False
        )
        if error:
            return error
        provenance, error = self._provenance_param(msg)
        if error:
            return error

        # Pair with the original query from the session cache (consumes the entry)
        query = self.session_cache.consume(session_id)
        if query is None:
            logger.warning("Session '%s' expired or does not exist.", session_id)
            return self._error_response(
                SESSION_EXPIRED, f"Session '{session_id}' expired or does not exist."
            )

        if response_text is not None:
            key = query
            key_source = 'query'
            value = response_text
            value_source = 'response_text'
        else:
            words = len(str(query).split())
            if words <= self.QUERY_WORD_THRESHOLD:
                key = query
                key_source = 'query'
            else:
                key = user_summary or query
                key_source = 'user_summary'
            value = model_summary
            value_source = 'model_summary'

        logger.info(
            "STORE: key_source=%s, value_source=%s, key_length=%d, value_length=%d",
            key_source,
            value_source,
            len(str(key)),
            len(str(value)),
        )

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    new_centers = self.memory.store(
                        key, value, provenance=provenance
                    )
                except Exception:
                    logger.error("STORE: memory write failed")
                    return self._error_response(WRITE_ERROR, "Memory write failed.")
                try:
                    self.memory.save()
                    logger.info("💾 Autosave completed successfully after STORE command")
                except Exception:
                    logger.error("STORE: memory save failed")
            return self._success_response(
                key=key,
                key_source=key_source,
                value=value,
                value_source=value_source,
                new_centers=new_centers,
            )

        return asyncio.ensure_future(_do())

    def _handle_store_record(self, msg: Dict) -> Dict:
        """Stores one explicit client-neutral record with local provenance."""
        key, error = self._text_param(msg, 'key', MAX_KEY_CHARS)
        if error:
            return error
        value, error = self._text_param(msg, 'value', MAX_VALUE_CHARS)
        if error:
            return error
        memory_id, error = self._text_param(msg, 'memory_id', 128, required=False)
        if error:
            return error
        provenance, error = self._provenance_param(msg)
        if error:
            return error
        intensity, error = self._number_param(msg, 'intensity', 1.0, 0.000001, 10.0)
        if error:
            return error
        surprise, error = self._number_param(msg, 'surprise', 0.0, 0.0, 1.0)
        if error:
            return error
        age, error = self._integer_param(msg, 'age', 0, 0, 2_147_483_647)
        if error:
            return error

        emotion = msg.get('emotion')
        if emotion is not None:
            try:
                encoded_emotion = json.dumps(
                    emotion, ensure_ascii=False, separators=(',', ':')
                )
            except (TypeError, ValueError):
                return self._error_response(
                    INVALID_PARAMS, "'emotion' must contain JSON values."
                )
            if len(encoded_emotion) > MAX_PROVENANCE_CHARS:
                return self._error_response(
                    INVALID_PARAMS,
                    f"'emotion' exceeds the {MAX_PROVENANCE_CHARS} character limit.",
                )

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    record = self.memory.store_record(
                        key,
                        value,
                        emotion=emotion,
                        intensity=intensity,
                        surprise=surprise,
                        age=age,
                        memory_id=memory_id,
                        provenance=provenance,
                    )
                except Exception:
                    logger.exception("store_record: memory write failed")
                    return self._error_response(WRITE_ERROR, 'Memory write failed.')

                if not isinstance(record, dict):
                    return self._error_response(
                        WRITE_ERROR, 'Memory core returned an invalid write result.'
                    )
                record_status = record.get('status')
                if record_status not in ('created', 'reinforced'):
                    if record_status == 'duplicate_memory_id':
                        return self._error_response(
                            DUPLICATE_MEMORY_ID,
                            'The supplied memory_id is already assigned to a different record.',
                        )
                    if record_status == 'capacity_exhausted':
                        return self._error_response(WRITE_ERROR, 'Memory capacity is exhausted.')
                    return self._error_response(WRITE_ERROR, 'Memory did not store the record.')
                try:
                    self.memory.save()
                except Exception:
                    logger.exception("store_record: save failed")
                    return self._error_response(SAVE_FAILED, 'Memory save failed.')

            payload = dict(record)
            payload['record_status'] = payload.pop('status')
            return self._success_response(**payload)

        return asyncio.ensure_future(_do())

    def _handle_search(self, msg: Dict) -> Dict:
        """Performs administrative semantic search without updating recall statistics."""
        query, error = self._text_param(msg, 'query', MAX_KEY_CHARS)
        if error:
            return error
        top_k, error = self._integer_param(msg, 'top_k', 10, 1, 50)
        if error:
            return error
        layer, error = self._layer_param(msg)
        if error:
            return error

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    raw_results = self.memory.search(query, top_k=top_k, source=layer)
                    results = [self._normalise_record(record) for record in raw_results]
                except Exception:
                    logger.exception("search: memory access failed")
                    return self._error_response(
                        MEMORY_ACCESS_ERROR, 'Memory search failed.'
                    )
            return self._success_response(
                query=query,
                layer=layer,
                results=results,
                count=len(results),
            )

        return asyncio.ensure_future(_do())

    def _handle_list_memories(self, msg: Dict) -> Dict:
        """Lists records in deterministic stable-ID order with opaque pagination."""
        layer, error = self._layer_param(msg)
        if error:
            return error
        limit, error = self._integer_param(msg, 'limit', 20, 1, 100)
        if error:
            return error

        cursor = msg.get('cursor')
        after: Optional[Tuple[str, str]] = None
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS:
                return self._error_response(
                    INVALID_PARAMS,
                    f"'cursor' must be a non-empty string up to {MAX_CURSOR_CHARS} characters.",
                )
            after, error = self._decode_cursor(cursor, layer)
            if error:
                return error

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    raw_records = self.memory.list_memories(
                        source=layer, limit=1_000_000
                    )
                    records = [self._normalise_record(record) for record in raw_records]
                except Exception:
                    logger.exception("list_memories: memory access failed")
                    return self._error_response(
                        MEMORY_ACCESS_ERROR, 'Memory listing failed.'
                    )

            records.sort(key=lambda record: (record['layer'], record['memory_id']))
            start = 0
            if after is not None:
                keys = [(record['layer'], record['memory_id']) for record in records]
                try:
                    start = keys.index(after) + 1
                except ValueError:
                    return self._error_response(
                        INVALID_PARAMS, "'cursor' no longer identifies a record."
                    )

            page = records[start:start + limit]
            has_more = start + len(page) < len(records)
            next_cursor = None
            if has_more and page:
                last = page[-1]
                next_cursor = self._encode_cursor(
                    layer, (last['layer'], last['memory_id'])
                )
            return self._success_response(
                layer=layer,
                records=page,
                count=len(page),
                total=len(records),
                has_more=has_more,
                next_cursor=next_cursor,
            )

        return asyncio.ensure_future(_do())

    # ------------------------------------------------------------------
    # Ollama chat (pre_enriched = True/False, PAM tokens)
    # ------------------------------------------------------------------

    def _handle_ollama_chat(self, msg: Dict) -> Dict:
        """Conversation cycle via a local Ollama model.

        Supports two modes, selected by the 'pre_enriched' field in msg:

        pre_enriched=True  (NEW – default from web client):
            The prompt was already built by ConversationHandler._buildEnrichedPrompt()
            on the frontend, identical to Gemini/Claude/ChatGPT prompts, including:
              - Conversation history (context window trimmed)
              - Associated memory context retrieved by the client via biomem.retrieve()
              - Deep Recall instructions / round-2 results (when applicable)
              - PAM token instructions (same wording as for cloud models)
              - |TITLE| instruction on the first turn
            Server responsibility:
              - Run homeostasis (memory.step)
              - Call Ollama API with the received prompt as-is
              - Return raw Ollama response text to client
              - Client handles PAM parsing  (_parseLLMResponse) and biomem store

        pre_enriched=False (LEGACY – backward compatibility):
            Server handles the full cycle: retrieve, enrich, call Ollama, parse
            PAM tokens, store to memory, return display_text + thread_title.
        """
        try:
            url = normalize_ollama_base_url(msg.get('url') or DEFAULT_OLLAMA_URL)
        except ValueError:
            return self._error_response(
                INVALID_PARAMS,
                'Ollama must use an HTTP loopback URL with an explicit port.',
            )
        model_name = msg.get('model_name')
        if not model_name:
            return self._error_response(INVALID_PARAMS, "Missing 'model_name'.")
        request_prompt = msg.get('prompt')
        if not request_prompt:
            return self._error_response(INVALID_PARAMS, "Missing 'prompt'.")

        pre_enriched = bool(msg.get('pre_enriched', True))
        session_id = msg.get('session_id')

        timeout_sec = OLLAMA_TIMEOUT
        if self.settings_manager is not None:
            try:
                timeout_min = self.settings_manager.get_ollama_timeout_min()
                if timeout_min:
                    timeout_sec = int(timeout_min) * 60
            except Exception:
                logger.warning("OLLAMA_CHAT: using the default local timeout")

        async def _run() -> Dict[str, Any]:
            # Homeostasis before each conversation cycle
            try:
                self.memory.step()
            except Exception:
                pass

            system_prompt = ''
            ollama_prompt = request_prompt
            if not pre_enriched:
                try:
                    history = msg.get('history') or []
                    memories = msg.get('memories')
                    if memories is None:
                        memories = self._retrieve_memories(
                            request_prompt, top_k=5, increment_stats=True
                        )
                    ollama_prompt = self._build_ollama_prompt(
                        request_prompt, history, memories, context_limit=250
                    )
                except Exception:
                    logger.error("OLLAMA_CHAT: local prompt enrichment failed")
                    return self._error_response(
                        OLLAMA_ERROR, "Could not prepare the local Ollama prompt."
                    )

            logger.info("OLLAMA_CHAT: local request started")
            try:
                response_text = await self._call_ollama_api(
                    url,
                    model_name,
                    ollama_prompt,
                    system_prompt=system_prompt,
                    timeout=timeout_sec,
                )
            except urllib.error.URLError:
                logger.error("OLLAMA_CHAT: local Ollama server unavailable")
                return self._error_response(
                    OLLAMA_UNAVAILABLE,
                    "Could not connect to the local Ollama server. Check that Ollama is running.",
                )
            except asyncio.TimeoutError:
                logger.error("OLLAMA_CHAT: Ollama timeout (%d min.)", timeout_sec // 60)
                return self._error_response(
                    OLLAMA_TIMEOUT, f"The Ollama server did not respond within {timeout_sec // 60} minutes."
                )
            except Exception:
                logger.error("OLLAMA_CHAT: local communication error")
                return self._error_response(
                    OLLAMA_ERROR, "Communication with local Ollama failed."
                )

            if pre_enriched:
                # The client parses the PAM tokens itself (see _parseLLMResponse on the frontend)
                return self._success_response(response=response_text, raw_response=response_text)

            # LEGACY flow: parse PAM → store → display_text + thread_title
            try:
                display_text, user_summary, model_summary, thread_title = (
                    self._parse_pam_tokens(response_text)
                )
            except Exception:
                logger.error("OLLAMA_CHAT: local response parsing failed")
                return self._error_response(
                    OLLAMA_ERROR, "Could not process the local Ollama response."
                )
            if user_summary is None and model_summary is None:
                logger.warning("OLLAMA_CHAT: Model did not return PAM tokens, memory not stored")
                return self._success_response(
                    display_text=display_text or response_text,
                    thread_title=thread_title,
                )

            stored_key = None
            if session_id:
                try:
                    cached_query = self.session_cache.retrieve(session_id)
                except Exception:
                    logger.error("OLLAMA_CHAT: local session lookup failed")
                    cached_query = None
                if cached_query is not None:
                    if len(str(cached_query).split()) <= self.QUERY_WORD_THRESHOLD:
                        stored_key = cached_query
                    else:
                        stored_key = user_summary or cached_query
            if stored_key is None:
                stored_key = user_summary or request_prompt

            try:
                async with self.lock:
                    self.memory.store(stored_key, model_summary or display_text)
                    try:
                        self.memory.save()
                        logger.info(
                            "💾 Autosave completed successfully after ollama_chat"
                        )
                    except Exception as error:
                        logger.error(
                            "❌ Autosave failed after ollama_chat (%s)",
                            type(error).__name__,
                        )
            except Exception as error:
                logger.error(
                    "OLLAMA_CHAT STORE: failed (%s)", type(error).__name__
                )
                return self._error_response(
                    OLLAMA_ERROR, "Could not store the local Ollama memory."
                )

            logger.info("OLLAMA_CHAT STORE: memory stored")
            return self._success_response(
                display_text=display_text or response_text,
                thread_title=thread_title,
                stored_key=stored_key,
                stored=True,
            )

        return asyncio.ensure_future(_run())

    def _build_ollama_prompt(self, query: str, history: List[Dict], memories: List[Dict], context_limit: int = 250) -> str:
        """Builds the enriched prompt for the Ollama model.

        Mirrors the _buildEnrichedPrompt logic from conversation.js on the frontend.
        Includes conversation history, the current query, memory context
        and PAM token instructions. Trims history to context_limit words.
        """
        history_text = self._build_history_text(history, context_limit)

        memory_text = "\n".join(self._format_memory(m) for m in memories) if memories else ""

        history_block = (
            _RECENT_HISTORY_OPEN
            + str(context_limit)
            + _RECENT_HISTORY_SUFFIX
            + "\n"
            + history_text
            + _RECENT_HISTORY_CLOSE
        )

        memory_block = _MEMORY_CONTEXT_OPEN
        if memory_text:
            memory_block += "\n" + memory_text

        prompt = (
            MEMORY_SUMMARY_PROMPT
            + history_block
            + _NEW_THREAD_PROMPT
            + memory_block
            + f"\nUser: {query}\n"
        )
        return prompt

    def _build_history_text(self, history: List[Dict], context_limit: int) -> str:
        """History text in User:/Model: format, trimmed to a word limit."""
        lines: List[str] = []
        total_words = 0
        for item in history or []:
            if not isinstance(item, dict):
                continue
            role = item.get('role', 'user') or 'user'
            content = item.get('content') or item.get('text') or item.get('message') or ''
            if role in ('assistant', 'model', 'bot'):
                line = f"Model: {content}"
            else:
                line = f"User: {content}"
            lines.append(line)
            total_words += len(str(content).split())

        # Trims history to context_limit words – cut from the start (oldest messages)
        if total_words > context_limit and lines:
            acc = 0
            trimmed: List[str] = []
            for line in reversed(lines):
                words = len(str(line).split())
                if acc + words > context_limit:
                    break
                trimmed.append(line)
                acc += words
            lines = list(reversed(trimmed))
        return "\n".join(lines)

    def _format_memory(self, memory: Dict[str, Any]) -> str:
        """Formats a single memory into the prompt (User/Model/Confidence/Turn distance).

        Exact block alignment:
        'User: ', 'Model: ', 'Confidence: ', 'Turn distance: ', '(pre-restore backup: ').
        """
        parts = [
            _MEMORY_FORMAT_USER % memory.get('user', ''),
            _MEMORY_FORMAT_MODEL % memory.get('model', ''),
            _MEMORY_FORMAT_CONFIDENCE % float(memory.get('confidence', 0.0) or 0.0),
            _MEMORY_FORMAT_TURN % int(memory.get('turn_distance', 0) or 0),
        ]
        if memory.get('source') == 'pre_restore_backup':
            parts.append(_MEMORY_FORMAT_PRE_RESTORE % (memory.get('key_text', '') or memory.get('user', '')))
        return "\n".join(parts)

    async def _call_ollama_api(self, url: str, model_name: str, prompt: str, system_prompt: str = '', timeout: int = OLLAMA_TIMEOUT) -> str:
        """Asynchronous Ollama API call via urllib (no external dependencies).

        Uses the /api/chat endpoint for better multi-turn context understanding.
        Timeout: OLLAMA_TIMEOUT (600s / 10 min) – synced with the frontend
        timeout in bdbm-client.js.
        """
        base_url = normalize_ollama_base_url(url)
        endpoint = base_url + '/api/chat'

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})

        payload = json.dumps({
            'model': model_name,
            'messages': messages,
            'stream': False,
        }).encode('utf-8')

        return await self._post_json(endpoint, payload, timeout)

    async def _post_json(self, endpoint: str, payload: bytes, timeout: int) -> str:
        """Blocking urllib POST in an executor (asyncio)."""
        loop = asyncio.get_event_loop()

        def _request() -> str:
            class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirectHandler()
            )
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            message = data.get('message', {})
            if isinstance(message, dict):
                return message.get('content', '') or data.get('response', '')
            return data.get('response', '')

        return await loop.run_in_executor(None, _request)

    def _parse_pam_tokens(self, response_text: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """Parses PAM tokens from the Ollama model's response.

        Mirrors the _parseLLMResponse logic from conversation.js on the frontend.

        Returns:
            Tuple: (display_text, user_summary, model_summary, thread_title)
        """
        text = response_text or ''
        if '|STPAM|' not in text:
            return text, None, None, None

        stpam_pos = text.find('|STPAM|')
        display_text = text[:stpam_pos].strip()

        rest = text[stpam_pos + len('|STPAM|'):]
        midpam_pos = rest.find('|MIDPAM|')
        endpam_pos = rest.find('|ENDPAM|')

        user_summary = None
        model_summary = None
        thread_title = None

        if midpam_pos != -1:
            user_summary = rest[:midpam_pos].strip()
            tail = rest[midpam_pos + len('|MIDPAM|'):]
            end_in_tail = tail.find('|ENDPAM|')
            if end_in_tail != -1:
                model_summary = tail[:end_in_tail].strip()
                after = tail[end_in_tail + len('|ENDPAM|'):]
            else:
                model_summary = tail.strip()
                after = ''
        else:
            if endpam_pos != -1:
                model_summary = rest[:endpam_pos].strip()
                after = rest[endpam_pos + len('|ENDPAM|'):]
            else:
                after = ''

        title_pos = after.find('|TITLE|')
        if title_pos != -1:
            thread_title = after[title_pos + len('|TITLE|'):].strip()

        return display_text, user_summary, model_summary, thread_title

    # ------------------------------------------------------------------
    # Memory recall
    # ------------------------------------------------------------------

    def _retrieve_memories(self, query: str, top_k: int = 5, increment_stats: bool = True) -> list:
        """Retrieves relevant memories from the memory core.

        Returns:
            List[Dict] with keys: user, model, turn_distance, confidence, source
        """
        try:
            result = self.memory.recall(query, top_k=top_k, increment_stats=increment_stats)
        except Exception:
            logger.error("retrieve: memory recall failed")
            return []

        matches = getattr(result, 'matches', None) or []
        memories: List[Dict[str, Any]] = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            key_text = m.get('key', '') or m.get('key_text', '') or ''
            value_text = m.get('value', '') or m.get('value_text', '') or ''
            weight = m.get('weight')
            if weight is None:
                weight = m.get('confidence', 0.0) or 0.0
            source = str(m.get('layer') or m.get('source') or 'ltm').lower()
            memory_id = m.get('memory_id')
            provenance = dict(m.get('provenance') or {})
            memories.append({
                'user': key_text,
                'model': value_text,
                'key': key_text,
                'value': value_text,
                'turn_distance': self._calculate_turn_distance(source, key_text),
                'confidence': float(weight),
                'source': source,
                'layer': source,
                'memory_id': memory_id,
                'provenance': provenance,
            })

        return self.filter_duplicate_memories(memories)

    def _calculate_turn_distance(self, source: str, key_text: str) -> int:
        """Computes the turn distance – how many interactions since the memory was stored.

        Uses the center's `age` and the system's `step_count`.
        """
        try:
            stats = self.memory.get_stats()
            step_count = int(stats.get('step_count', 0) or 0)
        except Exception:
            step_count = 0

        age = 0
        try:
            entries = self.memory.search(key_text, top_k=1, source='both')
            if entries:
                age = int(entries[0].get('age', 0) or 0)
        except Exception:
            age = 0

        distance = step_count - age
        return distance if distance > 0 else 0

    @staticmethod
    def filter_duplicate_memories(memories: list) -> list:
        seen_pairs = set()
        result = []
        for m in memories:
            pair_key = (m.get('user', ''), m.get('model', ''))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            result.append(m)
        return result

    # ------------------------------------------------------------------
    # Module state
    # ------------------------------------------------------------------

    def _handle_activate(self, msg: Dict) -> Dict:
        """Activates the module."""
        state = self.security.activate()
        return self._success_response(state=state)

    def _handle_deactivate(self, msg: Dict) -> Dict:
        """Deactivates the module."""
        state = self.security.deactivate()
        return self._success_response(state=state)

    def _handle_suspend_timed(self, msg: Dict) -> Dict:
        """Suspends the module for a given period.

        Args (msg):
            duration: int - suspension time in seconds
        """
        duration = msg.get('duration')
        if not isinstance(duration, (int, float)) or duration <= 0:
            return self._error_response(
                INVALID_PARAMS, "Parameter 'duration' must be a positive number (seconds)."
            )
        info = self.security.suspend_timed(int(duration))
        logger.info("SUSPEND_TIMED: duration=%s", duration)
        return self._success_response(**info)

    def _handle_force_resume(self, msg: Dict) -> Dict:
        """Immediately resumes the module from the SUSPENDED state.

        Also overrides an active suspension – administrative override.
        """
        state = self.security.force_resume()
        logger.info("FORCE_RESUME: state=%s", state)
        return self._success_response(state=state)

    def _handle_status(self, msg: Dict) -> Dict:
        """Returns the module status."""
        info: Dict[str, Any] = {
            'state': self.security.state,
            'is_active': self.security.is_active,
            'is_deactivated': self.security.is_deactivated,
            'is_suspended': self.security.is_suspended,
            'sessions_active': self.session_cache.get_active_count(),
        }
        if self.security.state == 'SUSPENDED':
            suspend_info = self.security.get_suspend_info()
            if suspend_info:
                info['suspend_info'] = suspend_info
        try:
            info['memory_stored'] = True
            info['stats'] = self.memory.get_stats()
        except Exception:
            pass
        return self._success_response(**info)

    def _handle_clear_stm(self, msg: Dict) -> Dict:
        """Clears short-term memory (STM)."""

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                n = self._clear_centers('stm')
                logger.info("CLEAR_STM: deleted %d", n)
                try:
                    self.memory.save()
                    logger.info("💾 CLEAR_STM: state saved to disk")
                except Exception as e:
                    logger.error("❌ CLEAR_STM: save error: %s", e)
                    return self._error_response(SAVE_FAILED, str(e))
            return self._success_response(cleared=n)

        return asyncio.ensure_future(_do())

    def _handle_clear_ltm(self, msg: Dict) -> Dict:
        """Clears the entire memory (STM + LTM)."""

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    self.memory.reset()
                    logger.info("CLEAR_LTM: entire memory reset")
                except Exception as e:
                    logger.error("❌ CLEAR_LTM: save error: %s", e)
                    return self._error_response(SAVE_FAILED, str(e))
                try:
                    self.memory.save()
                    logger.info("💾 CLEAR_LTM: state saved to disk")
                except Exception as e:
                    logger.error("❌ CLEAR_LTM: save error: %s", e)
                    return self._error_response(SAVE_FAILED, str(e))
            return self._success_response(cleared=True)

        return asyncio.ensure_future(_do())

    def _clear_centers(self, memory_type: str) -> int:
        """Zeroes the STM/LTM buffer (active/h/texts), returns the count."""
        centers = self.memory.stm_centers if memory_type == 'stm' else self.memory.ltm_centers
        if centers is None:
            return 0
        n = 0
        active = getattr(centers, 'active', None)
        if active is not None:
            try:
                n = int(active.sum().item()) if hasattr(active, 'sum') else len(active)
            except Exception:
                n = 0
        if hasattr(centers, 'reset'):
            centers.reset()
        return n

    def _handle_set_mem_thresholds(self, msg: Dict) -> Dict:
        """Sets stm_new_center_threshold and ltm_new_center_threshold
        on the live MemoryConfig object of the running server.

        Called exclusively from the Dashboard (Advanced Settings sliders).
        """

        def _clamp(value: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, float(value)))

        applied: List[str] = []
        config = self.memory.config

        stm_value = msg.get('stm_new_center_threshold')
        ltm_value = msg.get('ltm_new_center_threshold')
        max_assoc = msg.get('max_associations')

        if stm_value is not None:
            try:
                v = _clamp(stm_value, 0.25, 0.85)
                config.stm_new_center_threshold = v
                applied.append(f"stm_new_center_threshold={v:.2f}")
            except (TypeError, ValueError):
                return self._error_response(
                    INVALID_PARAMS, "Invalid value for 'stm_new_center_threshold'."
                )

        if ltm_value is not None:
            try:
                v = _clamp(ltm_value, 0.25, 0.85)
                config.ltm_new_center_threshold = v
                applied.append(f"ltm_new_center_threshold={v:.2f}")
            except (TypeError, ValueError):
                return self._error_response(
                    INVALID_PARAMS, "Invalid value for 'ltm_new_center_threshold'."
                )

        if max_assoc is not None:
            try:
                v = int(_clamp(max_assoc, 3, 10))
                config.max_associations = v
                applied.append(f"max_associations={v}")
            except (TypeError, ValueError):
                return self._error_response(INVALID_PARAMS, "Invalid value for 'max_associations'.")

        if not applied:
            logger.info("[AdvancedSettings] set_mem_thresholds called with no changes")
            return self._success_response(applied=[])

        logger.info("[AdvancedSettings] Applied live config changes: %s", ", ".join(applied))
        return self._success_response(applied=applied)

    # ------------------------------------------------------------------
    # Backups / restore / export
    # ------------------------------------------------------------------

    def _handle_backup(self, msg: Dict) -> Dict:
        """Creates a backup of the memory state.

        Backups use the portable .bdbm representation.
        """
        path = msg.get('path')
        try:
            backup_path = self.memory.backup(path)
        except Exception as e:
            logger.error("BACKUP: %s", e)
            return self._error_response(SAVE_FAILED, str(e))
        return self._success_response(path=backup_path, status_message='OK')

    def _handle_restore(self, msg: Dict) -> Dict:
        """Restores the memory state from a backup."""
        path = msg.get('path')
        if not path:
            return self._error_response(INVALID_PARAMS, "Missing 'path'.")

        path = Path(path)
        if not path.exists():
            return self._error_response(FILE_NOT_FOUND, f"Backup not found: {path}")

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                try:
                    self.memory.restore(str(path))
                except Exception as e:
                    logger.error("RESTORE: %s", e)
                    return self._error_response(READ_ERROR, str(e))
            return self._success_response(restored=True, path=str(path))

        return asyncio.ensure_future(_do())

    def _handle_export_product(self, msg: Dict) -> Dict:
        """Exports the memory into a portable .bdbm file.

        The user chooses the destination via the dashboard FileDialog.
        The file is always portable (unencrypted ZIP with BDBMZIP01 header).

        """
        path = msg.get('path')
        if not path:
            return self._error_response(INVALID_PARAMS, "Missing 'path'.")
        try:
            self.memory.save(path=path)
        except Exception as e:
            logger.error("EXPORT_PRODUCT: %s", e)
            return self._error_response(EXPORT_ERROR, str(e))
        return self._success_response(path=path, exported=True)

    def _handle_generate_cognitive_report(self, msg: Dict) -> Dict:
        """Generates a purely algorithmic cognitive audit PDF at the given path."""
        path = msg.get('path')
        if not path:
            return self._error_response(INVALID_PARAMS, "Missing 'path'.")

        async def _do() -> Dict[str, Any]:
            try:
                analyzer = CognitiveAuditAnalyzer(self.memory)
                report = analyzer.analyze()
                generator = CognitiveReportPDFGenerator()
                generator.generate_pdf(str(path), report)
            except Exception as e:
                logger.error("generate_cognitive_report: error: %s", e)
                return self._error_response(
                    READ_ERROR, f"PDF rendering failed (PyQt6 error). {e}"
                )
            return self._success_response(path=str(path), generated=True)

        return asyncio.ensure_future(_do())

    def _handle_refactor_memory(self, msg: Dict) -> Dict:
        """Performs a full rebuild of the cognitive terrain (re-writing all records)."""

        async def _do() -> Dict[str, Any]:
            async with self.lock:
                logger.info("REFACTOR_MEMORY: cognitive terrain rebuild started")
                try:
                    result = self.memory.refactor(progress_callback=self._refactor_progress_callback)
                except Exception as e:
                    logger.error("REFACTOR_MEMORY: failure — %s", e)
                    return self._error_response(COMPUTE_ERROR, str(e))
            logger.info("REFACTOR_MEMORY: completed — %s", result)
            return self._success_response(result=result)

        return asyncio.ensure_future(_do())

    # ------------------------------------------------------------------
    # Centers (LTM/STM) – read, update, delete
    # ------------------------------------------------------------------

    def _get_target_centers(self, msg: Dict):
        """Returns the target centers (LTM or STM) based on the 'memory_type' parameter in the message."""
        memory_type = str(msg.get('memory_type', 'ltm')).lower()
        if memory_type == 'stm':
            return self.memory.stm_centers
        return self.memory.ltm_centers

    def _handle_get_center(self, msg: Dict) -> Dict:
        """Returns details of a single LTM or STM center (texts, intensity, usage)."""
        index = msg.get('index')
        if index is None:
            return self._error_response(MISSING_INDEX, "Missing 'index' field.")
        index = int(index)

        centres = self._get_target_centers(msg)
        memory_type = str(msg.get('memory_type', 'ltm')).lower()

        async def _read() -> Dict[str, Any]:
            try:
                n = len(centres.key_text) if hasattr(centres, 'key_text') else 0
                if index < 0 or index >= n:
                    logger.error("get_center: read error: out of range (0..%d)", n)
                    return self._error_response(
                        OUT_OF_RANGE, f"index out of range (0..{n})"
                    )
                key_text = centres.key_text[index]
                value_text = centres.value_text[index]
                h_val = centres.h[index]
                h = float(h_val.item()) if hasattr(h_val, 'item') else float(h_val)
                usage_val = centres.usage[index]
                usage = int(usage_val.item()) if hasattr(usage_val, 'item') else int(usage_val)
                return self._success_response(
                    index=index,
                    key_text=key_text or '',
                    value_text=value_text or '',
                    h=h,
                    usage=usage,
                    memory_type=memory_type,
                )
            except Exception as e:
                logger.error("get_center: read error: %s", e)
                return self._error_response(READ_ERROR, str(e))

        return asyncio.ensure_future(_read())

    def _handle_update_center(self, msg: Dict) -> Dict:
        """Updates the center texts and immediately saves the state to disk."""
        index = msg.get('index')
        if index is None:
            return self._error_response(MISSING_INDEX, "Missing 'index' field.")
        index = int(index)
        key_text = msg.get('key_text', '')
        value_text = msg.get('value_text', '')

        centres = self._get_target_centers(msg)
        memory_type = str(msg.get('memory_type', 'ltm')).lower()

        def _write() -> Dict[str, Any]:
            try:
                centres.key_text[index] = key_text
                centres.value_text[index] = value_text
                self.memory.save()
            except Exception as e:
                logger.error("update_center: write error: %s", e)
                return self._error_response(WRITE_ERROR, str(e))
            logger.info("update_center (%s): center %d saved.", memory_type, index)
            return self._success_response(index=index, updated=True, memory_type=memory_type)

        return asyncio.ensure_future(asyncio.to_thread(_write))

    def _handle_delete_center(self, msg: Dict) -> Dict:
        """Deletes a center (active=False, h=0, texts=None) and saves to disk."""
        index = msg.get('index')
        if index is None:
            return self._error_response(MISSING_INDEX, "Missing 'index' field.")
        index = int(index)

        centres = self._get_target_centers(msg)
        memory_type = str(msg.get('memory_type', 'ltm')).lower()

        def _delete() -> Dict[str, Any]:
            try:
                # Delete: active=False, h=0, texts=None
                if hasattr(centres, 'active') and index < len(centres.active):
                    centres.active[index] = False
                if hasattr(centres, 'h') and index < len(centres.h):
                    centres.h[index] = 0.0
                if hasattr(centres, 'key_text') and index < len(centres.key_text):
                    centres.key_text[index] = None
                if hasattr(centres, 'value_text') and index < len(centres.value_text):
                    centres.value_text[index] = None
                if hasattr(centres, 'texts') and index < len(centres.texts):
                    centres.texts[index] = None
                self.memory.save()
            except Exception as e:
                logger.error("delete_center: delete error: %s", e)
                return self._error_response(DELETE_ERROR, str(e))
            logger.info("delete_center (%s): center %d deleted.", memory_type, index)
            return self._success_response(index=index, deleted=True, memory_type=memory_type)

        return asyncio.ensure_future(asyncio.to_thread(_delete))

    # ------------------------------------------------------------------
    # Dendrogram / timeline / graph
    # ------------------------------------------------------------------

    @staticmethod
    def _active_projection_records(centres) -> Dict[str, Any]:
        """Return projection fields aligned to active center indices."""
        active = centres.active.bool() if hasattr(centres.active, 'bool') else centres.active
        indices = [int(index) for index in active.nonzero(as_tuple=True)[0].tolist()]
        key_store = getattr(centres, 'key_texts', [])
        value_store = getattr(centres, 'value_texts', [])
        memory_id_store = getattr(centres, 'memory_ids', [])
        provenance_store = getattr(centres, 'provenances', [])

        def _number(values, index, cast):
            value = values[index]
            if hasattr(value, 'item'):
                value = value.item()
            return cast(value)

        key_texts = [
            (key_store[index] or '') if index < len(key_store) else ''
            for index in indices
        ]
        value_texts = [
            (value_store[index] or '') if index < len(value_store) else ''
            for index in indices
        ]
        memory_ids = [
            memory_id_store[index] if index < len(memory_id_store) else None
            for index in indices
        ]
        provenances = [
            dict(provenance_store[index] or {})
            if index < len(provenance_store)
            else {}
            for index in indices
        ]
        intensities = [_number(centres.h, index, float) for index in indices]
        usages = [_number(centres.usage, index, int) for index in indices]
        ages = [_number(centres.age, index, float) for index in indices]

        return {
            'indices': indices,
            'key_texts': key_texts,
            'value_texts': value_texts,
            'memory_ids': memory_ids,
            'provenances': provenances,
            'intensities': intensities,
            'usages': usages,
            'ages': ages,
            'n_points': len(indices),
            'n_active': len(indices),
            'n_active_flag': len(indices),
            'n_h_positive': sum(1 for intensity in intensities if intensity > 0),
            'n_texts': sum(1 for text in key_texts if text),
        }

    def _empty_projection_error(
        self, records: Dict[str, Any], memory_type: str
    ) -> Dict[str, Any]:
        result = self._error_response(NOT_ENOUGH_DATA, "No active center found.")
        result.update(records)
        result['response_version'] = 2
        result['memory_type'] = memory_type
        return result

    def _handle_get_dendrogram(self, msg: Dict) -> Dict:
        """Returns the linkage matrix for a dendrogram from LTM or STM centers with real content.

        Uses Ward's hierarchical clustering method (scipy).
        The only dependency: scipy (in requirements.txt).
        """
        centres = self._get_target_centers(msg)
        memory_type = str(msg.get('memory_type', 'ltm')).lower()

        async def _compute() -> Dict[str, Any]:
            try:
                records = self._active_projection_records(centres)

                logger.info(
                    "[dendrogram] type=%s (%s), active.sum=%d, h>0.sum=%d, texts=%d",
                    memory_type, "LTM" if memory_type != 'stm' else "STM",
                    records['n_active'], records['n_h_positive'], records['n_texts'],
                )

                if records['n_points'] == 0:
                    return self._empty_projection_error(records, memory_type)

                linkage_data: List[List[float]] = []
                if records['n_points'] >= 2:
                    if hierarchy is None:
                        return self._error_response(
                            SCIPY_MISSING, "scipy is not installed. Run: pip install scipy"
                        )
                    K_active = centres.K[records['indices']].detach().cpu().numpy()
                    Z = hierarchy.linkage(K_active, method='ward')
                    linkage_data = [
                        [float(value) for value in row]
                        for row in Z.tolist()
                    ]
            except Exception as e:
                logger.error("get_dendrogram: computation failed: %s", e)
                return self._error_response(COMPUTE_ERROR, str(e))

            return self._success_response(
                response_version=2,
                linkage=linkage_data,
                linkage_matrix=linkage_data,
                leaves=records['indices'],
                memory_type=memory_type,
                **records,
            )

        return asyncio.ensure_future(_compute())

    def _handle_get_temporal_evolution_map(self, msg: Dict) -> Dict:
        """Alias for getting data for the cognitive development timeline (same data as get_dendrogram + ages)."""
        memory_type = str(msg.get('memory_type', 'ltm')).lower()

        async def _do() -> Dict[str, Any]:
            base = await self._handle_get_dendrogram(msg)
            if base.get('status') != 'success':
                return base
            base['memory_type'] = memory_type
            return base

        return asyncio.ensure_future(_do())

    def _handle_get_memory_graph(self, msg: Dict) -> Dict:
        """Returns nodes and edges (semantic similarities) for the interactive center map (Graph Explorer)."""
        layer_value = msg.get('layer')
        memory_type_value = msg.get('memory_type')
        if layer_value is not None and memory_type_value is not None:
            if str(layer_value).lower() != str(memory_type_value).lower():
                return self._error_response(
                    INVALID_PARAMS,
                    "'layer' and 'memory_type' must match when both are provided.",
                )
        memory_type = str(
            layer_value if layer_value is not None else memory_type_value or 'ltm'
        ).lower()
        if memory_type not in ('stm', 'ltm'):
            return self._error_response(
                INVALID_PARAMS, "'layer' must be 'stm' or 'ltm'."
            )
        threshold, error = self._number_param(msg, 'threshold', 0.6, 0.0, 1.0)
        if error:
            return error
        max_nodes, error = self._integer_param(msg, 'max_nodes', 100, 1, 250)
        if error:
            return error
        centres = self.memory.stm_centers if memory_type == 'stm' else self.memory.ltm_centers

        async def _compute() -> Dict[str, Any]:
            try:
                records = self._active_projection_records(centres)
                if records['n_points'] == 0:
                    return self._empty_projection_error(records, memory_type)

                total_nodes = records['n_points']
                selected_nodes = min(total_nodes, max_nodes)
                aligned_fields = (
                    'indices', 'key_texts', 'value_texts', 'memory_ids', 'provenances',
                    'intensities', 'usages', 'ages',
                )
                records = dict(records)
                for field in aligned_fields:
                    records[field] = records[field][:selected_nodes]
                records['n_points'] = selected_nodes
                records['n_texts'] = sum(1 for text in records['key_texts'] if text)

                K_active = centres.K[records['indices']].detach().cpu()
                norms = K_active.norm(dim=-1, keepdim=True).clamp_min(1e-12)
                K_norm = K_active / norms
                sim_matrix = K_norm @ K_norm.T

                nodes: List[Dict[str, Any]] = []
                edges: List[List[float]] = []
                edge_records: List[Dict[str, Any]] = []
                for i, idx in enumerate(records['indices']):
                    stable_id = records['memory_ids'][i] or idx
                    nodes.append({
                        'id': stable_id,
                        'memory_id': records['memory_ids'][i],
                        'center_index': idx,
                        'key_text': records['key_texts'][i],
                        'value_text': records['value_texts'][i],
                        'provenance': records['provenances'][i],
                        'h': records['intensities'][i],
                        'usage': records['usages'][i],
                        'age': records['ages'][i],
                    })
                for i in range(records['n_points']):
                    for j in range(i + 1, records['n_points']):
                        sim_val = float(sim_matrix[i, j].item())
                        if sim_val >= threshold:
                            edges.append([i, j, sim_val])
                            edge_records.append({
                                'source': records['memory_ids'][i] or records['indices'][i],
                                'target': records['memory_ids'][j] or records['indices'][j],
                                'weight': sim_val,
                            })
            except Exception as e:
                logger.error("get_memory_graph: computation failed: %s", e)
                return self._error_response(COMPUTE_ERROR, str(e))

            return self._success_response(
                response_version=2,
                nodes=nodes,
                edges=edges,
                edge_records=edge_records,
                memory_type=memory_type,
                layer=memory_type,
                threshold=threshold,
                max_nodes=max_nodes,
                total_nodes=total_nodes,
                selected_nodes=selected_nodes,
                truncated=selected_nodes < total_nodes,
                **records,
            )

        return asyncio.ensure_future(_compute())

    # ------------------------------------------------------------------
    # Batch import
    # ------------------------------------------------------------------

    def _handle_batch_import(self, msg: Dict) -> Dict:
        """Batch import of Q/A pairs into memory.

        Designed for importing exported conversation history (history.txt).

        Key features:
        - Does NOT run homeostasis (step()) – prevents artificial decay of existing memories.
        - Optional periodic consolidation (every CONSOLIDATE_EVERY writes)
          moves STM → LTM before the STM buffer overflows (512 centers).
        - Fatigue reset before import – writes from an old conversation have
          lower intensity, so they do not trigger spurious consolidations.
        - A single save at the end (not 500× torch.save).

        Input format:
            msg["pairs"]: list of {{"user": str, "model": str}}

        Output:
            stored:  number of successfully stored pairs
            skipped: number of skipped (empty text etc.)
            consolidations: number of triggered consolidations
        """
        pairs = msg.get('pairs')
        if not pairs or not isinstance(pairs, list):
            return self._error_response(INVALID_PARAMS, "Missing or empty 'pairs' list.")

        if self.settings_manager is not None and self.settings_manager.pt_import_locked:
            return self._error_response(
                PT_IMPORT_LOCKED,
                "Import of legacy .pt is permanently locked — an import or memory write "
                "has already occurred.",
            )

        async def _run() -> Dict[str, Any]:
            stored = 0
            skipped = 0
            consolidations = 0
            async with self.lock:
                for pair in pairs:
                    if not isinstance(pair, dict):
                        skipped += 1
                        continue
                    user = str(pair.get('user', '') or '').strip()
                    model = str(pair.get('model', '') or '').strip()
                    if not user or not model:
                        skipped += 1
                        continue
                    try:
                        self.memory.store(user, model, intensity=IMPORT_INTENSITY)
                        stored += 1
                    except Exception as e:
                        logger.error("BATCH_IMPORT: store error: %s", e)
                        skipped += 1

                    if stored % CONSOLIDATE_EVERY == 0 and stored > 0:
                        stm_count = 0
                        if hasattr(self.memory, 'stm_centers') and hasattr(self.memory.stm_centers, 'key_text'):
                            stm_count = len(self.memory.stm_centers.key_text)
                        logger.info(
                            "BATCH_IMPORT: consolidation after %d record writes, STM=\"%d\"",
                            CONSOLIDATE_EVERY, stm_count,
                        )
                        try:
                            self.memory.consolidate()
                            consolidations += 1
                        except Exception as e:
                            logger.error("BATCH_IMPORT: store error: %s", e)

                try:
                    self.memory.save()
                except Exception as e:
                    logger.error("BATCH_IMPORT: store error: %s", e)

            logger.info(
                "BATCH_IMPORT: done. stored=%d, skipped=%d, consolidations=%d",
                stored, skipped, consolidations,
            )
            return self._success_response(
                stored=stored, skipped=skipped, consolidations=consolidations
            )

        return asyncio.ensure_future(_run())
