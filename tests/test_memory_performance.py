"""Deterministic performance gates for the 256-record memory workload.

The benchmark deliberately replaces the production embedder and terrain with
small local fakes.  It therefore measures memory-engine overhead only and can
run without a model cache, network access, or downloads.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import torch


if sys.platform != "win32":
    import resource


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_module.bdbm_container import load_bdbm, save_bdbm  # noqa: E402
from memory_module.memory_centers import MemoryCenters  # noqa: E402
from memory_module.text_memory import TextMemory  # noqa: E402


_RECORD_COUNT = 256
_CAPACITY = 320
_EMBEDDING_DIM = 16
_VALUE_DIM = 8
_SAMPLE_COUNT = 24

_FAST_P95_MS = 5.0
_LIST_AND_BACKFILL_P95_MS = 10.0
_PERSISTENCE_P95_MS = 25.0
_MAX_CONTAINER_BYTES = 256 * 1024
_MAX_PEAK_RSS_DELTA_BYTES = 32 * 1024 * 1024


def _vector_for(text: str) -> torch.Tensor:
    """Return a stable, normalized embedding without model or network I/O."""
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=_EMBEDDING_DIM).digest()
    vector = torch.tensor([byte - 127.5 for byte in digest], dtype=torch.float32)
    return torch.nn.functional.normalize(vector, dim=0)


class _DeterministicEmbedder:
    @staticmethod
    def encode(text: str) -> torch.Tensor:
        return _vector_for(text)


class _IdentityProjections:
    @staticmethod
    def project_to_ltm(embedding: torch.Tensor) -> torch.Tensor:
        return embedding

    @staticmethod
    def project_to_stm(embedding: torch.Tensor) -> torch.Tensor:
        return embedding

    @staticmethod
    def project_to_value(embedding: torch.Tensor) -> torch.Tensor:
        return embedding[:, :_VALUE_DIM]

    @staticmethod
    def project_to_context(embedding: torch.Tensor) -> torch.Tensor:
        return embedding

    @staticmethod
    def ltm_to_3d(key: torch.Tensor) -> torch.Tensor:
        return key[:, :3]

    @staticmethod
    def stm_to_3d(key: torch.Tensor) -> torch.Tensor:
        return key[:, :3]


class _NoOpTerrain:
    @staticmethod
    def splat(*args, **kwargs) -> None:
        return None


class _NoOpConsolidator:
    @staticmethod
    def step(**kwargs) -> None:
        return None


def _make_centers() -> MemoryCenters:
    return MemoryCenters(
        n_centers=_CAPACITY,
        d_key=_EMBEDDING_DIM,
        d_value=_VALUE_DIM,
        d_emotion=4,
        use_hybrid_metric=False,
        device="cpu",
    )


def _make_memory() -> TextMemory:
    """Build a 256-record TextMemory instance without loading an embedder."""
    torch.manual_seed(0)
    memory = TextMemory.__new__(TextMemory)
    memory._lock = threading.RLock()
    memory.device = "cpu"
    memory.state_file = ""
    memory.config = SimpleNamespace(
        d_emotion=4,
        stm_top_k_write=3,
        stm_new_center_threshold=0.999999,
        terrain_stm_eta=1.0,
        auto_save=False,
        auto_save_interval=1,
    )
    memory.embedder = _DeterministicEmbedder()
    memory.projections = _IdentityProjections()
    memory.ltm_centers = _make_centers()
    memory.stm_centers = _make_centers()
    memory.ltm_terrain = _NoOpTerrain()
    memory.stm_terrain = _NoOpTerrain()
    memory.automatic_consolidator = _NoOpConsolidator()
    memory._compute_write_strength = lambda *args, **kwargs: torch.tensor([1.0])
    memory.write_count = 0
    memory.read_count = 0
    memory.consolidation_count = 0
    memory.step_count = 0

    centers = memory.stm_centers
    for index in range(_RECORD_COUNT):
        key_text = f"memory-key-{index:03d}"
        value_text = f"memory-value-{index:03d}"
        key = _vector_for(key_text)
        centers.K[index].copy_(key)
        centers.K_context[index].copy_(key)
        centers.K_terrain[index].copy_(key[:3])
        centers.V[index].copy_(_vector_for(value_text)[:_VALUE_DIM])
        centers.h[index] = 1.0
        centers.active[index] = True
        centers.key_texts[index] = key_text
        centers.value_texts[index] = value_text
        centers.memory_ids[index] = f"memory-id-{index:03d}"
        centers.provenances[index] = {
            "source_class": "performance-test",
            "origin": "local",
            "session_id": "benchmark",
        }
    return memory


def _sample_ms(operation: Callable[[], object], count: int = _SAMPLE_COUNT) -> list[float]:
    samples = []
    for _ in range(count):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return samples


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _peak_rss_bytes() -> int:
    if sys.platform != "win32":
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)

    # ``resource`` is unavailable on Windows, so query the same process-level
    # peak working-set metric directly without adding psutil as a dependency.
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def _assert_gates(metrics: dict[str, tuple[float, float]]) -> None:
    failures = [
        f"{name}: p95={actual:.3f}ms exceeds {limit:.1f}ms"
        for name, (actual, limit) in metrics.items()
        if actual > limit
    ]
    assert not failures, "performance gate failures:\n" + "\n".join(failures)


def test_256_record_memory_performance_gates() -> None:
    previous_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        memory = _make_memory()
        assert memory.stm_centers.get_n_active() == _RECORD_COUNT

        # Warm lazy PyTorch paths before recording latency or memory growth.
        memory.recall("memory-key-000", top_k=5, increment_stats=False)
        memory.search("memory-key-000", top_k=10, source="stm")
        memory.list_memories(source="both", limit=_RECORD_COUNT)
        memory.store_record("memory-key-000", "warm-reinforcement")

        rss_before = _peak_rss_bytes()
        next_store_index = _RECORD_COUNT

        def store_new_record() -> None:
            nonlocal next_store_index
            result = memory.store_record(
                f"new-key-{next_store_index:03d}",
                f"new-value-{next_store_index:03d}",
                provenance={"source_class": "performance-test"},
            )
            assert result["status"] == "created"
            next_store_index += 1

        reinforcement_index = 0

        def reinforce_record() -> None:
            nonlocal reinforcement_index
            result = memory.store_record(
                "memory-key-000",
                f"reinforced-value-{reinforcement_index:03d}",
                provenance={"source_class": "performance-test"},
            )
            assert result["status"] == "reinforced"
            reinforcement_index += 1

        fast_metrics = {
            "store": (_p95(_sample_ms(store_new_record)), _FAST_P95_MS),
            "reinforce": (_p95(_sample_ms(reinforce_record)), _FAST_P95_MS),
            "recall": (
                _p95(
                    _sample_ms(
                        lambda: memory.recall(
                            "memory-key-127", top_k=5, increment_stats=False
                        )
                    )
                ),
                _FAST_P95_MS,
            ),
            "search": (
                _p95(
                    _sample_ms(
                        lambda: memory.search(
                            "memory-key-127", top_k=10, source="stm"
                        )
                    )
                ),
                _FAST_P95_MS,
            ),
            "list": (
                _p95(
                    _sample_ms(
                        lambda: memory.list_memories(
                            source="both", limit=_RECORD_COUNT
                        )
                    )
                ),
                _LIST_AND_BACKFILL_P95_MS,
            ),
        }

        legacy_centers_state = deepcopy(memory.stm_centers.state_dict_custom())
        legacy_centers_state.pop("memory_ids")
        legacy_centers_state.pop("provenances")
        legacy_centers_state.pop("record_metadata_version")
        backfill_p95 = _p95(
            _sample_ms(
                lambda: MemoryCenters.from_state_dict(legacy_centers_state),
                count=12,
            )
        )

        state = {
            "version": TextMemory.STATE_VERSION,
            "stats": {"write_count": memory.write_count},
            "ltm_centers": memory.ltm_centers.state_dict_custom(),
            "stm_centers": memory.stm_centers.state_dict_custom(),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            container_paths = [
                Path(temp_dir) / f"memory-{index}.bdbm" for index in range(8)
            ]
            save_p95 = _p95(
                [
                    _sample_ms(
                        lambda path=path: save_bdbm(state, str(path)),
                        count=1,
                    )[0]
                    for path in container_paths
                ]
            )
            container_path = container_paths[-1]
            container_bytes = container_path.stat().st_size
            load_p95 = _p95(
                _sample_ms(lambda: load_bdbm(str(container_path)), count=12)
            )

        legacy_state = dict(state)
        legacy_state["version"] = "1.0"
        migration_p95 = _p95(
            _sample_ms(lambda: memory.migrate_state(dict(legacy_state)), count=12)
        )
        rss_delta = max(0, _peak_rss_bytes() - rss_before)

        metrics = {
            **fast_metrics,
            "metadata_backfill": (backfill_p95, _LIST_AND_BACKFILL_P95_MS),
            "container_save": (save_p95, _PERSISTENCE_P95_MS),
            "container_load": (load_p95, _PERSISTENCE_P95_MS),
            "state_migration": (migration_p95, _PERSISTENCE_P95_MS),
        }
        _assert_gates(metrics)
        assert container_bytes <= _MAX_CONTAINER_BYTES, (
            f"container size {container_bytes}B exceeds {_MAX_CONTAINER_BYTES}B"
        )
        assert rss_delta <= _MAX_PEAK_RSS_DELTA_BYTES, (
            f"peak RSS delta {rss_delta}B exceeds {_MAX_PEAK_RSS_DELTA_BYTES}B"
        )
    finally:
        torch.set_num_threads(previous_threads)


def main() -> None:
    test_256_record_memory_performance_gates()
    print("MEMORY PERFORMANCE OK — all 256-record gates passed")


if __name__ == "__main__":
    main()
