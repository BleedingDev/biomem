"""Regression tests that keep user content and identifiers out of diagnostics."""

import asyncio
import inspect
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_module.protocol import CommandHandler, INTERNAL_ERROR, WRITE_ERROR  # noqa: E402


class SensitiveLoggingTests(unittest.TestCase):
    def test_store_logs_report_bounded_metadata_without_memory_content(self):
        legacy_source = inspect.getsource(CommandHandler._handle_store)
        self.assertIn(
            '"STORE: key_source=%s, value_source=%s, key_length=%d, value_length=%d"',
            legacy_source,
        )
        self.assertNotIn('key=\'%s\'', legacy_source)
        self.assertNotIn('value=\'%s\'', legacy_source)

        ollama_source = inspect.getsource(CommandHandler._handle_ollama_chat)
        self.assertIn('logger.info("OLLAMA_CHAT STORE: memory stored")', ollama_source)
        self.assertNotIn('logger.info("OLLAMA_CHAT STORE: key', ollama_source)

    def test_retrieve_failure_log_does_not_include_exception_or_query(self):
        sensitive_query = "private recall query"
        sensitive_error = "memory payload escaped through an exception"

        class FailingMemory:
            def recall(self, *_args, **_kwargs):
                raise RuntimeError(sensitive_error)

        handler = object.__new__(CommandHandler)
        handler.memory = FailingMemory()

        with self.assertLogs("bdbm.protocol", level="ERROR") as captured:
            result = handler._retrieve_memories(sensitive_query)

        log_text = "\n".join(captured.output)
        self.assertEqual(result, [])
        self.assertIn("retrieve: memory recall failed", log_text)
        self.assertNotIn(sensitive_query, log_text)
        self.assertNotIn(sensitive_error, log_text)

    def test_dispatch_failure_returns_and_logs_only_stable_error(self):
        sensitive_error = "dispatcher leaked a private memory canary"

        class Security:
            @staticmethod
            def is_operational_command(_command):
                return False

        class ExplodingHandler:
            def __call__(self, _message):
                raise RuntimeError(sensitive_error)

        handler = object.__new__(CommandHandler)
        handler.handlers = {"explode": ExplodingHandler()}
        handler.security = Security()
        handler._check_auth = lambda _command, _message: None

        with self.assertLogs("bdbm.protocol", level="ERROR") as captured:
            response = asyncio.run(handler.handle({"command": "explode"}))

        log_text = "\n".join(captured.output)
        self.assertEqual(response["code"], INTERNAL_ERROR)
        self.assertEqual(response["error"], "Command processing failed.")
        self.assertIn("Command processing failed", log_text)
        self.assertNotIn(sensitive_error, log_text)
        self.assertNotIn(sensitive_error, json.dumps(response))

    def test_store_failure_returns_and_logs_only_stable_error(self):
        sensitive_error = "store leaked private key and value canary"

        class SessionCache:
            @staticmethod
            def consume(_session_id):
                return "private original query"

        class FailingMemory:
            def store(self, *_args, **_kwargs):
                raise RuntimeError(sensitive_error)

        handler = object.__new__(CommandHandler)
        handler.session_cache = SessionCache()
        handler.memory = FailingMemory()

        async def exercise():
            handler._lock = asyncio.Lock()
            return await handler._handle_store(
                {
                    "session_id": "private-session-id",
                    "model_summary": "private model summary",
                }
            )

        with self.assertLogs("bdbm.protocol", level="INFO") as captured:
            response = asyncio.run(exercise())

        log_text = "\n".join(captured.output)
        self.assertEqual(response["code"], WRITE_ERROR)
        self.assertEqual(response["error"], "Memory write failed.")
        self.assertIn("STORE: memory write failed", log_text)
        self.assertNotIn(sensitive_error, log_text)
        self.assertNotIn(sensitive_error, json.dumps(response))

    def test_extension_prompt_dump_logger_is_a_production_noop(self):
        repository_root = Path(__file__).resolve().parents[1]
        common_files = (
            repository_root / "extensions/chrome-src/content/common.js",
            repository_root / "extensions/firefox-src/content/common.js",
            repository_root / "extensions/safari-src/content/common.js",
        )

        for common_file in common_files:
            with self.subTest(common_file=common_file):
                source = common_file.read_text(encoding="utf-8")
                start = source.index("  function debugIo(label, payload) {")
                end = source.index("\n  }\n\n  async function loadPanelUiState", start)
                logger_body = source[start:end]

                self.assertIn("const DEBUG_PROMPT_IO = false;", source)
                self.assertIn("Production no-op", logger_body)
                self.assertNotIn("console.", logger_body)
                self.assertNotIn("window.location", logger_body)

        chrome_source = common_files[0].read_text(encoding="utf-8")
        for raw_dump in (
            "text preview:",
            '"preview:", (rawText || "").slice',
            '"parsed.modelSummary:"',
            '"with sessionId:"',
            '"store() SUCCEEDED:", storeResult',
            '"store() FAILED:", err.message',
        ):
            with self.subTest(raw_dump=raw_dump):
                self.assertNotIn(raw_dump, chrome_source)


if __name__ == "__main__":
    unittest.main()
