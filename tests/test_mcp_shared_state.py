import uuid
import unittest

from memory_module.http_fallback import HTTPFallbackServer
from memory_module.local_daemon_client import LocalDaemonClient
from memory_module.protocol import CommandHandler


class _SessionCache:
    def __init__(self):
        self.entries = {}

    def store(self, session_id, query, metadata=None):
        self.entries[session_id] = (query, metadata or {})

    def consume(self, session_id):
        item = self.entries.pop(session_id, None)
        return item[0] if item else None

    def get_active_count(self):
        return len(self.entries)


class _Security:
    state = "ACTIVE"
    is_active = True
    is_deactivated = False
    is_suspended = False

    @staticmethod
    def is_operational_command(_command):
        return True

    @staticmethod
    def check_command_allowed(_command):
        return None

    @staticmethod
    def get_suspend_info():
        return None


class _SharedMemory:
    """Small protocol-core fixture; the adapter never receives this object."""

    def __init__(self):
        self.records = []
        self.save_count = 0

    def store_record(self, key, value, **kwargs):
        for record in self.records:
            if record["key"] == key and record["value"] == value:
                record["provenance"] = dict(kwargs.get("provenance") or {})
                return {
                    **record,
                    "status": "reinforced",
                    "created": False,
                    "reinforced": True,
                    "index": self.records.index(record),
                    "key_text": key,
                    "value_text": value,
                    "new_centers": 0,
                }
        record = {
            "memory_id": str(uuid.uuid4()),
            "key": key,
            "value": value,
            "layer": "stm",
            "provenance": dict(kwargs.get("provenance") or {}),
            "age": 0,
            "usage": 0,
            "intensity": kwargs.get("intensity", 1.0),
        }
        self.records.append(record)
        return {
            **record,
            "status": "created",
            "created": True,
            "reinforced": False,
            "index": len(self.records) - 1,
            "key_text": key,
            "value_text": value,
            "new_centers": 1,
        }

    def list_memories(self, source="both", limit=100):
        records = [
            record for record in self.records
            if source == "both" or record["layer"] == source
        ]
        return records[:limit]

    def save(self):
        self.save_count += 1

    def get_stats(self):
        return {"records": len(self.records)}


class MCPSharedStateContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            import mcp  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("official mcp==2.1.1 is not installed")
        self.memory = _SharedMemory()
        handler = CommandHandler(
            self.memory,
            session_cache=_SessionCache(),
            security=_Security(),
        )
        self.daemon = HTTPFallbackServer(handler=handler, security=None, port=0)
        self.daemon.start()

    async def asyncTearDown(self):
        if hasattr(self, "daemon"):
            self.daemon.stop()

    async def test_mcp_and_native_clients_observe_one_protocol_state(self):
        from mcp.client import Client
        from memory_module.mcp_server import create_server

        port = self.daemon.bound_port
        adapter_client = LocalDaemonClient(port=port)
        server = create_server(adapter_client)
        async with Client(server, mode="legacy") as mcp_client:
            stored = await mcp_client.call_tool(
                "biomem_store", {"key": "shared fact", "value": "shared value"}
            )
            self.assertFalse(stored.is_error)

            async with LocalDaemonClient(port=port) as native_client:
                listed = await native_client.command(
                    "list_memories", layer="both", limit=20
                )
                await native_client.command(
                    "store_record",
                    key="native-only fact",
                    value="native-only value",
                    provenance={
                        "source_class": "browser",
                        "origin": "local-test",
                        "session_id": "browser:test",
                    },
                )

            mcp_list = await mcp_client.call_tool(
                "biomem_list", {"layer": "both", "limit": 20}
            )

        self.assertEqual(listed["records"][0]["key"], "shared fact")
        provenance = listed["records"][0]["provenance"]
        self.assertEqual(provenance["source_class"], "mcp")
        self.assertEqual(provenance["origin"], "local-mcp-stdio")
        self.assertRegex(provenance["session_id"], r"^mcp:[0-9a-f-]{36}$")
        self.assertEqual(
            {record["key"] for record in mcp_list.structured_content["records"]},
            {"shared fact", "native-only fact"},
        )
        self.assertEqual(self.memory.save_count, 2)


if __name__ == "__main__":
    unittest.main()
