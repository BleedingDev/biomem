"""Privacy regressions for conversation failure diagnostics."""

import asyncio
import builtins
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_module.conversation_handler import ConversationHandler  # noqa: E402


_CANARY = (
    "PROMPT_CANARY MEMORY_CANARY ATTACHMENT_CANARY "
    "RESPONSE_BODY_CANARY TOKEN_CANARY"
)


class _ThreadStore:
    def __init__(self):
        self.saved = []

    def get_thread_list(self):
        return []

    def save_thread(self, *args):
        self.saved.append(args)


class _Settings:
    def get_max_associations(self):
        return 5

    def get_context_limit(self, _model):
        return 1000

    def get_personalisation(self, _model):
        return ""


class _LLM:
    def __init__(self, has_key=False):
        self._has_key = has_key

    def has_api_key(self, _model):
        return self._has_key


class _Cache:
    def __init__(self):
        self.query = None

    def store(self, _session_id, query, _metadata):
        self.query = query

    def consume(self, _session_id):
        return self.query


class _Memory:
    def __init__(self, failure=None):
        self.failure = failure

    def step(self):
        return None

    def store(self, **_kwargs):
        if self.failure is not None:
            raise self.failure

    def save(self):
        return None


class _CommandHandler:
    def __init__(self, retrieve_failure=None, store_failure=None):
        self.cache = _Cache()
        self.memory = _Memory(store_failure)
        self.retrieve_failure = retrieve_failure

    def _retrieve_memories(self, *_args):
        if self.retrieve_failure is not None:
            raise self.retrieve_failure
        return []


class ConversationLoggingTests(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, command_handler=None, has_key=False):
        handler = ConversationHandler(
            command_handler,
            _LLM(has_key=has_key),
            _Settings(),
            _ThreadStore(),
        )
        self.system_messages = []
        handler.system_message.connect(
            lambda text, level: self.system_messages.append((text, level))
        )
        return handler

    def assert_canary_absent(self, value):
        rendered = str(value)
        for word in _CANARY.split():
            self.assertNotIn(word, rendered)

    def test_llm_error_messages_never_return_exception_text(self):
        handler = self.make_handler()

        for code in ("NO_API_KEY", "TIMEOUT", "NETWORK_ERROR", "API_ERROR", None):
            with self.subTest(code=code):
                error = RuntimeError(_CANARY)
                error.code = code
                message = handler._format_llm_error(error)
                self.assert_canary_absent(message)
                self.assertTrue(message)

    async def test_attachment_failures_hide_paths_suffixes_and_exceptions(self):
        handler = self.make_handler()
        missing_path = "/private/ATTACHMENT_CANARY/missing.txt"

        with tempfile.TemporaryDirectory() as directory:
            unsupported = Path(directory) / "document.TOKEN_CANARY"
            unsupported.write_bytes(b"RESPONSE_BODY_CANARY")
            unreadable = Path(directory) / "MEMORY_CANARY.txt"
            unreadable.write_text("PROMPT_CANARY", encoding="utf-8")

            with self.assertLogs("bdbm.conv_handler", level="ERROR") as captured:
                missing_result = await handler._prepare_attachments([missing_path])
                unsupported_result = await handler._prepare_attachments(
                    [str(unsupported)]
                )
                with patch.object(
                    Path, "read_text", side_effect=RuntimeError(_CANARY)
                ):
                    unreadable_result = await handler._prepare_attachments(
                        [str(unreadable)]
                    )

        exposed = "\n".join(captured.output) + repr(self.system_messages)
        self.assertEqual(missing_result, [])
        self.assertEqual(unsupported_result, [])
        self.assertEqual(unreadable_result, [])
        self.assert_canary_absent(exposed)
        self.assertNotIn(missing_path, exposed)
        self.assertNotIn("token_canary", exposed.lower())

    def test_missing_pdf_support_does_not_log_attachment_path(self):
        handler = self.make_handler()
        sensitive_path = "/private/ATTACHMENT_CANARY/document.pdf"
        original_import = builtins.__import__

        def import_without_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError(_CANARY)
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=import_without_pypdf):
            with self.assertLogs("bdbm.conv_handler", level="ERROR") as captured:
                result = handler._extract_pdf_text(sensitive_path)

        exposed = "\n".join(captured.output)
        self.assertEqual(result, "")
        self.assert_canary_absent(exposed)
        self.assertNotIn(sensitive_path, exposed)

    def test_scheduling_failure_hides_exception_and_input(self):
        handler = self.make_handler()

        class _ImmediateLoop:
            @staticmethod
            def call_soon_threadsafe(callback):
                callback()

        handler._loop = _ImmediateLoop()

        def fail_schedule(coroutine):
            coroutine.close()
            raise RuntimeError(_CANARY)

        with patch.object(asyncio, "ensure_future", side_effect=fail_schedule):
            with self.assertLogs("bdbm.conv_handler", level="ERROR") as captured:
                handler.process_message("PROMPT_CANARY", "TOKEN_CANARY")

        exposed = "\n".join(captured.output) + repr(self.system_messages)
        self.assert_canary_absent(exposed)
        self.assertFalse(handler.is_busy)

    async def test_top_level_processing_failure_is_sanitized(self):
        handler = self.make_handler()
        handler._prepare_attachments = AsyncMock(side_effect=RuntimeError(_CANARY))

        with self.assertLogs("bdbm.conv_handler", level="ERROR") as captured:
            await handler._process_message_async(
                "PROMPT_CANARY", "TOKEN_CANARY", "associative", False
            )

        exposed = "\n".join(captured.output) + repr(self.system_messages)
        self.assert_canary_absent(exposed)
        self.assertFalse(handler.is_busy)

    async def test_retrieve_and_store_failures_use_static_diagnostics(self):
        retrieve_handler = self.make_handler(
            _CommandHandler(retrieve_failure=RuntimeError(_CANARY))
        )
        with self.assertLogs("bdbm.conv_handler", level="WARNING") as retrieve_logs:
            await retrieve_handler._process_message_async(
                "ordinary query", "ollama", "associative", False
            )

        store_handler = self.make_handler(
            _CommandHandler(store_failure=RuntimeError(_CANARY)), has_key=True
        )
        store_handler._call_llm_with_timeout = AsyncMock(
            return_value=(
                "ordinary answer\n"
                "|STPAM| ordinary query |MIDPAM| ordinary answer |ENDPAM|"
            )
        )
        with self.assertLogs("bdbm.conv_handler", level="ERROR") as store_logs:
            await store_handler._process_message_async(
                "ordinary query", "ollama", "associative", False
            )

        exposed = (
            "\n".join(retrieve_logs.output)
            + "\n".join(store_logs.output)
            + repr(self.system_messages)
        )
        self.assert_canary_absent(exposed)
        self.assertIn("biomem retrieve failed", "\n".join(retrieve_logs.output))
        self.assertIn("biomem store failed", "\n".join(store_logs.output))

    async def test_deep_recall_failure_hides_exception_text(self):
        handler = self.make_handler(
            _CommandHandler(retrieve_failure=RuntimeError(_CANARY))
        )
        handler._call_llm_with_timeout = AsyncMock(
            return_value="|MEMQUERY|ordinary lookup|ENDQUERY|"
        )

        with self.assertLogs("bdbm.conv_handler", level="ERROR") as captured:
            result = await handler._process_deep_recall(
                "ordinary query", [], "ollama", False
            )

        self.assertFalse(result["usedDeepRecall"])
        self.assert_canary_absent("\n".join(captured.output) + repr(result))

    async def test_unknown_provider_identifier_is_not_reflected_in_error(self):
        handler = self.make_handler()
        await handler._process_message_async(
            "ordinary query", "TOKEN_CANARY", "associative", False
        )

        self.assert_canary_absent(self.system_messages)
        self.assertIn("selected provider", self.system_messages[-1][0])


if __name__ == "__main__":
    unittest.main()
