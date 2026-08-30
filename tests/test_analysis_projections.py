"""Regression tests for active-record analysis protocol projections."""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

os.environ.setdefault("OMP_NUM_THREADS", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import torch  # noqa: E402

from memory_module.memory_centers import MemoryCenters  # noqa: E402
from memory_module.protocol import CommandHandler  # noqa: E402


ACTIVE_INDICES = [0, 2, 4]
KEYS = [
    [1.0, 0.0, 0.0],
    [0.8, 0.6, 0.0],
    [0.0, 0.0, 1.0],
]
AGES = [11, 22, 33]


class _Security:
    @staticmethod
    def is_operational_command(_command: str) -> bool:
        return False


def _centers(layer: str, count: int) -> MemoryCenters:
    centers = MemoryCenters(n_centers=6, d_key=3, d_value=2)
    for ordinal, index in enumerate(ACTIVE_INDICES[:count]):
        centers.active[index] = True
        centers.K[index] = torch.tensor(KEYS[ordinal])
        centers.h[index] = float(ordinal + 1)
        centers.usage[index] = (ordinal + 1) * 10
        centers.age[index] = AGES[ordinal]
        centers.key_texts[index] = f"{layer.upper()} key {ordinal + 1}"
        centers.value_texts[index] = f"{layer.upper()} value {ordinal + 1}"

    # A populated but inactive capacity slot must never leak into a projection.
    centers.K[1] = torch.tensor([1.0, 0.0, 0.0])
    centers.h[1] = 99.0
    centers.usage[1] = 99
    centers.age[1] = 999
    centers.key_texts[1] = "inactive key"
    centers.value_texts[1] = "inactive value"
    return centers


def _handler(count: int) -> CommandHandler:
    memory = SimpleNamespace(
        stm_centers=_centers("stm", count),
        ltm_centers=_centers("ltm", count),
    )
    return CommandHandler(memory, session_cache=None, security=_Security())


async def _request(handler: CommandHandler, command: str, memory_type: str, **params):
    return await handler.handle(
        {"command": command, "memory_type": memory_type, **params}
    )


class AnalysisProjectionTests(unittest.TestCase):
    def test_multiple_active_records_are_aligned_for_stm_and_ltm(self):
        for memory_type in ("stm", "ltm"):
            with self.subTest(memory_type=memory_type):
                handler = _handler(3)
                graph = asyncio.run(
                    _request(handler, "get_memory_graph", memory_type, threshold=0.75)
                )
                dendrogram = asyncio.run(
                    _request(handler, "get_dendrogram", memory_type)
                )
                temporal = asyncio.run(
                    _request(handler, "get_temporal_evolution_map", memory_type)
                )

                prefix = memory_type.upper()
                expected_keys = [f"{prefix} key {i}" for i in range(1, 4)]
                expected_values = [f"{prefix} value {i}" for i in range(1, 4)]

                self.assertEqual(graph["status"], "success")
                self.assertEqual(graph["n_points"], 3)
                self.assertEqual(graph["indices"], ACTIVE_INDICES)
                self.assertEqual(graph["key_texts"], expected_keys)
                self.assertEqual(graph["value_texts"], expected_values)
                self.assertEqual([node["key_text"] for node in graph["nodes"]], expected_keys)
                self.assertEqual(len(graph["edges"]), 1)
                self.assertEqual(graph["edges"][0][:2], [0, 1])
                self.assertAlmostEqual(graph["edges"][0][2], 0.8, places=6)
                self.assertEqual(
                    [(edge["source"], edge["target"]) for edge in graph["edge_records"]],
                    [(0, 2)],
                )

                self.assertEqual(dendrogram["status"], "success")
                self.assertEqual(dendrogram["n_points"], 3)
                self.assertEqual(dendrogram["n_texts"], 3)
                self.assertEqual(dendrogram["indices"], ACTIVE_INDICES)
                self.assertEqual(dendrogram["key_texts"], expected_keys)
                self.assertEqual(len(dendrogram["linkage_matrix"]), 2)
                self.assertEqual(dendrogram["linkage"], dendrogram["linkage_matrix"])

                self.assertEqual(temporal["status"], "success")
                self.assertEqual(temporal["n_points"], 3)
                self.assertEqual(temporal["ages"], [float(age) for age in AGES])
                self.assertEqual(temporal["indices"], ACTIVE_INDICES)
                self.assertEqual(temporal["key_texts"], expected_keys)

    def test_single_active_record_has_a_valid_empty_cluster_and_edge_set(self):
        handler = _handler(1)
        for memory_type in ("stm", "ltm"):
            with self.subTest(memory_type=memory_type):
                graph = asyncio.run(_request(handler, "get_memory_graph", memory_type))
                dendrogram = asyncio.run(_request(handler, "get_dendrogram", memory_type))
                temporal = asyncio.run(
                    _request(handler, "get_temporal_evolution_map", memory_type)
                )

                self.assertEqual(graph["status"], "success")
                self.assertEqual(graph["n_points"], 1)
                self.assertEqual(graph["edges"], [])
                self.assertEqual(dendrogram["status"], "success")
                self.assertEqual(dendrogram["n_points"], 1)
                self.assertEqual(dendrogram["linkage_matrix"], [])
                self.assertEqual(dendrogram["n_texts"], 1)
                self.assertEqual(temporal["ages"], [11.0])

    def test_empty_layers_report_zero_active_projection_diagnostics(self):
        handler = _handler(0)
        for memory_type in ("stm", "ltm"):
            for command in (
                "get_memory_graph",
                "get_dendrogram",
                "get_temporal_evolution_map",
            ):
                with self.subTest(memory_type=memory_type, command=command):
                    result = asyncio.run(_request(handler, command, memory_type))
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["code"], "NOT_ENOUGH_DATA")
                    self.assertEqual(result["response_version"], 2)
                    self.assertEqual(result["n_active"], 0)
                    self.assertEqual(result["n_active_flag"], 0)
                    self.assertEqual(result["n_h_positive"], 0)
                    self.assertEqual(result["n_texts"], 0)
                    self.assertEqual(result["memory_type"], memory_type)


if __name__ == "__main__":
    unittest.main()
