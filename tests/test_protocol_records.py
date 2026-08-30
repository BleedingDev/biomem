"""Client-neutral protocol contract tests for stable memory records."""

import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("OMP_NUM_THREADS", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import torch  # noqa: E402

from memory_module.protocol import CommandHandler  # noqa: E402
from memory_module.security import SecurityManager  # noqa: E402


class _SessionCache:
    def __init__(self):
        self.entries = {}

    def store(self, session_id, query, metadata=None):
        self.entries[session_id] = (query, metadata or {})

    def consume(self, session_id):
        entry = self.entries.pop(session_id, None)
        return entry[0] if entry else None

    def get_active_count(self):
        return len(self.entries)


class _Security:
    state = "ACTIVE"
    is_active = True
    is_deactivated = False
    is_suspended = False

    @staticmethod
    def is_operational_command(command):
        return command in {
            "retrieve",
            "store",
            "store_record",
            "search",
            "list_memories",
        }

    @staticmethod
    def check_command_allowed(_command):
        return None

    @staticmethod
    def get_suspend_info():
        return None


def _centers(prefix):
    active = torch.tensor([True, True, True, False])
    return SimpleNamespace(
        active=active,
        K=torch.tensor(
            [
                [1.0, 0.0, 0.0],
                [0.8, 0.6, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0],
            ]
        ),
        h=torch.tensor([1.0, 2.0, 3.0, 0.0]),
        usage=torch.tensor([3, 2, 1, 0]),
        age=torch.tensor([10, 20, 30, 0]),
        key_texts=[f"{prefix} key {index}" for index in range(4)],
        value_texts=[f"{prefix} value {index}" for index in range(4)],
        memory_ids=[f"{prefix}-id-{index}" for index in range(4)],
        provenances=[{"source_class": "browser", "origin": "chatgpt.com"}] * 4,
    )


class _Memory:
    def __init__(self):
        self.read_count = 0
        self.save_count = 0
        self.store_calls = []
        self.legacy_store_calls = []
        self.search_calls = []
        self._stored_id = None
        self._canonical_by_id = {}
        self._id_by_key = {}
        self.stm_centers = _centers("stm")
        self.ltm_centers = _centers("ltm")
        self.records = [
            {
                "index": 8,
                "layer": "stm",
                "key_text": "third",
                "value_text": "value 3",
                "memory_id": "c-id",
                "provenance": {"source_class": "browser"},
                "intensity": 0.3,
                "usage": 3,
                "age": 30,
            },
            {
                "index": 4,
                "layer": "ltm",
                "key_text": "second",
                "value_text": "value 2",
                "memory_id": "b-id",
                "provenance": {"source_class": "mcp"},
                "intensity": 0.2,
                "usage": 2,
                "age": 20,
            },
            {
                "index": 2,
                "layer": "ltm",
                "key_text": "first",
                "value_text": "value 1",
                "memory_id": "a-id",
                "provenance": {"source_class": "browser"},
                "intensity": 0.1,
                "usage": 1,
                "age": 10,
            },
        ]

    def store(self, key, value, **kwargs):
        self.legacy_store_calls.append((key, value, kwargs))
        return 1

    def store_record(self, key, value, **kwargs):
        self.store_calls.append((key, value, kwargs))
        provenance = dict(kwargs.get("provenance") or {})
        requested_id = kwargs.get("memory_id")
        assigned_id = (
            requested_id
            or self._id_by_key.get(key)
            or ("stable-record-id" if not self._canonical_by_id else f"id-{len(self._canonical_by_id)}")
        )
        canonical_key = self._canonical_by_id.get(assigned_id)
        if canonical_key is not None and canonical_key != key:
            return {
                "status": "duplicate_memory_id",
                "memory_id": None,
                "created": False,
                "reinforced": False,
                "layer": None,
                "index": None,
                "key_text": None,
                "value_text": None,
                "provenance": None,
                "new_centers": 0,
            }
        created = canonical_key is None
        self._canonical_by_id[assigned_id] = key
        self._id_by_key[key] = assigned_id
        self._stored_id = assigned_id
        return {
            "status": "created" if created else "reinforced",
            "memory_id": assigned_id,
            "created": created,
            "reinforced": not created,
            "layer": "stm",
            "index": 7,
            "key_text": key,
            "value_text": value,
            "provenance": provenance,
            "new_centers": 1 if created else 0,
        }

    def save(self):
        self.save_count += 1

    def recall(self, _query, top_k=5, increment_stats=True):
        if increment_stats:
            self.read_count += 1
        return SimpleNamespace(
            matches=[
                {
                    "index": 7,
                    "key": "remembered key",
                    "value": "remembered value",
                    "weight": 0.91,
                    "source": "STM",
                    "layer": "stm",
                    "memory_id": "stable-record-id",
                    "provenance": {
                        "source_class": "browser",
                        "origin": "chatgpt.com",
                    },
                }
            ][:top_k]
        )

    def search(self, query, top_k=10, source="both"):
        self.search_calls.append((query, top_k, source))
        return [
            {
                "index": 7,
                "key": "remembered key",
                "value": "remembered value",
                "similarity": 0.91,
                "source": "stm",
                "memory_id": "stable-record-id",
                "provenance": {"source_class": "browser"},
                "age": 2,
                "usage": 4,
                "h": 0.75,
            }
        ][:top_k]

    def list_memories(self, source="both", limit=100):
        return [
            record for record in self.records
            if source == "both" or record["layer"] == source
        ][:limit]

    def get_stats(self):
        return {"step_count": 5, "reads": self.read_count}


def _handler(memory=None):
    return CommandHandler(
        memory or _Memory(),
        session_cache=_SessionCache(),
        security=_Security(),
    )


def _request(handler, command, **params):
    return asyncio.run(handler.handle({"command": command, **params}))


class ProtocolRecordTests(unittest.TestCase):
    def test_legacy_browser_store_forwards_validated_provenance(self):
        memory = _Memory()
        handler = _handler(memory)
        provenance = {
            "source_class": "browser",
            "origin": "chatgpt.com",
            "session_id": "browser-session",
        }
        handler.session_cache.store("browser-session", "original browser query")

        result = _request(
            handler,
            "store",
            session_id="browser-session",
            model_summary="browser answer summary",
            provenance=provenance,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], "original browser query")
        self.assertEqual(
            memory.legacy_store_calls,
            [
                (
                    "original browser query",
                    "browser answer summary",
                    {"provenance": provenance},
                )
            ],
        )

        handler.session_cache.store("invalid-session", "must remain available")
        invalid = _request(
            handler,
            "store",
            session_id="invalid-session",
            model_summary="answer",
            provenance={"raw_url": "https://example.invalid/private"},
        )
        self.assertEqual(invalid["status"], "error")
        self.assertEqual(invalid["code"], "INVALID_PARAMS")
        self.assertEqual(
            handler.session_cache.consume("invalid-session"), "must remain available"
        )
        self.assertEqual(len(memory.legacy_store_calls), 1)

    def test_browser_store_preserves_exact_turn_when_summaries_are_lossy(self):
        memory = _Memory()
        handler = _handler(memory)
        session_id = "lossless-browser-session"
        exact_query = (
            "Biomem authenticated Chrome test. Remember this synthetic fact: "
            "BIOMEM_LOSSLESS_4107 means the silver compass points east. "
            "Reply with a short acknowledgement while keeping every exact "
            "identifier and directional detail intact for later recall."
        )
        exact_response = (
            "Acknowledged: BIOMEM_LOSSLESS_4107 means the silver compass "
            "points east."
        )
        provenance = {
            "source_class": "browser",
            "origin": "chatgpt.com",
            "session_id": session_id,
        }
        handler.session_cache.store(session_id, exact_query)

        result = _request(
            handler,
            "store",
            session_id=session_id,
            user_summary="Remember synthetic fact; short acknowledgement",
            model_summary="Acknowledged fact",
            response_text=exact_response,
            provenance=provenance,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["key"], exact_query)
        self.assertEqual(result["key_source"], "query")
        self.assertEqual(result["value"], exact_response)
        self.assertEqual(result["value_source"], "response_text")
        self.assertEqual(
            memory.legacy_store_calls,
            [(exact_query, exact_response, {"provenance": provenance})],
        )

    def test_store_record_preserves_authoritative_identity_and_provenance(self):
        memory = _Memory()
        handler = _handler(memory)
        provenance = {
            "source_class": "browser",
            "origin": "chatgpt.com",
            "session_id": "browser-session",
        }

        created = _request(
            handler,
            "store_record",
            key="shared fact",
            value="shared value",
            provenance=provenance,
        )
        reinforced = _request(
            handler,
            "store_record",
            key="shared fact",
            value="shared value",
            provenance={"source_class": "mcp", "session_id": "mcp-session"},
        )

        self.assertEqual(created["status"], "success")
        self.assertEqual(created["record_status"], "created")
        self.assertTrue(created["created"])
        self.assertEqual(reinforced["record_status"], "reinforced")
        self.assertTrue(reinforced["reinforced"])
        self.assertEqual(created["memory_id"], reinforced["memory_id"])
        self.assertEqual(memory.store_calls[0][2]["provenance"], provenance)
        self.assertEqual(memory.save_count, 2)

    def test_caller_memory_id_reinforces_same_key_and_rejects_different_key(self):
        memory = _Memory()
        handler = _handler(memory)

        created = _request(
            handler,
            "store_record",
            key="canonical key",
            value="first value",
            memory_id="caller-owned-id",
        )
        reinforced = _request(
            handler,
            "store_record",
            key="canonical key",
            value="updated value",
            memory_id="caller-owned-id",
        )
        rejected = _request(
            handler,
            "store_record",
            key="different key",
            value="must not overwrite",
            memory_id="caller-owned-id",
        )

        self.assertEqual(created["record_status"], "created")
        self.assertEqual(reinforced["record_status"], "reinforced")
        self.assertEqual(created["memory_id"], reinforced["memory_id"])
        self.assertEqual(rejected["status"], "error")
        self.assertEqual(rejected["code"], "DUPLICATE_MEMORY_ID")
        self.assertEqual(
            rejected["error"],
            "The supplied memory_id is already assigned to a different record.",
        )
        self.assertEqual(memory.save_count, 2)

    def test_retrieve_is_enriched_and_search_does_not_increment_recall_stats(self):
        memory = _Memory()
        handler = _handler(memory)

        retrieved = _request(
            handler,
            "retrieve",
            query="remember this",
            session_id="session-1",
            top_k=1,
        )
        searched = _request(
            handler,
            "search",
            query="remember this",
            top_k=1,
            layer="stm",
        )

        self.assertEqual(memory.read_count, 1)
        match = retrieved["memories"][0]
        self.assertEqual(match["memory_id"], "stable-record-id")
        self.assertEqual(match["key"], "remembered key")
        self.assertEqual(match["value"], "remembered value")
        self.assertEqual(match["layer"], "stm")
        self.assertEqual(match["provenance"]["origin"], "chatgpt.com")
        result = searched["results"][0]
        self.assertEqual(result["memory_id"], "stable-record-id")
        self.assertEqual(result["similarity"], 0.91)
        self.assertEqual(result["intensity"], 0.75)
        self.assertNotIn("index", result)

    def test_list_pagination_is_deterministic_and_opaque(self):
        handler = _handler()

        first = _request(handler, "list_memories", layer="both", limit=2)
        repeated = _request(handler, "list_memories", layer="both", limit=2)
        second = _request(
            handler,
            "list_memories",
            layer="both",
            limit=2,
            cursor=first["next_cursor"],
        )

        self.assertEqual(
            [record["memory_id"] for record in first["records"]],
            ["a-id", "b-id"],
        )
        self.assertEqual(first, repeated)
        self.assertTrue(first["has_more"])
        self.assertNotIn("a-id", first["next_cursor"])
        self.assertEqual(
            [record["memory_id"] for record in second["records"]], ["c-id"]
        )
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])
        self.assertTrue(all("index" not in record for record in first["records"]))

    def test_list_cursor_advances_without_repeating_or_looping(self):
        handler = _handler()
        cursor = None
        seen_cursors = set()
        seen_ids = []

        while True:
            params = {"layer": "both", "limit": 1}
            if cursor is not None:
                self.assertNotIn(cursor, seen_cursors)
                seen_cursors.add(cursor)
                params["cursor"] = cursor
            page = _request(handler, "list_memories", **params)
            seen_ids.extend(record["memory_id"] for record in page["records"])
            cursor = page["next_cursor"]
            if cursor is None:
                self.assertFalse(page["has_more"])
                break

        self.assertEqual(seen_ids, ["a-id", "b-id", "c-id"])
        self.assertEqual(len(seen_ids), len(set(seen_ids)))
        self.assertEqual(len(seen_cursors), 2)

    def test_graph_uses_stable_ids_and_enforces_node_cap(self):
        result = _request(
            _handler(),
            "get_memory_graph",
            layer="stm",
            threshold=0.0,
            max_nodes=2,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_nodes"], 3)
        self.assertEqual(result["selected_nodes"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(
            [node["id"] for node in result["nodes"]], ["stm-id-0", "stm-id-1"]
        )
        self.assertEqual(result["nodes"][0]["center_index"], 0)
        self.assertEqual(result["nodes"][0]["provenance"]["origin"], "chatgpt.com")
        self.assertEqual(
            result["edge_records"][0]["source"], "stm-id-0"
        )
        self.assertEqual(
            result["edge_records"][0]["target"], "stm-id-1"
        )

    def test_new_commands_are_open_and_operational(self):
        messages = {
            "store_record": {"key": "key", "value": "value"},
            "retrieve": {"query": "query", "session_id": "session"},
            "search": {"query": "query"},
            "list_memories": {},
        }
        handler = _handler()
        for command in messages:
            with self.subTest(command=command):
                self.assertIn(command, handler.handlers)

        with tempfile.TemporaryDirectory() as tmp:
            security = SecurityManager(data_dir=tmp)
            for command in messages:
                self.assertTrue(security.is_operational_command(command))

    def test_invalid_inputs_return_stable_errors_without_calling_memory(self):
        memory = _Memory()
        handler = _handler(memory)
        cases = [
            ("store_record", {"key": " ", "value": "value"}),
            ("store_record", {"key": "key", "value": "value", "intensity": 0}),
            (
                "store_record",
                {"key": "key", "value": "value", "provenance": {"raw": "no"}},
            ),
            ("retrieve", {"query": "query", "session_id": "session", "top_k": True}),
            ("search", {"query": "query", "layer": "stm", "source": "ltm"}),
            ("list_memories", {"limit": 0}),
            ("list_memories", {"cursor": "not-base64"}),
            ("get_memory_graph", {"layer": "both"}),
            ("get_memory_graph", {"layer": "stm", "max_nodes": 251}),
        ]

        for command, params in cases:
            with self.subTest(command=command, params=params):
                result = _request(handler, command, **params)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "INVALID_PARAMS")
        self.assertEqual(memory.store_calls, [])
        self.assertEqual(memory.read_count, 0)


if __name__ == "__main__":
    unittest.main()
