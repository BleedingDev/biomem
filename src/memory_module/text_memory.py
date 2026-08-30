# memory_module/text_memory.py — biomem TextMemory
"""
TextMemory - Main API class for text in / text out memory.

Provides a simple interface:
- store(key, value) - storing
- recall(query) -> text - recalling
- step() - homeostasis
- consolidate() - STM→LTM consolidation
- save() / load() - persistence
"""
import logging
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch

from .config import MemoryConfig, DEFAULT_CONFIG
from .embedder import TextEmbedder, EmotionExtractor
from .memory_centers import MemoryCenters
from .consolidation import SleepConsolidator, AutomaticConsolidator
from .projections import ProjectionBundle
from .terrain_3d import Terrain3D
from .bdbm_container import save_bdbm, load_bdbm
from .security import get_data_dir

logger = logging.getLogger('bdbm.text_memory')


@dataclass
class RecallResult:
    """Recall result from memory."""
    text: str
    confidence: float
    source: str
    key_text: str = ''
    emotion: Optional[torch.Tensor] = None
    matches: List[Dict] = field(default_factory=list)
    memory_id: Optional[str] = None
    provenance: Optional[Dict[str, Any]] = None
    layer: str = ''


class TextMemory:
    """
    Main class for text in / text out memory.

    Complete cognitive memory system with:
    - Two-layer architecture (LTM + STM)
    - 3D terrain visualization
    - Automatic consolidation
    - Direct text storage (Variant B)

    Usage:
        memory = TextMemory()
        memory.store("What is the capital of France?", "Paris")
        result = memory.recall("Capital of France?")
        print(result.text)  # "Paris"
    """

    STATE_VERSION = '1.0'

    def __init__(
        self,
        config: Optional[MemoryConfig] = None,
        state_file: Optional[str] = None,
        device: Optional[str] = None,
        auto_load: bool = True,
    ):
        """Initializes the memory system.

        Args:
            config: Memory configuration (or uses DEFAULT_CONFIG)
            state_file: Path to the persistence file
            device: Device ('cpu', 'cuda', or None for auto)
            auto_load: Attempt to load an existing state
        """
        self.config = config or DEFAULT_CONFIG
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')

        # Data directory + state file
        data_dir = Path(get_data_dir())
        data_dir.mkdir(parents=True, exist_ok=True)
        if state_file:
            self.state_file = str(state_file)
        elif self.config.data_dir:
            self.state_file = str(Path(self.config.data_dir) / self.config.state_file)
        else:
            self.state_file = str(data_dir / self.config.state_file)

        # Components
        self.embedder = TextEmbedder(
            model_name=self.config.embedding_model, device=self.device
        )
        self.projections = ProjectionBundle(
            d_embedding=self.config.embedding_dim,
            d_ltm_key=self.config.d_ltm_key,
            d_stm_key=self.config.d_stm_key,
            d_value=self.config.d_value,
        ).to(self.device)

        self.ltm_centers = MemoryCenters(
            n_centers=self.config.n_ltm_centers,
            d_key=self.config.d_ltm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            sigma_read=self.config.ltm_sigma_read,
            sigma_write=self.config.ltm_sigma_write,
            leak=self.config.ltm_leak,
            leak_emotion=self.config.ltm_leak_emotion,
            leak_value=self.config.ltm_leak_value,
            alpha_value=self.config.ltm_alpha_value,
            alpha_emotion=self.config.ltm_alpha_emotion,
            use_hybrid_metric=True,
            minkowski_p=0.5,
            weight_cosine=0.7,
            weight_minkowski=0.3,
            hybrid_candidates=64,
            device=self.device,
        )
        self.stm_centers = MemoryCenters(
            n_centers=self.config.n_stm_centers,
            d_key=self.config.d_stm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            sigma_read=self.config.stm_sigma_read,
            sigma_write=self.config.stm_sigma_write,
            leak=self.config.stm_leak,
            leak_emotion=self.config.stm_leak_emotion,
            leak_value=self.config.stm_leak_value,
            alpha_value=self.config.stm_alpha_value,
            alpha_emotion=self.config.stm_alpha_emotion,
            use_hybrid_metric=True,
            minkowski_p=0.5,
            weight_cosine=0.7,
            weight_minkowski=0.3,
            hybrid_candidates=64,
            device=self.device,
        )
        self.ltm_terrain = Terrain3D(
            resolution=self.config.terrain_resolution,
            n_emotions=self.config.d_emotion,
            alpha_h=self.config.terrain_ltm_alpha_h,
            alpha_e=self.config.terrain_ltm_alpha_e,
            leak=self.config.terrain_ltm_lambda,
            device=self.device,
        )
        self.stm_terrain = Terrain3D(
            resolution=self.config.terrain_resolution,
            n_emotions=self.config.d_emotion,
            alpha_h=self.config.terrain_stm_alpha_h,
            alpha_e=self.config.terrain_stm_alpha_e,
            leak=self.config.terrain_stm_lambda,
            device=self.device,
        )

        self.consolidator = SleepConsolidator(
            d_stm_key=self.config.d_stm_key,
            d_ltm_key=self.config.d_ltm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            fatigue_leak=self.config.fatigue_leak,
            fatigue_threshold=self.config.fatigue_threshold,
            consolidation_top_m=self.config.consolidation_top_m,
            consolidation_kappa=self.config.consolidation_kappa,
            consolidation_min_intensity=self.config.consolidation_min_intensity,
            consolidation_xi_h=self.config.consolidation_xi_h,
            consolidation_xi_e=self.config.consolidation_xi_e,
            normalization_rho_f=self.config.normalization_rho_f,
            normalization_c_v=self.config.normalization_c_v,
            blur_sigma=2.0,
            ltm_new_center_threshold=self.config.ltm_new_center_threshold,
        ).to(self.device)
        # Shared 16D→64D projection (U) — stored in the ProjectionBundle state
        self.consolidator.stm_to_ltm = self.projections.stm_to_ltm
        self.automatic_consolidator = AutomaticConsolidator(
            self.consolidator, min_interval=100
        )

        # Lock for thread-safety
        self._lock = threading.RLock()

        # Statistics
        self.write_count = 0
        self.read_count = 0
        self.consolidation_count = 0
        self.step_count = 0

        if auto_load:
            self.load()

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #
    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures save."""
        if exc_type is None:
            self.save()
        return False

    def __repr__(self):
        return (
            f"TextMemory(stm_active={self.stm_centers.get_n_active()}, "
            f"ltm_active={self.ltm_centers.get_n_active()}, "
            f"device={self.device})"
        )

    # ------------------------------------------------------------------ #
    # Public API — store
    # ------------------------------------------------------------------ #
    def store(
        self,
        key: str,
        value: str,
        emotion: Union[str, dict, torch.Tensor, None] = None,
        intensity: float = 1.0,
        surprise: float = 0.0,
        age: int = 0,
        memory_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Stores a key-value pair in memory with dynamic write strength.

        Args:
            key: Key text (question, prompt, ...)
            value: Value text (answer, data, ...)
            emotion: Emotion (str, dict, tensor, None)
            intensity: Base intensity multiplier (default 1.0)
            surprise: Surprise level sent from the LLM agent
            age: Original age of the record (e.g. when replaying during cognitive terrain rebuild)
            memory_id: Optional stable ID supplied by an importing adapter
            provenance: Optional local source/session metadata from an adapter
        """
        with self._lock:
            return self._store_impl(
                key, value, emotion, intensity, surprise, age, memory_id, provenance
            )

    def store_record(
        self,
        key: str,
        value: str,
        emotion: Union[str, dict, torch.Tensor, None] = None,
        intensity: float = 1.0,
        surprise: float = 0.0,
        age: int = 0,
        memory_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Stores one record and returns its authoritative center metadata.

        Unlike a store-then-search sequence, winner selection and the returned
        snapshot happen under the same memory lock.
        """
        with self._lock:
            return self._store_impl(
                key,
                value,
                emotion,
                intensity,
                surprise,
                age,
                memory_id,
                provenance,
                return_record=True,
            )

    def _store_impl(
        self,
        key: str,
        value: str,
        emotion: Union[str, dict, torch.Tensor, None] = None,
        intensity: float = 1.0,
        surprise: float = 0.0,
        age: int = 0,
        memory_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
        return_record: bool = False,
    ) -> Union[int, Dict[str, Any]]:
        """Internal store implementation."""
        recorded_at = datetime.now(timezone.utc).isoformat()
        provenance_record = dict(provenance or {})
        provenance_record.setdefault('source_class', 'unknown')
        provenance_record.setdefault('origin', None)
        provenance_record.setdefault('session_id', None)
        provenance_record.setdefault('created_at', recorded_at)
        provenance_record['updated_at'] = recorded_at

        # 1. Emotion
        e_vec = self._sanitize_emotion(emotion)

        # 2. Embed key (=query) and value
        with torch.no_grad():
            emb_key = self.embedder.encode(key).unsqueeze(0).to(self.device)  # [1,384]
            emb_value = self.embedder.encode(value).unsqueeze(0).to(self.device)  # [1,384]

            # 3. Projections
            k_ltm = self.projections.project_to_ltm(emb_key)      # [1,64] norm
            k_stm = self.projections.project_to_stm(emb_key)      # [1,16] norm
            v_val = self.projections.project_to_value(emb_value)  # [1,128]
            k_ctx = self.projections.project_to_context(emb_key)  # [1,16] norm

            # 4. Terrain positions (from keys)
            terr_ltm = self.projections.ltm_to_3d(k_ltm)          # [1,3] in [-1,1]
            terr_stm = self.projections.stm_to_3d(k_stm)          # [1,3]

            # 5. Write strength
            omega = self._compute_write_strength(
                k_ltm, e_vec, surprise=torch.tensor([surprise]), intensity=intensity
            )

        # 6. Novelty → write goes to STM; LTM is filled by consolidation
        write_outcome = self.stm_centers.write(
            keys=k_stm,
            values=v_val,
            emotions=e_vec.unsqueeze(0),
            intensities=omega,
            top_k=self.config.stm_top_k_write,
            new_center_threshold=self.config.stm_new_center_threshold,
            context_keys=k_ctx,
            terrain_positions=terr_stm,
            key_texts=[key],
            value_texts=[value],
            ages=[age],
            memory_ids=[memory_id],
            provenances=[provenance_record],
            return_results=return_record,
        )
        if return_record:
            new_centers, center_results = write_outcome
        else:
            new_centers = write_outcome
            center_results = []

        # 7. Splat into the STM terrain
        omega_val = float(omega[0].item())
        self.stm_terrain.splat(
            terr_stm,
            torch.tensor([omega_val], device=self.device),
            e_vec.unsqueeze(0) if e_vec.dim() == 1 else e_vec,
            eta=self.config.terrain_stm_eta,
            sigma=0.1,
        )

        # 8. Fatigue + automatic consolidation (if tired)
        self.automatic_consolidator.step(
            write_strength=1.0 * intensity,
            stm_centers=self.stm_centers,
            ltm_centers=self.ltm_centers,
            stm_terrain=self.stm_terrain,
            ltm_terrain=self.ltm_terrain,
        )

        # 10. Auto-save
        self.write_count += 1
        if self.config.auto_save and self.write_count % self.config.auto_save_interval == 0:
            self.save()

        if not return_record:
            return new_centers

        center_result = center_results[0] if center_results else {
            'index': None,
            'memory_id': None,
            'created': False,
            'reinforced': False,
            'status': 'not_stored',
        }
        center_index = center_result['index']
        if center_index is None:
            return {
                **center_result,
                'layer': None,
                'key_text': None,
                'value_text': None,
                'provenance': None,
                'new_centers': new_centers,
            }
        return {
            **center_result,
            'layer': 'stm',
            'key_text': self.stm_centers.key_texts[center_index],
            'value_text': self.stm_centers.value_texts[center_index],
            'provenance': deepcopy(self.stm_centers.provenances[center_index]),
            'new_centers': new_centers,
        }

    def _compute_write_strength(
        self,
        k_ltm: torch.Tensor,
        emotions: torch.Tensor,
        surprise: Optional[torch.Tensor] = None,
        intensity: float = 1.0,
    ) -> torch.Tensor:
        """
        ω = intensity * 3.0 * σ(c_n·novelty + c_δ·surprise + c_a·salience + b_ω)

        The configured novelty, surprise, emotion, and intensity terms jointly
        determine the write strength.
        """
        if self.ltm_centers.get_n_active() > 0:
            weights, indices = self.ltm_centers.compute_rbf_weights(
                k_ltm.unsqueeze(1), top_k=1, normalize=False
            )
            max_similarity = weights.squeeze(-1)  # [1,1]
            novelty = 1.0 - max_similarity.squeeze(0)
        else:
            novelty = torch.ones(1, device=k_ltm.device)

        emotion_salience = (emotions - 1.0).abs().max(dim=-1).values  # [1]
        if surprise is None:
            surprise = torch.zeros_like(novelty)

        logit = (
            self.config.write_novelty_weight * novelty
            + self.config.write_surprise_weight * surprise
            + self.config.write_emotion_weight * emotion_salience
            + self.config.write_bias
        )
        omega = self.config.write_strength_base * 3.0 * torch.sigmoid(logit)
        omega = omega * intensity
        return omega

    # ------------------------------------------------------------------ #
    # Public API — recall
    # ------------------------------------------------------------------ #
    def recall(self, query: str, top_k: int = 5, increment_stats: bool = True) -> RecallResult:
        """Recalls text from memory.

        Args:
            query: Query (text)
            top_k: Number of candidates for search

        Returns:
            RecallResult with the recalled information
        """
        with self._lock:
            return self._recall_impl(query, top_k, increment_stats)

    def _recall_impl(self, query: str, top_k: int = 5, increment_stats: bool = True) -> RecallResult:
        """Internal recall implementation."""
        with torch.no_grad():
            emb_query = self.embedder.encode(query).unsqueeze(0).to(self.device)
            k_ltm = self.projections.project_to_ltm(emb_query)   # [1,64] norm
            k_stm = self.projections.project_to_stm(emb_query)   # [1,16] norm
            k_ctx = self.projections.project_to_context(emb_query)  # [1,16] norm

            q_ltm = k_ltm.unsqueeze(1)
            q_stm = k_stm.unsqueeze(1)
            ctx_q = k_ctx.unsqueeze(1)

            # LTM read
            r_V_ltm, r_E_ltm, w_ltm, records_ltm = self.ltm_centers.read_compound_records(
                q_ltm, context_queries=ctx_q, top_k=top_k, increment_stats=increment_stats
            )
            # STM read
            r_V_stm, r_E_stm, w_stm, records_stm = self.stm_centers.read_compound_records(
                q_stm, context_queries=ctx_q, top_k=top_k, increment_stats=increment_stats
            )

        if increment_stats:
            self.read_count += 1

        # Merge both layers, deduplicate by key while keeping the higher weight,
        # and sort descending by weight.
        merged: Dict[str, Dict] = {}
        for record in records_ltm:
            merged[record['key_text']] = {
                'index': record['index'],
                'memory_id': record['memory_id'],
                'key': record['key_text'],
                'value': record['value_text'],
                'weight': record['weight'],
                'source': 'LTM',
                'layer': 'ltm',
                'provenance': record['provenance'],
            }
        for record in records_stm:
            entry = {
                'index': record['index'],
                'memory_id': record['memory_id'],
                'key': record['key_text'],
                'value': record['value_text'],
                'weight': record['weight'],
                'source': 'STM',
                'layer': 'stm',
                'provenance': record['provenance'],
            }
            if record['key_text'] not in merged or entry['weight'] > merged[record['key_text']]['weight']:
                merged[record['key_text']] = entry

        if not merged:
            return RecallResult(
                text='', confidence=0.0, source='EMPTY', matches=[]
            )

        matches = sorted(merged.values(), key=lambda m: m['weight'], reverse=True)
        best = matches[0]
        return RecallResult(
            text=best['value'],
            confidence=best['weight'],
            source=best['source'],
            key_text=best['key'],
            matches=matches,
            memory_id=best['memory_id'],
            provenance=best['provenance'],
            layer=best['layer'],
        )

    # ------------------------------------------------------------------ #
    # Search / edit / forget
    # ------------------------------------------------------------------ #
    def search(self, query: str, top_k: int = 10, source: str = 'both') -> List[Dict]:
        """Searches memories by semantic similarity.

        Args:
            query: Search text
            top_k: Number of results
            source: "ltm", "stm", or "both"

        Returns:
            List of memories sorted by similarity
        """
        with self._lock:
            with torch.no_grad():
                emb = self.embedder.encode(query).unsqueeze(0).to(self.device)
            results = []
            if source in ('ltm', 'both'):
                q = self.projections.project_to_ltm(emb).unsqueeze(1)
                w, idx = self.ltm_centers.compute_rbf_weights(q, top_k=top_k, normalize=True)
                for i in range(w.shape[-1]):
                    ci = int(idx[0, 0, i].item())
                    results.append({
                        'index': ci,
                        'key': self.ltm_centers.key_texts[ci],
                        'value': self.ltm_centers.value_texts[ci],
                        'similarity': float(w[0, 0, i].item()),
                        'source': 'ltm',
                        'memory_id': self.ltm_centers.memory_ids[ci],
                        'provenance': deepcopy(self.ltm_centers.provenances[ci]),
                        'age': int(self.ltm_centers.age[ci].item()),
                        'usage': int(self.ltm_centers.usage[ci].item()),
                        'h': float(self.ltm_centers.h[ci].item()),
                    })
            if source in ('stm', 'both'):
                q = self.projections.project_to_stm(emb).unsqueeze(1)
                w, idx = self.stm_centers.compute_rbf_weights(q, top_k=top_k, normalize=True)
                for i in range(w.shape[-1]):
                    ci = int(idx[0, 0, i].item())
                    results.append({
                        'index': ci,
                        'key': self.stm_centers.key_texts[ci],
                        'value': self.stm_centers.value_texts[ci],
                        'similarity': float(w[0, 0, i].item()),
                        'source': 'stm',
                        'memory_id': self.stm_centers.memory_ids[ci],
                        'provenance': deepcopy(self.stm_centers.provenances[ci]),
                        'age': int(self.stm_centers.age[ci].item()),
                        'usage': int(self.stm_centers.usage[ci].item()),
                        'h': float(self.stm_centers.h[ci].item()),
                    })
            results.sort(key=lambda r: r['similarity'], reverse=True)
            return results[:top_k]

    def edit(
        self,
        old_value: str,
        new_value: str,
        exact_match: bool = True,
        source: str = 'both',
    ) -> int:
        """Edits the value of an existing memory.

        Args:
            old_value: Original value text
            new_value: New value text
            exact_match: True = exact match, False = contains
            source: "ltm", "stm", or "both"

        Returns:
            Number of edited memories
        """
        with self._lock:
            count = 0
            for centers in self._iter_centers(source):
                for i in range(centers.n_centers):
                    if not centers.active[i]:
                        continue
                    vt = centers.value_texts[i]
                    if vt is None:
                        continue
                    match = (vt == old_value) if exact_match else (old_value in vt)
                    if match:
                        centers.value_texts[i] = new_value
                        provenance = dict(centers.provenances[i] or {})
                        provenance['updated_at'] = datetime.now(timezone.utc).isoformat()
                        centers.provenances[i] = provenance
                        count += 1
            return count

    def forget(
        self,
        key_pattern: str,
        exact_match: bool = False,
        source: str = 'both',
    ) -> int:
        """Forgets memories matching the key.

        Args:
            key_pattern: Key text to forget (or a part of it)
            exact_match: True = exact match, False = contains
            source: "ltm", "stm", or "both"

        Returns:
            Number of forgotten memories
        """
        with self._lock:
            count = 0
            for centers in self._iter_centers(source):
                for i in range(centers.n_centers):
                    if not centers.active[i]:
                        continue
                    kt = centers.key_texts[i]
                    if kt is None:
                        continue
                    match = (kt == key_pattern) if exact_match else (key_pattern in kt)
                    if match:
                        centers.scrub_slot(i)
                        count += 1
            return count

    def _iter_centers(self, source: str):
        if source in ('ltm', 'both'):
            yield self.ltm_centers
        if source in ('stm', 'both'):
            yield self.stm_centers

    # ------------------------------------------------------------------ #
    # Homeostasis
    # ------------------------------------------------------------------ #
    def step(self):
        """One homeostasis step.

        Applies decay to all components (LTM, STM, terrains).
        Call periodically (e.g. after each interaction).
        """
        with self._lock:
            self.ltm_centers.homeostasis_step()
            self.stm_centers.homeostasis_step()
            self.ltm_terrain.step()
            self.stm_terrain.step()
            self.step_count += 1

    def maybe_consolidate(self) -> Optional[Dict]:
        """Consolidation if the system is tired.

        Returns:
            Dict with consolidation statistics, or None
        """
        with self._lock:
            if self.consolidator.should_sleep() and self.stm_centers.get_n_active() > 0:
                return self.consolidate()
            return None

    def consolidate(self) -> Dict:
        """Forced STM → LTM consolidation.

        Transfers significant memories from STM to LTM.

        Returns:
            Dict with consolidation statistics
        """
        with self._lock:
            result = self.consolidator.consolidate(
                self.stm_centers, self.ltm_centers, self.stm_terrain, self.ltm_terrain
            )
            self.consolidation_count += 1
            return result

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def save(self, path: Optional[str] = None) -> str:
        """Saves the memory state.

        The format is chosen by the file extension:
          - .bdbm → portable biomem container
          - .pt  → PyTorch legacy format

        Args:
            path: Path to the file (or uses state_file)
        """
        target = path or self.state_file
        state = self._build_state_dict()
        if str(target).endswith('.pt'):
            torch.save(state, target)
        else:
            save_bdbm(state, target)
        return target

    def load(self, path: Optional[str] = None):
        """Loads the memory state.

        Automatically detects the format:
          - .bdbm → biomem container
          - .pt  → legacy PyTorch format

        Args:
            path: Path to the file (or uses state_file)
        """
        with self._lock:
            self._load_impl(path)

    def _load_impl(self, path: Optional[str] = None):
        target = path or self.state_file
        if not target or not os.path.exists(target):
            return
        try:
            if str(target).endswith('.bdbm'):
                state = load_bdbm(target)
            else:
                state = torch.load(target, map_location=self.device, weights_only=False)
                state = self.migrate_state(state, source_path=target)
            self._apply_state(state)
            logger.info('Memory loaded from %s', target)
        except Exception as e:
            logger.error('Failed to load memory from %s: %s', target, e)

    def _build_state_dict(self) -> Dict:
        """Builds the complete state dict for saving."""
        return {
            'version': self.STATE_VERSION,
            'config': self.config,
            'ltm_centers': self.ltm_centers.state_dict_custom(),
            'stm_centers': self.stm_centers.state_dict_custom(),
            'ltm_terrain': self.ltm_terrain.state_dict_custom(),
            'stm_terrain': self.stm_terrain.state_dict_custom(),
            'projections': self.projections.state_dict(),
            'consolidator': {'fatigue': self.consolidator.fatigue.detach().cpu()},
            'stats': {
                'write_count': self.write_count,
                'read_count': self.read_count,
                'consolidation_count': self.consolidation_count,
                'step_count': self.step_count,
            },
        }

    def _apply_state(self, state: Dict) -> None:
        """Applies a loaded state dict to the current instance."""
        from .memory_centers import MemoryCenters
        from .terrain_3d import Terrain3D

        if 'config' in state and isinstance(state['config'], dict):
            self.config = MemoryConfig(**state['config'])
        if 'ltm_centers' in state:
            self.ltm_centers = MemoryCenters.from_state_dict(
                state['ltm_centers'], device=self.device
            )
        if 'stm_centers' in state:
            self.stm_centers = MemoryCenters.from_state_dict(
                state['stm_centers'], device=self.device
            )
        if 'ltm_terrain' in state:
            self.ltm_terrain = Terrain3D.from_state_dict(state['ltm_terrain'], device=self.device)
        if 'stm_terrain' in state:
            self.stm_terrain = Terrain3D.from_state_dict(state['stm_terrain'], device=self.device)
        if 'projections' in state:
            self.projections.load_state_dict(state['projections'])
        if 'consolidator' in state and isinstance(state['consolidator'], dict):
            if 'fatigue' in state['consolidator']:
                self.consolidator.fatigue.copy_(
                    torch.as_tensor(state['consolidator']['fatigue'])
                )
        if 'stats' in state:
            self.write_count = state['stats'].get('write_count', 0)
            self.read_count = state['stats'].get('read_count', 0)
            self.consolidation_count = state['stats'].get('consolidation_count', 0)
            self.step_count = state['stats'].get('step_count', 0)

    def reset(self):
        """Resets memory to the initial state."""
        with self._lock:
            self._reset_impl()

    def _reset_impl(self):
        self.ltm_centers = MemoryCenters(
            n_centers=self.config.n_ltm_centers,
            d_key=self.config.d_ltm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            sigma_read=self.config.ltm_sigma_read,
            sigma_write=self.config.ltm_sigma_write,
            leak=self.config.ltm_leak,
            leak_emotion=self.config.ltm_leak_emotion,
            leak_value=self.config.ltm_leak_value,
            alpha_value=self.config.ltm_alpha_value,
            alpha_emotion=self.config.ltm_alpha_emotion,
            use_hybrid_metric=True,
            minkowski_p=0.5,
            weight_cosine=0.7,
            weight_minkowski=0.3,
            hybrid_candidates=64,
            device=self.device,
        )
        self.stm_centers = MemoryCenters(
            n_centers=self.config.n_stm_centers,
            d_key=self.config.d_stm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            sigma_read=self.config.stm_sigma_read,
            sigma_write=self.config.stm_sigma_write,
            leak=self.config.stm_leak,
            leak_emotion=self.config.stm_leak_emotion,
            leak_value=self.config.stm_leak_value,
            alpha_value=self.config.stm_alpha_value,
            alpha_emotion=self.config.stm_alpha_emotion,
            use_hybrid_metric=True,
            minkowski_p=0.5,
            weight_cosine=0.7,
            weight_minkowski=0.3,
            hybrid_candidates=64,
            device=self.device,
        )
        self.ltm_terrain = Terrain3D(
            resolution=self.config.terrain_resolution,
            n_emotions=self.config.d_emotion,
            alpha_h=self.config.terrain_ltm_alpha_h,
            alpha_e=self.config.terrain_ltm_alpha_e,
            leak=self.config.terrain_ltm_lambda,
            device=self.device,
        )
        self.stm_terrain = Terrain3D(
            resolution=self.config.terrain_resolution,
            n_emotions=self.config.d_emotion,
            alpha_h=self.config.terrain_stm_alpha_h,
            alpha_e=self.config.terrain_stm_alpha_e,
            leak=self.config.terrain_stm_lambda,
            device=self.device,
        )
        self.consolidator = SleepConsolidator(
            d_stm_key=self.config.d_stm_key,
            d_ltm_key=self.config.d_ltm_key,
            d_value=self.config.d_value,
            d_emotion=self.config.d_emotion,
            fatigue_leak=self.config.fatigue_leak,
            fatigue_threshold=self.config.fatigue_threshold,
            consolidation_top_m=self.config.consolidation_top_m,
            consolidation_kappa=self.config.consolidation_kappa,
            consolidation_min_intensity=self.config.consolidation_min_intensity,
            consolidation_xi_h=self.config.consolidation_xi_h,
            consolidation_xi_e=self.config.consolidation_xi_e,
            normalization_rho_f=self.config.normalization_rho_f,
            normalization_c_v=self.config.normalization_c_v,
            blur_sigma=2.0,
            ltm_new_center_threshold=self.config.ltm_new_center_threshold,
        ).to(self.device)
        self.automatic_consolidator = AutomaticConsolidator(
            self.consolidator, min_interval=100
        )
        self.write_count = 0
        self.read_count = 0
        self.consolidation_count = 0
        self.step_count = 0

    # ------------------------------------------------------------------ #
    # Migration
    # ------------------------------------------------------------------ #
    def migrate_state(self, state: Dict, source_path: str = '') -> Dict:
        """Checks the version of the saved state and migrates it if needed.

        Automatically creates a PRE-MIGRATION BACKUP before migration.

        Args:
            state: Loaded dictionary from a .pt file
            source_path: Path to the source file (for backup)

        Returns:
            Upgraded state compatible with the current version.
        """
        # Pre-migration backup (only once)
        if source_path and os.path.exists(source_path) and getattr(self, '_migration_backed_up', False):
            pass
        elif source_path and os.path.exists(source_path):
            try:
                backup_dir = Path(self.state_file).parent / 'backups'
                backup_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                from shutil import copy2
                copy2(source_path, backup_dir / f'pre_migration_{ts}.pt')
                self._migration_backed_up = True
            except Exception:
                pass

        version = state.get('version', '1.0')
        if str(version) != self.STATE_VERSION:
            logger.info('Migrating memory state %s -> %s', version, self.STATE_VERSION)
        state['version'] = self.STATE_VERSION
        return state

    # ------------------------------------------------------------------ #
    # Listing / statistics
    # ------------------------------------------------------------------ #
    def get_stats(self) -> Dict:
        """Returns system statistics."""
        ltm = self.ltm_centers.get_stats()
        stm = self.stm_centers.get_stats()
        return {
            'ltm_active': ltm['n_active'],
            'ltm_total': ltm['n_total'],
            'ltm_texts': sum(1 for t in self.ltm_centers.key_texts[:ltm['n_total']] if t),
            'stm_active': stm['n_active'],
            'stm_total': stm['n_total'],
            'stm_texts': sum(1 for t in self.stm_centers.key_texts[:stm['n_total']] if t),
            'writes': self.write_count,
            'reads': self.read_count,
            'consolidations': self.consolidation_count,
            'steps': self.step_count,
            'fatigue': float(self.consolidator.get_fatigue_level()),
            'device': self.device,
        }

    def list_memories(self, source: str = 'both', limit: int = 100) -> List[Dict]:
        """Lists all stored memories.

        Args:
            source: "ltm", "stm", or "both"
            limit: Maximum number of items

        Returns:
            List of dictionaries with memory information
        """
        with self._lock:
            records = []
            for layer, centers in zip(('ltm', 'stm'), (self.ltm_centers, self.stm_centers)):
                if source not in ('both', layer):
                    continue
                for i in range(centers.n_centers):
                    if not centers.active[i]:
                        continue
                    records.append({
                        'index': i,
                        'layer': layer,
                        'key_text': centers.key_texts[i],
                        'value_text': centers.value_texts[i],
                        'memory_id': centers.memory_ids[i],
                        'provenance': deepcopy(centers.provenances[i]),
                        'intensity': float(centers.h[i].item()),
                        'usage': int(centers.usage[i].item()),
                        'age': int(centers.age[i].item()),
                        'trusted': bool(centers.trust_flags[i]) if hasattr(centers, 'trust_flags') else False,
                    })
            return records[:limit]

    # ------------------------------------------------------------------ #
    # Backup / restore
    # ------------------------------------------------------------------ #
    def backup(self, path: Optional[str] = None) -> str:
        """Creates a backup of the memory state.

        Args:
            path: Path for the backup (default: memory_backup_<timestamp>.bdbm)

        Returns:
            Path to the created backup.
        """
        target = path or self._default_backup_path()
        self.save(target)
        return target

    def _default_backup_path(self) -> str:
        backup_dir = Path(self.state_file).parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        return str(backup_dir / f'memory_backup_{ts}.bdbm')

    def restore(self, path: str) -> None:
        """Restores the memory state from a backup.

        Creates a safety backup of the current state before restoring.

        Args:
            path: Path to the backup.
        """
        with self._lock:
            try:
                self.backup()  # Safety backup of the current state
            except Exception:
                pass
            self._load_impl(path)

    def list_backups(self, directory: Optional[str] = None) -> List[Dict]:
        """Lists available backups.

        Args:
            directory: Directory with backups (default: backups/ next to state_file)

        Returns:
            List of backups with metadata (path, date, size_bytes).
        """
        backup_dir = directory or str(Path(self.state_file).parent / 'backups')
        if not os.path.isdir(backup_dir):
            return []
        backups = []
        for name in sorted(os.listdir(backup_dir)):
            if not (name.endswith('.bdbm') or name.endswith('.pt')):
                continue
            fp = os.path.join(backup_dir, name)
            try:
                st = os.stat(fp)
                backups.append({
                    'path': fp,
                    'date': datetime.fromtimestamp(st.st_mtime).isoformat(),
                    'size_bytes': st.st_size,
                })
            except OSError:
                continue
        backups.sort(key=lambda b: b['date'], reverse=True)
        return backups

    # ------------------------------------------------------------------ #
    # Refactor (cognitive terrain rebuild)
    # ------------------------------------------------------------------ #
    def refactor(self, progress_callback: Optional[Callable] = None) -> Dict:
        """
        Performs a complete rebuild of the cognitive terrain.

        All records from STM and LTM are replayed from the oldest
        to the newest, regenerating both 3D terrains. STM→LTM
        consolidation runs through the standard fatigue mechanism
        (not forced).

        Args:
            progress_callback: Optional function (step: str, current: int,
                               total: int, detail: str) for reporting
                               progress to the GUI.

        Returns:
            Dict with the results of the cognitive terrain rebuild.
        """
        with self._lock:
            # State snapshot
            ltm_records, stm_records = [], []
            for i in range(self.ltm_centers.n_centers):
                if not self.ltm_centers.active[i]:
                    continue
                ltm_records.append((self.ltm_centers, i))
            for i in range(self.stm_centers.n_centers):
                if not self.stm_centers.active[i]:
                    continue
                stm_records.append((self.stm_centers, i))

            total = len(ltm_records) + len(stm_records)
            # Reset terrains (not centers)
            self.ltm_terrain.reset()
            self.stm_terrain.reset()

            current = 0
            # Replay from the oldest to the newest
            for layer, records in (('ltm', ltm_records), ('stm', stm_records)):
                for centers, i in records:
                    if progress_callback:
                        progress_callback('replay', current, total, centers.key_texts[i] or '')
                    # Splat into the corresponding terrain
                    terr = self.ltm_terrain if layer == 'ltm' else self.stm_terrain
                    terr.splat(
                        centers.K_terrain[i].unsqueeze(0),
                        float(centers.h[i].item()),
                        centers.e[i],
                    )
                    current += 1

            if progress_callback:
                progress_callback('done', current, total, '')

            return {
                'traces_replayed': total,
                'ltm_traces': len(ltm_records),
                'stm_traces': len(stm_records),
            }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _sanitize_emotion(self, emotion: Union[str, dict, torch.Tensor, None]) -> torch.Tensor:
        """Normalizes the emotion input to a 4D tensor [dopamine, serotonin, cortisol, oxytocin]."""
        if emotion is None:
            return torch.ones(self.config.d_emotion, device=self.device)
        if isinstance(emotion, str):
            return EmotionExtractor.from_name(emotion).to(self.device)
        if isinstance(emotion, dict):
            return EmotionExtractor.from_dict(emotion).to(self.device)
        if isinstance(emotion, torch.Tensor):
            t = emotion.float().to(self.device).squeeze()
            if t.numel() == 1:
                return torch.ones(self.config.d_emotion, device=self.device) * t
            return t[: self.config.d_emotion]
        return torch.ones(self.config.d_emotion, device=self.device)
