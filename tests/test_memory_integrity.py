"""Regression tests for memory-center metadata integrity."""

import os
import sys
import tempfile
import threading
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import torch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory_module.memory_centers import MemoryCenters  # noqa: E402
from memory_module.bdbm_container import load_bdbm, save_bdbm  # noqa: E402
from memory_module.consolidation import SleepConsolidator  # noqa: E402
from memory_module.terrain_3d import Terrain3D  # noqa: E402
from memory_module.text_memory import TextMemory  # noqa: E402


def _make_centers() -> MemoryCenters:
    torch.manual_seed(0)
    return MemoryCenters(
        n_centers=8,
        d_key=4,
        d_value=3,
        d_emotion=2,
        use_hybrid_metric=False,
    )


def _make_lightweight_text_memory() -> TextMemory:
    class FakeEmbedder:
        @staticmethod
        def encode(text):
            if "alpha" in text.lower():
                return torch.tensor([1.0, 0.0, 0.0, 0.0])
            if "beta" in text.lower():
                return torch.tensor([0.0, 1.0, 0.0, 0.0])
            return torch.tensor([0.0, 0.0, 1.0, 0.0])

    class FakeProjections:
        @staticmethod
        def project_to_ltm(embedding):
            return embedding

        @staticmethod
        def project_to_stm(embedding):
            return embedding

        @staticmethod
        def project_to_value(embedding):
            return embedding[:, :3]

        @staticmethod
        def project_to_context(embedding):
            return embedding.repeat(1, 4)

        @staticmethod
        def ltm_to_3d(key):
            return key[:, :3]

        @staticmethod
        def stm_to_3d(key):
            return key[:, :3]

    class NoOpTerrain:
        @staticmethod
        def splat(*args, **kwargs):
            return None

    class NoOpConsolidator:
        @staticmethod
        def step(**kwargs):
            return None

    memory = TextMemory.__new__(TextMemory)
    memory._lock = threading.RLock()
    memory.device = "cpu"
    memory.config = SimpleNamespace(
        d_emotion=4,
        stm_top_k_write=3,
        stm_new_center_threshold=0.9,
        terrain_stm_eta=1.0,
        auto_save=False,
        auto_save_interval=1,
    )
    memory.embedder = FakeEmbedder()
    memory.projections = FakeProjections()
    memory.ltm_centers = MemoryCenters(8, 4, 3, d_emotion=4, use_hybrid_metric=False)
    memory.stm_centers = MemoryCenters(8, 4, 3, d_emotion=4, use_hybrid_metric=False)
    memory.ltm_terrain = NoOpTerrain()
    memory.stm_terrain = NoOpTerrain()
    memory.automatic_consolidator = NoOpConsolidator()
    memory._compute_write_strength = lambda *args, **kwargs: torch.tensor([1.0])
    memory.write_count = 0
    memory.read_count = 0
    memory.consolidation_count = 0
    memory.step_count = 0
    return memory


def _write(
    centers: MemoryCenters,
    key: torch.Tensor,
    key_text: str,
    value_text: str,
    *,
    top_k: int = 3,
    threshold: float = 0.9,
    context: torch.Tensor = None,
    terrain: torch.Tensor = None,
    age: int = 0,
    memory_id: str = None,
    provenance: dict = None,
    return_results: bool = False,
):
    return centers.write(
        keys=key.unsqueeze(0),
        values=torch.tensor([[0.2, 0.4, 0.6]]),
        emotions=torch.tensor([[1.0, 1.0]]),
        intensities=torch.tensor([1.0]),
        top_k=top_k,
        new_center_threshold=threshold,
        context_keys=(context if context is not None else torch.tensor([0.1] * 16)).unsqueeze(0),
        terrain_positions=(terrain if terrain is not None else torch.tensor([0.2, 0.3, 0.4])).unsqueeze(0),
        key_texts=[key_text],
        value_texts=[value_text],
        ages=[age],
        memory_ids=[memory_id],
        provenances=[provenance],
        return_results=return_results,
    )


def test_shared_repeat_does_not_relabel_unrelated_top_k_centers():
    centers = _make_centers()
    facts = (
        (torch.tensor([1.0, 0.0, 0.0, 0.0]), "AZURE-PINE-482", "HTTP harbor"),
        (torch.tensor([0.0, 1.0, 0.0, 0.0]), "ORBITAL-MINT-731", "shared constellation"),
        (torch.tensor([0.0, 0.0, 1.0, 0.0]), "SILVER-KESTREL-593", "WS observatory"),
    )

    for key, key_text, value_text in facts:
        assert _write(centers, key, key_text, value_text) == 1

    assert _write(
        centers,
        facts[1][0],
        facts[1][1],
        facts[1][2],
        top_k=3,
        threshold=0.9,
    ) == 0

    active = torch.where(centers.active)[0].tolist()
    assert len(active) == 3
    assert [centers.key_texts[i] for i in active] == [fact[1] for fact in facts]
    assert [centers.value_texts[i] for i in active] == [fact[2] for fact in facts]


def test_store_record_and_recall_expose_authoritative_metadata():
    memory = _make_lightweight_text_memory()
    created = memory.store_record(
        "alpha key",
        "alpha value",
        memory_id="alpha-id",
        provenance={"source_class": "mcp", "session_id": "session-a"},
    )
    assert created == {
        "index": 0,
        "memory_id": "alpha-id",
        "created": True,
        "reinforced": False,
        "status": "created",
        "layer": "stm",
        "key_text": "alpha key",
        "value_text": "alpha value",
        "provenance": created["provenance"],
        "new_centers": 1,
    }
    assert created["provenance"]["source_class"] == "mcp"

    duplicate = memory.store_record(
        "beta key",
        "beta value",
        memory_id="alpha-id",
        provenance={"source_class": "mcp", "session_id": "session-b"},
    )
    assert duplicate == {
        "index": None,
        "memory_id": None,
        "created": False,
        "reinforced": False,
        "status": "duplicate_memory_id",
        "layer": None,
        "key_text": None,
        "value_text": None,
        "provenance": None,
        "new_centers": 0,
    }
    assert memory.stm_centers.get_n_active() == 1

    reinforced = memory.store_record(
        "alpha key",
        "alpha value updated",
        memory_id="replacement-id",
        provenance={"source_class": "browser", "origin": "chat.example"},
    )
    assert reinforced["status"] == "reinforced"
    assert reinforced["created"] is False
    assert reinforced["reinforced"] is True
    assert reinforced["memory_id"] == "alpha-id"
    assert reinforced["index"] == 0
    assert reinforced["layer"] == "stm"
    assert reinforced["key_text"] == "alpha key"
    assert reinforced["value_text"] == "alpha value updated"
    assert reinforced["new_centers"] == 0
    assert reinforced["provenance"]["source_history"] == [
        {"source_class": "mcp", "session_id": "session-a"},
        {"source_class": "browser", "origin": "chat.example"},
    ]

    assert memory.store("beta key", "beta value") == 1
    recalled = memory.recall("alpha key", top_k=1)
    assert recalled.text == "alpha value updated"
    assert recalled.memory_id == "alpha-id"
    assert recalled.layer == "stm"
    assert recalled.provenance["source_class"] == "mcp"
    assert recalled.matches[0]["memory_id"] == "alpha-id"
    assert recalled.matches[0]["layer"] == "stm"
    assert recalled.matches[0]["source"] == "STM"
    assert recalled.matches[0]["index"] == 0

    query = torch.tensor([[[1.0, 0.0, 0.0, 0.0]]])
    legacy_texts = memory.stm_centers.read_compound_with_text(query, top_k=1)[-1]
    assert legacy_texts[0][:2] == ("alpha key", "alpha value updated")
    assert len(legacy_texts[0]) == 3


def test_metadata_isolation_holds_for_top_k_variants():
    keys = torch.eye(4)[:3]
    expected = ["fact-a", "fact-b", "fact-c"]

    for top_k in (1, 2, 3, 8):
        centers = _make_centers()
        for key, text in zip(keys, expected):
            assert _write(centers, key, text, f"value-{text}") == 1
        assert _write(
            centers,
            keys[1],
            expected[1],
            "value-fact-b",
            top_k=top_k,
        ) == 0
        assert centers.key_texts[:3] == expected


def test_only_winner_receives_canonical_metadata_while_top_k_learns():
    centers = _make_centers()
    keys = torch.eye(4)[:3]
    for index, key in enumerate(keys):
        assert _write(
            centers,
            key,
            f"fact-{index}",
            f"value-{index}",
            context=torch.full((16,), float(index)),
            terrain=torch.full((3,), float(index)),
            memory_id=f"memory-{index}",
            provenance={"source_class": f"source-{index}"},
        ) == 1

    h_before = centers.h[:3].clone()
    contexts_before = centers.K_context[:3].clone()
    terrain_before = centers.K_terrain[:3].clone()
    ids_before = list(centers.memory_ids[:3])

    assert _write(
        centers,
        keys[1],
        "fact-1-updated",
        "value-1-updated",
        context=torch.full((16,), 9.0),
        terrain=torch.full((3,), 9.0),
        age=7,
        memory_id="replacement-id-must-not-win",
        provenance={"source_class": "browser", "origin": "example.test"},
    ) == 0

    assert torch.all(centers.h[:3] > h_before)
    assert torch.equal(centers.K_context[0], contexts_before[0])
    assert torch.equal(centers.K_context[2], contexts_before[2])
    assert torch.equal(centers.K_terrain[0], terrain_before[0])
    assert torch.equal(centers.K_terrain[2], terrain_before[2])
    assert torch.equal(centers.K_context[1], torch.full((16,), 9.0))
    assert torch.equal(centers.K_terrain[1], torch.full((3,), 9.0))
    assert centers.key_texts[:3] == ["fact-0", "fact-1-updated", "fact-2"]
    assert centers.value_texts[:3] == ["value-0", "value-1-updated", "value-2"]
    assert centers.age[:3].tolist() == [0, 7, 0]
    assert centers.memory_ids[:3] == ids_before
    assert centers.provenances[1]["source_history"] == [
        {"source_class": "source-1"},
        {"source_class": "browser", "origin": "example.test"},
    ]


def test_identity_and_provenance_round_trip_and_legacy_backfill():
    centers = _make_centers()
    key = torch.tensor([1.0, 0.0, 0.0, 0.0])
    provenance = {
        "source_class": "browser",
        "origin": "chat.example",
        "session_id": "session-7",
        "created_at": "2026-08-27T10:00:00+00:00",
        "updated_at": "2026-08-27T10:00:00+00:00",
    }
    assert _write(
        centers,
        key,
        "stable fact",
        "stable value",
        memory_id="stable-id",
        provenance=provenance,
    ) == 1

    state = centers.state_dict_custom()
    assert state["record_metadata_version"] == 1
    state["key_texts"][0] = "mutated snapshot key"
    state["value_texts"][0] = "mutated snapshot value"
    state["memory_ids"][0] = "mutated-snapshot-id"
    state["provenances"][0]["origin"] = "mutated-snapshot.example"
    state["provenances"][0]["source_history"][0]["origin"] = "mutated-history.example"
    assert centers.key_texts[0] == "stable fact"
    assert centers.value_texts[0] == "stable value"
    assert centers.memory_ids[0] == "stable-id"
    assert centers.provenances[0]["origin"] == "chat.example"
    assert centers.provenances[0]["source_history"][0]["origin"] == "chat.example"

    state = centers.state_dict_custom()
    restored = MemoryCenters.from_state_dict(state)
    assert restored.memory_ids[0] == "stable-id"
    assert restored.provenances[0]["origin"] == "chat.example"
    assert restored.provenances[0]["source_history"] == [
        {
            "source_class": "browser",
            "origin": "chat.example",
            "session_id": "session-7",
        }
    ]
    restored.provenances[0]["origin"] = "mutated-restored.example"
    restored.provenances[0]["source_history"][0]["origin"] = "mutated-restored-history.example"
    assert state["provenances"][0]["origin"] == "chat.example"
    assert state["provenances"][0]["source_history"][0]["origin"] == "chat.example"
    assert centers.provenances[0]["origin"] == "chat.example"

    legacy_state = dict(state)
    legacy_state.pop("memory_ids")
    legacy_state.pop("provenances")
    legacy_a = MemoryCenters.from_state_dict(legacy_state)
    legacy_b = MemoryCenters.from_state_dict(legacy_state)
    assert legacy_a.memory_ids[0] == legacy_b.memory_ids[0]
    assert legacy_a.provenances[0]["source_class"] == "unknown"
    assert len(legacy_a.memory_ids) == legacy_a.n_centers
    assert len(legacy_a.provenances) == legacy_a.n_centers


def test_provenance_history_is_deduplicated_and_bounded_to_recent_events():
    centers = _make_centers()
    key = torch.tensor([1.0, 0.0, 0.0, 0.0])
    assert _write(
        centers,
        key,
        "bounded fact",
        "bounded value",
        memory_id="bounded-id",
        provenance={
            "source_class": "source-0",
            "origin": "canonical.example",
            "session_id": "session-0",
        },
    ) == 1

    for index in range(1, 201):
        assert _write(
            centers,
            key,
            "bounded fact",
            "bounded value",
            provenance={
                "source_class": f"source-{index}",
                "origin": f"origin-{index}.example",
                "session_id": f"session-{index}",
            },
        ) == 0

    provenance = centers.provenances[0]
    history = provenance["source_history"]
    assert len(history) == MemoryCenters.MAX_PROVENANCE_HISTORY == 32
    assert [event["source_class"] for event in history] == [
        f"source-{index}" for index in range(169, 201)
    ]
    assert provenance["source_class"] == "source-0"
    assert provenance["origin"] == "canonical.example"
    assert provenance["session_id"] == "session-0"

    repeated_event = {
        "source_class": "source-190",
        "origin": "origin-190.example",
        "session_id": "session-190",
    }
    assert _write(
        centers,
        key,
        "bounded fact",
        "bounded value",
        provenance=repeated_event,
    ) == 0
    history = centers.provenances[0]["source_history"]
    assert len(history) == 32
    assert history[-1] == repeated_event
    assert history.count(repeated_event) == 1


def test_concurrent_reinforcement_preserves_identity_and_bounded_history():
    memory = _make_lightweight_text_memory()
    created = memory.store_record(
        "alpha key",
        "alpha value",
        memory_id="concurrent-id",
        provenance={"source_class": "initial", "session_id": "initial-session"},
    )
    assert created["created"] is True

    def reinforce(index):
        return memory.store_record(
            "alpha key",
            "alpha value",
            provenance={"source_class": "mcp", "session_id": f"session-{index}"},
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(reinforce, range(64)))

    assert all(result["reinforced"] for result in results)
    assert {result["memory_id"] for result in results} == {"concurrent-id"}
    provenance = memory.stm_centers.provenances[0]
    history = provenance["source_history"]
    assert len(history) == MemoryCenters.MAX_PROVENANCE_HISTORY
    assert len({(event.get("source_class"), event.get("session_id")) for event in history}) == 32
    assert provenance["source_class"] == "initial"
    assert provenance["session_id"] == "initial-session"

    state = memory.stm_centers.state_dict_custom()
    restored = MemoryCenters.from_state_dict(state)
    assert restored.memory_ids[0] == "concurrent-id"
    assert len(restored.provenances[0]["source_history"]) == 32


def test_duplicate_caller_id_fails_closed_and_legacy_ids_are_repaired():
    centers = _make_centers()
    first_key = torch.tensor([1.0, 0.0, 0.0, 0.0])
    second_key = torch.tensor([0.0, 1.0, 0.0, 0.0])
    assert _write(
        centers,
        first_key,
        "first logical record",
        "first value",
        memory_id="caller-id",
    ) == 1

    created, results = _write(
        centers,
        second_key,
        "different logical record",
        "different value",
        memory_id="caller-id",
        return_results=True,
    )
    assert created == 0
    assert results == [{
        "index": None,
        "memory_id": None,
        "created": False,
        "reinforced": False,
        "status": "duplicate_memory_id",
    }]
    assert centers.get_n_active() == 1
    assert centers.key_texts[0] == "first logical record"

    created, results = _write(
        centers,
        first_key,
        "first logical record",
        "updated first value",
        memory_id="caller-id",
        return_results=True,
    )
    assert created == 0
    assert results[0]["status"] == "reinforced"
    assert results[0]["memory_id"] == "caller-id"
    assert centers.value_texts[0] == "updated first value"

    assert _write(
        centers,
        second_key,
        "second logical record",
        "second value",
        memory_id="second-id",
    ) == 1
    legacy_state = centers.state_dict_custom()
    legacy_state["memory_ids"][1] = "caller-id"
    repaired = MemoryCenters.from_state_dict(legacy_state)
    active_ids = [
        repaired.memory_ids[index]
        for index in torch.where(repaired.active)[0].tolist()
    ]
    assert active_ids[0] == "caller-id"
    assert len(active_ids) == len(set(active_ids)) == 2

    round_trip = MemoryCenters.from_state_dict(repaired.state_dict_custom())
    round_trip_ids = [
        round_trip.memory_ids[index]
        for index in torch.where(round_trip.active)[0].tolist()
    ]
    assert round_trip_ids == active_ids
    assert len(round_trip_ids) == len(set(round_trip_ids))

    memory = TextMemory.__new__(TextMemory)
    memory._lock = threading.RLock()
    memory.ltm_centers = _make_centers()
    memory.stm_centers = round_trip
    cursor_keys = [
        (record["layer"], record["memory_id"])
        for record in memory.list_memories(source="stm")
    ]
    assert len(cursor_keys) == len(set(cursor_keys))


def test_public_record_provenance_is_mutation_isolated():
    memory = _make_lightweight_text_memory()
    memory.store_record(
        "alpha key",
        "alpha value",
        memory_id="isolated-id",
        provenance={"source_class": "mcp", "session_id": "private-session"},
    )

    listed = memory.list_memories(source="stm")
    listed[0]["provenance"]["source_class"] = "mutated-list"
    listed[0]["provenance"]["source_history"][0]["session_id"] = "mutated-list-session"
    assert memory.stm_centers.provenances[0]["source_class"] == "mcp"
    assert memory.stm_centers.provenances[0]["source_history"][0]["session_id"] == "private-session"

    searched = memory.search("alpha key", top_k=1, source="stm")
    searched[0]["provenance"]["source_class"] = "mutated-search"
    searched[0]["provenance"]["source_history"][0]["session_id"] = "mutated-search-session"
    assert memory.stm_centers.provenances[0]["source_class"] == "mcp"
    assert memory.stm_centers.provenances[0]["source_history"][0]["session_id"] == "private-session"


def test_explicit_forget_scrubs_sensitive_slot_and_persisted_metadata():
    centers = _make_centers()
    secret_key = "FORGOTTEN-KEY-SECRET"
    secret_value = "FORGOTTEN-VALUE-SECRET"
    secret_id = "forgotten-id-secret"
    secret_origin = "forgotten-origin.example"
    secret_session = "forgotten-session-secret"
    assert _write(
        centers,
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        secret_key,
        secret_value,
        memory_id=secret_id,
        provenance={
            "source_class": "browser",
            "origin": secret_origin,
            "session_id": secret_session,
        },
    ) == 1

    memory = TextMemory.__new__(TextMemory)
    memory._lock = threading.RLock()
    memory.ltm_centers = _make_centers()
    memory.stm_centers = centers
    assert memory.forget(secret_key, exact_match=True, source="stm") == 1

    assert not centers.active[0]
    for tensor in (
        centers.K,
        centers.K_context,
        centers.K_terrain,
        centers.V,
        centers.h,
        centers.e,
        centers.usage,
        centers.age,
    ):
        assert torch.count_nonzero(tensor[0]).item() == 0
    assert centers.key_texts[0] is None
    assert centers.value_texts[0] is None
    assert centers.memory_ids[0] is None
    assert centers.provenances[0] is None

    state = {"version": "1.0", "stats": {}, "stm_centers": centers.state_dict_custom()}
    state_text = repr(state)
    for secret in (secret_key, secret_value, secret_id, secret_origin, secret_session):
        assert secret not in state_text

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "forgotten.bdbm")
        save_bdbm(state, path)
        with open(path, "rb") as container_file:
            blob = container_file.read()
        assert blob.startswith(b"BDBMZIP01")
        with zipfile.ZipFile(io.BytesIO(blob[len(b"BDBMZIP01"):])) as archive:
            metadata = json.loads(archive.read("metadata.json"))
        loaded = load_bdbm(path)

    metadata_text = json.dumps(metadata)
    loaded_text = repr(loaded)
    for secret in (secret_key, secret_value, secret_id, secret_origin, secret_session):
        assert secret.encode() not in blob
        assert secret not in metadata_text
        assert secret not in loaded_text


def test_bdbm_container_keeps_identity_and_provenance_in_metadata():
    centers = _make_centers()
    assert _write(
        centers,
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        "portable fact",
        "portable value",
        memory_id="portable-id",
        provenance={"source_class": "cli", "session_id": "local-session"},
    ) == 1
    state = {
        "version": "1.0",
        "stats": {},
        "stm_centers": centers.state_dict_custom(),
    }

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "memory.bdbm")
        save_bdbm(state, path)
        loaded = load_bdbm(path)

    restored = MemoryCenters.from_state_dict(loaded["stm_centers"])
    assert restored.memory_ids[0] == "portable-id"
    assert restored.provenances[0]["source_class"] == "cli"
    assert restored.provenances[0]["session_id"] == "local-session"


def test_consolidation_transfers_identity_and_provenance():
    stm = MemoryCenters(4, 4, 3, d_emotion=2, use_hybrid_metric=False)
    ltm = MemoryCenters(4, 6, 3, d_emotion=2, use_hybrid_metric=False)
    assert _write(
        stm,
        torch.tensor([1.0, 0.0, 0.0, 0.0]),
        "consolidated fact",
        "consolidated value",
        memory_id="consolidated-id",
        provenance={"source_class": "browser", "origin": "chat.example"},
    ) == 1
    consolidator = SleepConsolidator(
        d_stm_key=4,
        d_ltm_key=6,
        d_value=3,
        d_emotion=2,
        consolidation_top_m=4,
        ltm_new_center_threshold=1.1,
    )
    stm_terrain = Terrain3D(resolution=8, n_emotions=2)
    ltm_terrain = Terrain3D(resolution=8, n_emotions=2)

    result = consolidator.consolidate(stm, ltm, stm_terrain, ltm_terrain)

    assert result["status"] == "success"
    assert result["new_ltm_centers"] == 1
    assert ltm.memory_ids[0] == "consolidated-id"
    assert ltm.provenances[0]["origin"] == "chat.example"
    assert ltm.key_texts[0] == "consolidated fact"

    second_result = consolidator.consolidate(stm, ltm, stm_terrain, ltm_terrain)
    assert second_result["status"] == "success"
    assert second_result["new_ltm_centers"] == 0
    assert ltm.get_n_active() == 1
    assert ltm.memory_ids[0] == "consolidated-id"


def test_merge_edit_and_forget_keep_metadata_aligned():
    centers = _make_centers()
    duplicate_key = torch.tensor([1.0, 0.0, 0.0, 0.0])
    centers._create_new_center(
        duplicate_key,
        torch.tensor([1.0, 0.0, 0.0]),
        torch.tensor([1.0, 1.0]),
        1.0,
        key_text="merge fact",
        value_text="old value",
        memory_id="winner-id",
        provenance={"source_class": "cli"},
    )
    centers._create_new_center(
        duplicate_key,
        torch.tensor([0.0, 1.0, 0.0]),
        torch.tensor([1.0, 1.0]),
        1.0,
        key_text="duplicate fact",
        value_text="duplicate value",
        memory_id="merged-id",
        provenance={"source_class": "browser"},
    )

    assert centers.merge_similar(threshold=0.99) == 1
    assert centers.get_n_active() == 1
    assert centers.memory_ids[0] == "winner-id"
    assert centers.provenances[0]["source_history"] == [
        {"source_class": "cli"},
        {"source_class": "browser"},
    ]

    memory = TextMemory.__new__(TextMemory)
    memory._lock = threading.RLock()
    memory.ltm_centers = centers
    memory.stm_centers = _make_centers()
    assert memory.edit("old value", "new value", source="ltm") == 1
    assert centers.memory_ids[0] == "winner-id"
    assert centers.value_texts[0] == "new value"
    assert memory.forget("merge fact", exact_match=True, source="ltm") == 1
    assert not centers.active[0]
    assert centers.memory_ids[0] is None
    assert centers.provenances[0] is None
    assert centers.key_texts[0] is None
    assert centers.value_texts[0] is None


def test_capacity_exhaustion_does_not_overwrite_existing_records():
    centers = MemoryCenters(2, 4, 3, d_emotion=2, use_hybrid_metric=False)
    assert _write(centers, torch.eye(4)[0], "first", "one") == 1
    assert _write(centers, torch.eye(4)[1], "second", "two") == 1
    assert _write(centers, torch.eye(4)[2], "third", "three") == 0
    assert centers.get_n_active() == 2
    assert centers.key_texts == ["first", "second"]


def main():
    test_shared_repeat_does_not_relabel_unrelated_top_k_centers()
    test_store_record_and_recall_expose_authoritative_metadata()
    test_metadata_isolation_holds_for_top_k_variants()
    test_only_winner_receives_canonical_metadata_while_top_k_learns()
    test_identity_and_provenance_round_trip_and_legacy_backfill()
    test_provenance_history_is_deduplicated_and_bounded_to_recent_events()
    test_concurrent_reinforcement_preserves_identity_and_bounded_history()
    test_duplicate_caller_id_fails_closed_and_legacy_ids_are_repaired()
    test_public_record_provenance_is_mutation_isolated()
    test_explicit_forget_scrubs_sensitive_slot_and_persisted_metadata()
    test_bdbm_container_keeps_identity_and_provenance_in_metadata()
    test_consolidation_transfers_identity_and_provenance()
    test_merge_edit_and_forget_keep_metadata_aligned()
    test_capacity_exhaustion_does_not_overwrite_existing_records()
    print("MEMORY INTEGRITY OK — all assertions passed")


if __name__ == "__main__":
    main()
