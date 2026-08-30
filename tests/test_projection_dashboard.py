"""Offline regressions for projection response-v2 dashboard handling."""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_normalizer():
    """Load the pure projection normalizer without importing optional PyQt6."""
    source = (SRC / "memory_module" / "dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_normalize_projection_result"
    ]
    if len(selected) != 1:
        raise AssertionError("dashboard must define one projection normalizer")
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "dashboard.py", "exec"), namespace)
    return namespace["_normalize_projection_result"]


def _payload(memory_type: str, count: int) -> dict:
    indices = [2 * i for i in range(count)]
    ids = [f"{memory_type}-{i}" for i in range(count)]
    linkage = []
    if count == 2:
        linkage = [[0, 1, 0.25, 2]]
    elif count == 3:
        linkage = [[0, 1, 0.25, 2], [2, 3, 0.75, 3]]
    edges = [[0, 1, 0.8]] if count > 1 else []
    edge_records = (
        [{"source": ids[0], "target": ids[1], "weight": 0.8}]
        if count > 1
        else []
    )
    return {
        "status": "success",
        "response_version": 2,
        "memory_type": memory_type,
        "n_points": count,
        "indices": indices,
        "key_texts": [f"{memory_type.upper()} key {i}" for i in range(count)],
        "value_texts": [f"{memory_type.upper()} value {i}" for i in range(count)],
        "memory_ids": ids,
        "provenances": [{"source_class": "test"} for _ in range(count)],
        "intensities": [float(i + 1) for i in range(count)],
        "usages": [10 * (i + 1) for i in range(count)],
        "ages": [11.0 * (i + 1) for i in range(count)],
        "linkage_matrix": linkage,
        "linkage": linkage,
        "edges": edges,
        "edge_records": edge_records,
        "nodes": [
            {
                "id": memory_id,
                "memory_id": memory_id,
                "center_index": index,
            }
            for memory_id, index in zip(ids, indices)
        ],
    }


class ProjectionPayloadTests(unittest.TestCase):
    def test_zero_one_and_multiple_records_are_aligned_for_both_sources(self):
        normalize = _load_normalizer()
        for memory_type in ("stm", "ltm"):
            for count in (0, 1, 3):
                with self.subTest(memory_type=memory_type, count=count):
                    data = normalize(_payload(memory_type, count))
                    self.assertEqual(data["memory_type"], memory_type)
                    self.assertEqual(data["n_points"], count)
                    for field in (
                        "indices",
                        "key_texts",
                        "value_texts",
                        "memory_ids",
                        "provenances",
                        "intensities",
                        "usages",
                        "ages",
                        "nodes",
                    ):
                        self.assertEqual(len(data[field]), count, field)

    def test_graph_keeps_local_edges_and_stable_id_edge_records_separate(self):
        data = _load_normalizer()(_payload("ltm", 3))
        self.assertEqual(data["edges"], [[0, 1, 0.8]])
        self.assertEqual(
            data["edge_records"],
            [{"source": "ltm-0", "target": "ltm-1", "weight": 0.8}],
        )

    def test_missing_optional_arrays_default_but_explicit_misalignment_is_rejected(self):
        normalize = _load_normalizer()
        partial = {
            "status": "success",
            "response_version": 2,
            "memory_type": "stm",
            "n_points": 2,
        }
        data = normalize(partial)
        self.assertEqual(data["indices"], [0, 1])
        self.assertEqual(data["key_texts"], ["", ""])

        partial["indices"] = [0]
        with self.assertRaisesRegex(ValueError, "indices"):
            normalize(partial)

    def test_invalid_versions_sources_and_edges_are_rejected(self):
        normalize = _load_normalizer()
        for mutation in (
            {"response_version": 99},
            {"memory_type": "disk"},
            {"edges": [[0, 4, 0.8]]},
            {"edge_records": [{"source": "a"}]},
        ):
            payload = _payload("stm", 2)
            payload.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                normalize(payload)


@unittest.skipUnless(importlib.util.find_spec("PyQt6"), "PyQt6 is optional")
class ProjectionWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _windows(self):
        from memory_module.dashboard import (
            DendrogramWindow,
            GraphMapWindow,
            TemporalEvolutionWindow,
        )

        return (
            DendrogramWindow(None, None, None),
            TemporalEvolutionWindow(None, None, None),
            GraphMapWindow(None, None, None),
        )

    def test_projection_windows_default_to_ltm_and_source_changes_fetch_once(self):
        for window in self._windows():
            with self.subTest(window=type(window).__name__):
                self.assertEqual(window._current_memory_type, "ltm")
                self.assertEqual(window._source_combo.currentData(), "ltm")
                fetch_name = {
                    "DendrogramWindow": "_fetch_dendrogram",
                    "TemporalEvolutionWindow": "_fetch_temporal_map",
                    "GraphMapWindow": "_fetch_graph",
                }[type(window).__name__]
                with patch.object(window, fetch_name) as fetch:
                    window._source_combo.setCurrentIndex(
                        window._source_combo.findData("stm")
                    )
                    self.assertEqual(window._current_memory_type, "stm")
                    fetch.assert_called_once_with()
                window.deleteLater()

    def test_all_windows_render_aligned_one_and_multiple_record_payloads(self):
        for memory_type in ("stm", "ltm"):
            for count in (1, 3):
                for window in self._windows():
                    with self.subTest(
                        memory_type=memory_type,
                        count=count,
                        window=type(window).__name__,
                    ):
                        window._current_memory_type = memory_type
                        window._on_result(_payload(memory_type, count))
                        self.assertEqual(window._indices, [2 * i for i in range(count)])
                        self.assertEqual(window._chart._n_points if hasattr(window._chart, "_n_points") else len(window._chart._nodes), count)
                        if type(window).__name__ == "TemporalEvolutionWindow":
                            self.assertEqual(len(window._chart._ages), count)
                        if type(window).__name__ == "GraphMapWindow":
                            self.assertEqual(window._chart._raw_edges, _payload(memory_type, count)["edges"])
                            self.assertEqual(window._edge_records, _payload(memory_type, count)["edge_records"])
                            self.assertEqual(
                                [node.label_text for node in window._chart._nodes],
                                _payload(memory_type, count)["key_texts"],
                            )
                        pixmap = window.grab()
                        self.assertFalse(pixmap.isNull())
                        window.deleteLater()

    def test_empty_error_partial_and_stale_payloads_do_not_crash_or_leave_old_data(self):
        for window in self._windows():
            with self.subTest(window=type(window).__name__):
                window._on_result(_payload("ltm", 3))
                window._on_result({
                    "status": "error",
                    "code": "NOT_ENOUGH_DATA",
                    "response_version": 2,
                    "memory_type": "ltm",
                    "n_active": 0,
                    "n_active_flag": 0,
                    "n_h_positive": 0,
                    "n_texts": 0,
                })
                self.assertEqual(window._indices, [])
                self.assertTrue(window._status_lbl.text())

                window._on_result({
                    "status": "success",
                    "response_version": 2,
                    "memory_type": "ltm",
                    "n_points": 2,
                    "indices": [0],
                })
                self.assertTrue(window._status_lbl.text())

                window._on_result(_payload("ltm", 1))
                window._current_memory_type = "stm"
                window._on_result(_payload("ltm", 3))
                self.assertEqual(window._indices, [0])
                window.deleteLater()


if __name__ == "__main__":
    unittest.main()
