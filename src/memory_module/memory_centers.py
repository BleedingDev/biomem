# memory_module/memory_centers.py
"""
Memory centers for LTM (64D) and STM (16D) with text storage support.

Each center contains:
- K: key (64D or 16D)
- V: value (d_v)
- h: intensity/GS
- e: emotion vector (4D)
- key_text: key text (for text in/out)
- value_text: value text (for text in/out)
- usage: usage counter
- age: center age
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import NAMESPACE_URL, uuid4, uuid5


class MemoryCenters(nn.Module):
    """
    Set of memory centers with RBF kernel operations and a text store.

    Supports:
    - RBF read (weighted sum based on distance)
    - RBF write (local update)
    - Homeostasis (decay)
    - Merge/prune for capacity management
    - TEXT IN/OUT: storing and recalling texts
    """

    # Minimum intensity h is floored to during normalization
    NORMALIZATION_MIN_INTENSITY = 0.01
    RECORD_METADATA_VERSION = 1
    MAX_PROVENANCE_HISTORY = 32

    def __init__(
        self,
        n_centers: int,
        d_key: int,
        d_value: int,
        d_emotion: int = 4,
        sigma_read: float = 0.5,
        sigma_write: float = 0.4,
        leak: float = 1e-4,
        leak_emotion: float = 1e-4,
        leak_value: float = 1e-4,
        alpha_value: float = 0.03,
        alpha_emotion: float = 0.01,
        alpha_key: float = 0.0,
        use_hybrid_metric: bool = True,
        minkowski_p: float = 0.5,
        weight_cosine: float = 0.7,
        weight_minkowski: float = 0.3,
        hybrid_candidates: int = 64,
        device: str = "cpu",
    ):
        super().__init__()
        self.n_centers = n_centers
        self.d_key = d_key
        self.d_value = d_value
        self.d_emotion = d_emotion
        self.sigma_read = sigma_read
        self.sigma_write = sigma_write
        self.leak = leak
        self.leak_emotion = leak_emotion
        self.leak_value = leak_value
        self.alpha_value = alpha_value
        self.alpha_emotion = alpha_emotion
        self.alpha_key = alpha_key

        # Hybrid metric for reading
        self.use_hybrid_metric = use_hybrid_metric
        self.minkowski_p = minkowski_p
        self.weight_cosine = weight_cosine
        self.weight_minkowski = weight_minkowski
        self.hybrid_candidates = hybrid_candidates

        # Context / terrain components of compound keys
        self.d_context = 16
        self.d_terrain = 3
        self.weight_context = 0.25
        # Compound score weights sum to 1.0.
        self.weight_semantic = 0.6
        self.weight_terrain = 0.15

        # Memory centers as buffers (not parameters — we do not train them)
        # Keys — normalized to unit norm
        self.register_buffer("K", F.normalize(torch.randn(n_centers, d_key, device=device), dim=-1))
        # Values
        self.register_buffer("V", torch.zeros(n_centers, d_value, device=device))
        # Intensity (GS)
        self.register_buffer("h", torch.zeros(n_centers, device=device))
        # Emotions (neutral value = 1.0, like PlantNet hormones)
        self.register_buffer("e", torch.ones(n_centers, d_emotion, device=device))
        # Usage counter (used for pruning)
        self.register_buffer("usage", torch.zeros(n_centers, dtype=torch.long, device=device))
        # Age (steps since creation)
        self.register_buffer("age", torch.zeros(n_centers, dtype=torch.long, device=device))
        # Active mask (for dynamic allocation)
        self.register_buffer("active", torch.zeros(n_centers, dtype=torch.bool, device=device))
        # Center context keys (for compound reads) [n_centers, d_context]
        self.register_buffer("K_context", torch.zeros(n_centers, self.d_context, device=device))
        # Center terrain positions [n_centers, 3] = grid [x, y, h]
        self.register_buffer("K_terrain", torch.zeros(n_centers, self.d_terrain, device=device))

        # Text representations (Python lists, not buffers)
        self.key_texts: List[Optional[str]] = [None] * n_centers
        self.value_texts: List[Optional[str]] = [None] * n_centers
        # Stable record identity and local-only provenance summaries. These
        # lists deliberately mirror the fixed center capacity, just like text.
        self.memory_ids: List[Optional[str]] = [None] * n_centers
        self.provenances: List[Optional[Dict[str, Any]]] = [None] * n_centers

        self.total_step = 0

    def compute_rbf_weights(
        self,
        queries: torch.Tensor,
        top_k: int = 32,
        normalize: bool = True,
        sigma: Optional[float] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Computes RBF kernel weights for queries against the centers.

        Args:
            queries: [B, T, d_key] normalized queries
            top_k: number of nearest centers
            normalize: apply softmax normalization

        Returns:
            weights: [B, T, top_k] RBF weights
            indices: [B, T, top_k] indices of the selected centers
        """
        B, T, D = queries.shape

        # Only active centers
        active_indices = torch.where(self.active)[0]
        n_active = active_indices.shape[0]

        if n_active == 0:
            # No active centers
            return (
                torch.zeros(B, T, 0, device=queries.device),
                torch.zeros(B, T, 0, dtype=torch.long, device=queries.device),
            )

        sigma_val = self.sigma_read if sigma is None else sigma

        K_active = self.K[active_indices]  # [n_active, d_key]
        h_active = self.h[active_indices]  # [n_active]

        # Cosine similarity (for normalized vectors = dot product)
        cos_similarities = torch.clamp(torch.matmul(queries, K_active.T), -1.0, 1.0)  # [B, T, n_active]

        # Convert to distance: ||q - k||^2 = 2 - 2*cos (for normalized vectors)
        distances_sq = 2.0 - 2.0 * cos_similarities

        # RBF uses cosine distance for normalized keys. The normalized read
        # weights combine the kernel with center intensity in log space:
        # softmax(log(exp(-d² / 2σ²)) + log(h)).
        rbf_weights = torch.exp(-distances_sq / (2.0 * sigma_val ** 2))

        if self.use_hybrid_metric and n_active > self.hybrid_candidates:
            # Hybrid metric: first select hybrid_candidates candidates via a
            # combined score (cosine + Minkowski), then perform the final selection
            # with cosine RBF. When n_active <= hybrid_candidates, every center
            # is already a candidate and this branch is unnecessary.
            n_candidates = self.hybrid_candidates

            # 1) Candidates by purely cosine RBF weight
            _, candidate_local_indices = torch.topk(rbf_weights, n_candidates, dim=-1)

            # 2) Minkowski distance on the candidates (manual computation, p=minkowski_p)
            candidate_keys = K_active[candidate_local_indices]  # [B, T, n_candidates, d_key]
            queries_expanded = queries.unsqueeze(2)             # [B, T, 1, d_key]
            diff = queries_expanded - candidate_keys            # [B, T, nc, d_key]
            p = self.minkowski_p
            dist_minkowski = diff.abs().pow(p).sum(dim=-1).pow(1.0 / p)  # [B, T, nc]
            dist_max = dist_minkowski.max()
            dist_normalized = dist_minkowski / (dist_max + 1e-8)

            # 3) Hybrid combined score (cosine + minkowski)
            minkowski_score = 1.0 - dist_normalized
            cos_candidates = cos_similarities.gather(-1, candidate_local_indices)
            combined_score = (
                self.weight_cosine * ((1.0 - cos_candidates) / 2.0)
                + self.weight_minkowski * (1.0 - minkowski_score)
            )

            # 4) Overhead: select the candidates with the lowest combined score and
            #    keep only them for the final step (topk_combined / candidate_keys)
            n_final = min(self.hybrid_candidates, n_active)
            effective_k = min(top_k, n_final)
            _, topk_in_candidates = torch.topk(combined_score, effective_k, dim=-1, largest=False)
            # Convert: index within candidates → index among active → global
            topk_local_indices = torch.gather(
                candidate_local_indices, -1, topk_in_candidates
            )
            topk_indices = active_indices[topk_local_indices]
            # Final weights = cosine RBF on the selected indices
            topk_weights = torch.exp(
                -distances_sq.gather(-1, topk_local_indices) / (2.0 * sigma_val ** 2)
            )
        else:
            # Standard cosine RBF over all active centers
            effective_k = min(top_k, n_active)
            topk_weights, topk_local_indices = torch.topk(rbf_weights, effective_k, dim=-1)
            topk_indices = active_indices[topk_local_indices]

        if normalize:
            # Mix intensity into the weights (log-space for stability)
            h_topk = self.h[topk_indices]  # [B, T, k]
            log_weights = torch.log(topk_weights + 1e-8) + torch.log(h_topk + 1e-8)
            weights = F.softmax(log_weights, dim=-1)
        else:
            weights = topk_weights

        return weights, topk_indices

    def read(
        self,
        queries: torch.Tensor,
        top_k: int = 32,
        increment_stats: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Reads from memory using the RBF kernel.

        Args:
            queries: [B, T, d_key] normalized queries
            top_k: number of centers to read from

        Returns:
            r_V: [B, T, d_value] read values
            r_E: [B, T, d_emotion] read emotions
            weights: [B, T, top_k] weights
            indices: [B, T, top_k] center indices
        """
        B, T, _ = queries.shape

        weights, indices = self.compute_rbf_weights(queries, top_k, normalize=True)

        if weights.shape[-1] == 0:
            # No active centers
            return (
                torch.zeros(B, T, self.d_value, device=queries.device),
                torch.zeros(B, T, self.d_emotion, device=queries.device),
                weights,
                indices,
            )

        # Gather values and emotions for the selected centers
        V_selected = self.V[indices]  # [B, T, k, d_value]
        e_selected = self.e[indices]  # [B, T, k, d_emotion]

        # Weighted sum
        r_V = torch.einsum('btk,btkv->btv', weights, V_selected)
        r_E = torch.einsum('btk,btke->bte', weights, e_selected)

        # Update usage counter
        if increment_stats:
            unique_indices = indices.unique()
            self.usage[unique_indices] += 1

        return r_V, r_E, weights, indices

    def read_with_text(
        self,
        queries: torch.Tensor,
        top_k: int = 5,
        increment_stats: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, List[Tuple[str, str, float]]]:
        """
        Reads from memory and also returns the texts.

        Args:
            queries: [1, 1, d_key] normalized query
            top_k: number of best results

        Returns:
            r_V: [1, 1, d_value] read value
            r_E: [1, 1, d_emotion] read emotion
            weights: [1, 1, top_k] weights
            text_results: List[(key_text, value_text, weight)]
        """
        r_V, r_E, weights, indices = self.read(queries, top_k=top_k, increment_stats=increment_stats)

        text_results: List[Tuple[str, str, float]] = []
        weights_flat = weights.flatten()
        indices_flat = indices.flatten()
        for w, idx in zip(weights_flat, indices_flat):
            key_text = self.key_texts[idx.item()]
            value_text = self.value_texts[idx.item()]
            text_results.append((key_text, value_text, float(w.item())))

        return r_V, r_E, weights, text_results

    def read_compound(
        self,
        queries: torch.Tensor,
        context_queries: torch.Tensor = None,
        terrain_queries: torch.Tensor = None,
        top_k: int = 32,
        increment_stats: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Reads from memory using compound keys.
        """
        B, T, _ = queries.shape

        # Compound score = weighted cosine similarities (0.6/0.25/0.15)
        # with parts: semantic key, context (16D), terrain position (3D).
        # Only ACTIVE centers are candidates (as in compute_rbf_weights).
        active_indices = torch.where(self.active)[0]
        n_active = active_indices.shape[0]
        if n_active == 0:
            return (
                torch.zeros(B, T, self.d_value, device=queries.device),
                torch.zeros(B, T, self.d_emotion, device=queries.device),
                torch.zeros(B, T, 0, device=queries.device),
                torch.zeros(B, T, 0, dtype=torch.long, device=queries.device),
            )

        K_active = self.K[active_indices]
        K_ctx_active = self.K_context[active_indices]
        K_terr_active = self.K_terrain[active_indices]

        def _cos_part(q_part, keys):
            if q_part is None:
                q_part = torch.zeros(B, T, keys.shape[-1], device=queries.device)
            # [B,T,1,d] · [n,d] -> [B,T,n]
            return torch.clamp(
                (q_part.unsqueeze(2) * keys.unsqueeze(0).unsqueeze(0)).sum(-1),
                -1.0, 1.0,
            )

        semantic_part = _cos_part(queries, K_active)
        context_part = _cos_part(
            None if context_queries is None else context_queries,
            K_ctx_active,
        )
        terrain_part = _cos_part(
            None if terrain_queries is None else terrain_queries,
            K_terr_active,
        )

        combined_score = (
            self.weight_semantic * semantic_part
            + self.weight_context * context_part
            + self.weight_terrain * terrain_part
        )  # [B, T, n_active] v rozsahu [-1, 1]

        # RBF over the distance (2 - 2·score), using sigma_read.
        distances_sq = 2.0 - 2.0 * combined_score
        combined_weights = torch.exp(-distances_sq / (2.0 * self.sigma_read ** 2))

        effective_k = min(top_k, n_active)
        topk_combined, topk_in_candidates = torch.topk(combined_weights, effective_k, dim=-1)
        # Convert: index among active → global index
        final_indices = active_indices[topk_in_candidates]

        # Normalization with intensity (log-space, as in compute_rbf_weights)
        h_topk = self.h[final_indices]  # [B, T, k]
        log_weights = torch.log(topk_combined + 1e-8) + torch.log(h_topk + 1e-8)
        final_weights = F.softmax(log_weights, dim=-1)

        weights, indices = final_weights, final_indices

        # Gather values and emotions for the selected centers
        V_selected = self.V[indices]  # [B, T, k, d_value]
        e_selected = self.e[indices]  # [B, T, k, d_emotion]

        # Weighted sum
        r_V = torch.einsum('btk,btkv->btv', weights, V_selected)
        r_E = torch.einsum('btk,btke->bte', weights, e_selected)

        if increment_stats:
            unique_indices = indices.unique()
            self.usage[unique_indices] += 1

        return r_V, r_E, weights, indices

    def read_compound_with_text(
        self,
        queries: torch.Tensor,
        context_queries: torch.Tensor = None,
        terrain_queries: torch.Tensor = None,
        top_k: int = 5,
        increment_stats: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, List[Tuple[str, str, float]]]:
        """
        Reads from memory using compound keys and also returns the texts.
        """
        r_V, r_E, weights, records = self.read_compound_records(
            queries,
            context_queries=context_queries,
            terrain_queries=terrain_queries,
            top_k=top_k,
            increment_stats=increment_stats,
        )

        text_results = [
            (record["key_text"], record["value_text"], record["weight"])
            for record in records
        ]

        return r_V, r_E, weights, text_results

    def read_compound_records(
        self,
        queries: torch.Tensor,
        context_queries: torch.Tensor = None,
        terrain_queries: torch.Tensor = None,
        top_k: int = 5,
        increment_stats: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, List[Dict[str, Any]]]:
        """Reads compound matches with stable identity and provenance."""
        r_V, r_E, weights, indices = self.read_compound(
            queries,
            context_queries=context_queries,
            terrain_queries=terrain_queries,
            top_k=top_k,
            increment_stats=increment_stats,
        )

        records: List[Dict[str, Any]] = []
        for weight, index in zip(weights.flatten(), indices.flatten()):
            center_index = int(index.item())
            records.append({
                "index": center_index,
                "memory_id": self.memory_ids[center_index],
                "key_text": self.key_texts[center_index],
                "value_text": self.value_texts[center_index],
                "weight": float(weight.item()),
                "provenance": deepcopy(self.provenances[center_index]),
            })
        return r_V, r_E, weights, records

    def write(
        self,
        keys: torch.Tensor,
        values: torch.Tensor,
        emotions: torch.Tensor,
        intensities: torch.Tensor,
        top_k: int = 16,
        new_center_threshold: float = 0.3,
        context_keys: torch.Tensor = None,
        terrain_positions: torch.Tensor = None,
        key_texts: List[str] = None,
        value_texts: List[str] = None,
        ages: Optional[Union[torch.Tensor, List[int]]] = None,
        memory_ids: Optional[List[Optional[str]]] = None,
        provenances: Optional[List[Optional[Dict[str, Any]]]] = None,
        return_results: bool = False,
    ) -> Union[int, Tuple[int, List[Dict[str, Any]]]]:
        """
        Writes to memory with text support.

        Args:
            keys: [N, d_key] segment keys
            values: [N, d_value] values
            emotions: [N, d_emotion] emotions
            intensities: [N] write strength
            key_texts: List of key texts
            value_texts: List of value texts
            ages: Optional ages [N] (carried over during consolidation)
            memory_ids: Optional stable record IDs [N]
            provenances: Optional local provenance summaries [N]

        Returns:
            Number of newly created centers
        """
        N = keys.shape[0]
        new_centers_created = 0
        write_results: List[Dict[str, Any]] = []

        for i in range(N):
            q = keys[i:i + 1]  # [1, d_key]
            v = values[i]      # [d_value]
            eps = emotions[i]  # [d_emotion]
            omega = intensities[i].item()

            if omega < 1e-6:
                write_results.append({
                    "index": None,
                    "memory_id": None,
                    "created": False,
                    "reinforced": False,
                    "status": "ignored_zero_intensity",
                })
                continue

            ctx = context_keys[i] if context_keys is not None else None
            terr = terrain_positions[i] if terrain_positions is not None else None
            key_txt = key_texts[i] if key_texts is not None else None
            val_txt = value_texts[i] if value_texts is not None else None
            age_val = ages[i] if ages is not None else None
            memory_id = memory_ids[i] if memory_ids is not None else None
            provenance = provenances[i] if provenances is not None else None

            matching_id_index = None
            if memory_id is not None:
                for active_index in torch.where(self.active)[0].tolist():
                    if self.memory_ids[active_index] == memory_id:
                        matching_id_index = active_index
                        break

            # Find the nearest centers
            weights, indices = self.compute_rbf_weights(
                q.unsqueeze(0),  # [1, 1, d_key]
                top_k=top_k,
                normalize=False,
            )
            weights = weights.squeeze(0).squeeze(0)  # [k]
            indices = indices.squeeze(0).squeeze(0)  # [k]

            if matching_id_index is not None:
                stored_key = self.key_texts[matching_id_index]
                nearest_index = int(indices[torch.argmax(weights)].item()) if weights.numel() else None
                same_logical_record = (
                    (key_txt is not None and stored_key == key_txt)
                    or (key_txt is None and nearest_index == matching_id_index)
                )
                if not same_logical_record:
                    write_results.append({
                        "index": None,
                        "memory_id": None,
                        "created": False,
                        "reinforced": False,
                        "status": "duplicate_memory_id",
                    })
                    continue
                indices = torch.tensor(
                    [matching_id_index], dtype=torch.long, device=keys.device
                )
                weights = torch.ones(1, dtype=keys.dtype, device=keys.device)

            # The maximum RBF weight decides novelty —
            # the local max_weight is recorded in the binary; an alternative
            # (sum threshold weights.sum() < new_center_threshold) would never
            # create a new center in a dense memory, so the max threshold is used.
            max_weight = weights.max() if weights.shape[0] > 0 else 0.0
            if matching_id_index is None and (
                weights.shape[0] == 0 or max_weight < new_center_threshold
            ):
                # New region - create a new center
                new_idx = self._create_new_center(
                    q.squeeze(0), v, eps, omega,
                    context_key=ctx,
                    terrain_pos=terr,
                    key_text=key_txt,
                    value_text=val_txt,
                    age=int(age_val) if age_val is not None else 0,
                    memory_id=memory_id,
                    provenance=provenance,
                )
                if new_idx >= 0:
                    new_centers_created += 1
                    write_results.append({
                        "index": new_idx,
                        "memory_id": self.memory_ids[new_idx],
                        "created": True,
                        "reinforced": False,
                        "status": "created",
                    })
                else:
                    write_results.append({
                        "index": None,
                        "memory_id": None,
                        "created": False,
                        "reinforced": False,
                        "status": "capacity_exhausted",
                    })
                continue

            # Normalize weights locally
            weights_normalized = weights / (weights.sum() + 1e-8)
            metadata_idx = int(indices[torch.argmax(weights)].item())

            # Update existing centers
            for j, idx in enumerate(indices):
                w = weights_normalized[j].item() * omega

                # Update intensity
                h_old = self.h[idx].item()
                self.h[idx] = h_old + w

                # Update values (exponential moving average)
                self.V[idx] += self.alpha_value * w * (v - self.V[idx])

                # Update emotions
                self.e[idx] += self.alpha_emotion * w * (eps - self.e[idx])

                # Update keys (very cautiously or not at all)
                if self.alpha_key > 0:
                    self.K[idx] = F.normalize(
                        self.K[idx] + self.alpha_key * w * (q.squeeze(0) - self.K[idx]),
                        dim=-1,
                    )

            # Numeric learning can update top-k candidates, but canonical
            # record metadata belongs only to the closest (winning) center.
            if ctx is not None:
                self.K_context[metadata_idx] = ctx
            if terr is not None:
                self.K_terrain[metadata_idx] = terr
            if key_txt is not None:
                self.key_texts[metadata_idx] = key_txt
            if val_txt is not None:
                self.value_texts[metadata_idx] = val_txt
            if age_val is not None:
                self.age[metadata_idx] += int(age_val)
            if self.memory_ids[metadata_idx] is None:
                self.memory_ids[metadata_idx] = memory_id or uuid4().hex
            self.provenances[metadata_idx] = self._merge_provenance(
                self.provenances[metadata_idx], provenance
            )
            write_results.append({
                "index": metadata_idx,
                "memory_id": self.memory_ids[metadata_idx],
                "created": False,
                "reinforced": True,
                "status": "reinforced",
            })

        if return_results:
            return new_centers_created, write_results
        return new_centers_created

    def _create_new_center(
        self,
        key: torch.Tensor,
        value: torch.Tensor,
        emotion: torch.Tensor,
        intensity: float,
        context_key: torch.Tensor = None,
        terrain_pos: torch.Tensor = None,
        key_text: str = None,
        value_text: str = None,
        age: int = 0,
        memory_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Creates a new center with text.

        Returns:
            Index of the new center, or -1 if there is no room
        """
        # Find the first inactive slot
        inactive_indices = torch.where(~self.active)[0]

        if inactive_indices.shape[0] == 0:
            # No room - prune or merge needed
            return -1

        idx = inactive_indices[0].item()

        self.K[idx] = F.normalize(key, dim=-1)
        self.V[idx] = value
        self.e[idx] = emotion
        self.h[idx] = intensity
        self.usage[idx] = 0
        self.age[idx] = age
        self.active[idx] = True

        if context_key is not None:
            self.K_context[idx] = context_key
        if terrain_pos is not None:
            self.K_terrain[idx] = terrain_pos
        if key_text is not None:
            self.key_texts[idx] = key_text
        if value_text is not None:
            self.value_texts[idx] = value_text
        self.memory_ids[idx] = memory_id or uuid4().hex
        self.provenances[idx] = self._merge_provenance(None, provenance)

        return idx

    def scrub_slot(self, index: int) -> None:
        """Securely clears one slot for an explicit user-requested forget."""
        index = int(index)
        if index < 0 or index >= self.n_centers:
            raise IndexError(f"center index out of range: {index}")
        self.K[index].zero_()
        self.K_context[index].zero_()
        self.K_terrain[index].zero_()
        self.V[index].zero_()
        self.h[index].zero_()
        self.e[index].zero_()
        self.usage[index].zero_()
        self.age[index].zero_()
        self.active[index] = False
        self.key_texts[index] = None
        self.value_texts[index] = None
        self.memory_ids[index] = None
        self.provenances[index] = None

    @staticmethod
    def _merge_provenance(
        existing: Optional[Dict[str, Any]],
        incoming: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Merges provenance while retaining 32 recent distinct source events.

        The first known top-level source fields remain canonical. History is a
        bounded recency ledger: repeating an event moves it to the end rather
        than adding a duplicate.
        """
        if incoming is None:
            return deepcopy(existing)

        incoming = deepcopy(incoming)
        if existing is None:
            merged = deepcopy(incoming)
        else:
            merged = deepcopy(existing)
            for field in ("source_class", "origin", "session_id", "created_at"):
                current = merged.get(field)
                candidate = incoming.get(field)
                if current in (None, "", "unknown") and candidate not in (None, ""):
                    merged[field] = candidate
            if incoming.get("updated_at") is not None:
                merged["updated_at"] = incoming["updated_at"]

        event = {
            field: incoming.get(field)
            for field in ("source_class", "origin", "session_id")
            if incoming.get(field) not in (None, "")
        }
        history: List[Dict[str, Any]] = []
        candidates = list(merged.get("source_history", []))
        candidates.extend(incoming.get("source_history", []))
        if event:
            candidates.append(event)
        for item in candidates:
            item = deepcopy(dict(item))
            if item in history:
                history.remove(item)
            history.append(item)
        history = history[-MemoryCenters.MAX_PROVENANCE_HISTORY:]
        if history:
            merged["source_history"] = history
        else:
            merged.pop("source_history", None)
        return merged

    def merge_similar(self, threshold: float = 0.95) -> int:
        """
        Merges similar centers.

        Returns:
            Number of merged pairs
        """
        active_indices = torch.where(self.active)[0]
        n_active = active_indices.shape[0]

        if n_active < 2:
            return 0

        merged = 0

        # Similarity matrix
        K_active = self.K[active_indices]
        sim = torch.matmul(K_active, K_active.T)

        # Find pairs above the threshold (excluding the diagonal)
        sim.fill_diagonal_(0.0)

        while True:
            max_sim, flat_idx = sim.max(), sim.argmax()
            if max_sim < threshold:
                break

            i = flat_idx // n_active
            j = flat_idx % n_active
            idx_i = active_indices[i]
            idx_j = active_indices[j]

            # Merge j into i
            h_i, h_j = self.h[idx_i], self.h[idx_j]
            total_h = h_i + h_j + 1e-8

            self.h[idx_i] = total_h
            self.K[idx_i] = F.normalize(
                (h_i * self.K[idx_i] + h_j * self.K[idx_j]) / total_h,
                dim=-1,
            )
            self.V[idx_i] = (h_i * self.V[idx_i] + h_j * self.V[idx_j]) / total_h
            self.e[idx_i] = (h_i * self.e[idx_i] + h_j * self.e[idx_j]) / total_h

            # Prefer the newer center's context and terrain while filling any
            # missing text fields from that same center.
            self.K_context[idx_i] = self.K_context[idx_j]
            self.K_terrain[idx_i] = self.K_terrain[idx_j]
            if self.key_texts[idx_i] is None:
                self.key_texts[idx_i] = self.key_texts[idx_j]
            if self.value_texts[idx_i] is None:
                self.value_texts[idx_i] = self.value_texts[idx_j]
            if self.memory_ids[idx_i] is None:
                self.memory_ids[idx_i] = self.memory_ids[idx_j]
            self.provenances[idx_i] = self._merge_provenance(
                self.provenances[idx_i], self.provenances[idx_j]
            )

            # Deactivate j
            self.active[idx_j] = False

            # Update the similarity matrix (remove j)
            sim[j, :] = 0.0
            sim[:, j] = 0.0
            sim[i, :] = 0.0  # Stop merging i in this iteration
            sim[:, i] = 0.0

            merged += 1

        return merged

    def prune_weak(
        self,
        intensity_threshold: float = 0.001,
        min_age: int = 1000,
    ) -> int:
        """
        Removes weak and old centers.

        Self-correcting threshold: always at least NORMALIZATION_MIN_INTENSITY * 1.1,
        so that centers floored by normalization are actually prunable.
        Trusted centers are always protected, even if they meet the other criteria.

        Returns:
            Number of removed centers
        """
        active_indices = torch.where(self.active)[0]

        # Self-correcting intensity threshold
        effective_threshold = max(intensity_threshold, self.NORMALIZATION_MIN_INTENSITY * 1.1)

        # Prune criteria
        weak = self.h[active_indices] < effective_threshold
        old = self.age[active_indices] > min_age
        unused = self.usage[active_indices] < 5

        # Trusted centers are always protected (high usage = frequently read)
        # — the trust boundary is not directly derivable
        # from the binary; here usage >= 5 (symmetric to `unused`).
        # Alternatives considered: h >= 10*effective_threshold,
        # or the combination usage >= 5 | h >= 0.5.
        trusted_tensor = torch.tensor(
            [self.usage[i] >= 5 for i in active_indices.tolist()],
            dtype=torch.bool,
            device=self.h.device,
        )
        untrusted = ~trusted_tensor

        to_prune = active_indices[weak & old & unused & untrusted]

        for idx in to_prune:
            self.active[idx] = False

        return to_prune.shape[0]

    def apply_normalization(
        self,
        c_v: float = 2.0,
        intensity_decay: float = 0.95,
    ):
        """
        Applies normalization after consolidation.

        Spec §8.3:
            h_i^s ← log(1 + h_i^s)          # logarithmic intensity compression
            V_i^s ← V_i^s / (1 + ||V_i^s||/c_V)
            e_i^s ← tanh(e_i^s)
        """
        active_mask = self.active

        # Logarithmic intensity compression
        self.h[active_mask] = torch.log1p(self.h[active_mask])

        # Value saturation
        V_norm = self.V[active_mask].norm(dim=-1, keepdim=True)
        self.V[active_mask] = self.V[active_mask] / (1.0 + V_norm / c_v)

        # Tanh on emotions
        self.e[active_mask] = torch.tanh(self.e[active_mask])

        # Floor intensity at NORMALIZATION_MIN_INTENSITY so that even very weak
        # centers remain representable (and prunable — see prune_weak)
        min_intensity = self.NORMALIZATION_MIN_INTENSITY
        weak_mask = self.h[active_mask] < min_intensity
        self.h[active_mask] = torch.where(
            weak_mask,
            torch.ones_like(self.h[active_mask]) * min_intensity,
            self.h[active_mask],
        )
        # intensity_decay is retained in serialized state; normalization does
        # not apply it.

    def get_n_active(self) -> int:
        """Returns the number of active centers."""
        return self.active.sum().item()

    def get_stats(self) -> Dict:
        """Returns statistics."""
        active_mask = self.active
        n_texts = sum(1 for t in self.key_texts if t is not None)
        return {
            "n_active": self.get_n_active(),
            "n_total": self.n_centers,
            "h_mean": self.h[active_mask].mean().item() if active_mask.any() else 0,
            "h_max": self.h[active_mask].max().item() if active_mask.any() else 0,
            "usage_mean": self.usage[active_mask].float().mean().item() if active_mask.any() else 0,
            "age_mean": self.age[active_mask].float().mean().item() if active_mask.any() else 0,
            "n_texts": n_texts,
        }

    def state_dict_custom(self) -> dict:
        """Returns the state for saving, including texts."""
        return {
            "K": self.K.cpu(),
            "K_context": self.K_context.cpu(),
            "K_terrain": self.K_terrain.cpu(),
            "V": self.V.cpu(),
            "h": self.h.cpu(),
            "e": self.e.cpu(),
            "usage": self.usage.cpu(),
            "age": self.age.cpu(),
            "active": self.active.cpu(),
            "key_texts": list(self.key_texts),
            "value_texts": list(self.value_texts),
            "memory_ids": list(self.memory_ids),
            "provenances": deepcopy(self.provenances),
            "record_metadata_version": self.RECORD_METADATA_VERSION,
            "n_centers": self.n_centers,
            "d_key": self.d_key,
            "d_value": self.d_value,
            "d_emotion": self.d_emotion,
            "d_context": self.d_context,
            "d_terrain": self.d_terrain,
            "sigma_read": self.sigma_read,
            "sigma_write": self.sigma_write,
            "leak": self.leak,
            "leak_emotion": self.leak_emotion,
            "leak_value": self.leak_value,
            "alpha_value": self.alpha_value,
            "alpha_emotion": self.alpha_emotion,
            "alpha_key": self.alpha_key,
            "weight_context": self.weight_context,
            "weight_semantic": self.weight_semantic,
            "weight_terrain": self.weight_terrain,
            "use_hybrid_metric": self.use_hybrid_metric,
            "minkowski_p": self.minkowski_p,
            "weight_cosine": self.weight_cosine,
            "weight_minkowski": self.weight_minkowski,
            "hybrid_candidates": self.hybrid_candidates,
            "total_step": self.total_step,
        }

    @classmethod
    def from_state_dict(cls, state: dict, device: str = "cpu") -> 'MemoryCenters':
        """Loads centers from state."""
        centers = cls(
            n_centers=state["n_centers"],
            d_key=state["d_key"],
            d_value=state["d_value"],
            d_emotion=state["d_emotion"],
            sigma_read=state["sigma_read"],
            sigma_write=state["sigma_write"],
            leak=state["leak"],
            leak_emotion=state["leak_emotion"],
            leak_value=state["leak_value"],
            alpha_value=state["alpha_value"],
            alpha_emotion=state["alpha_emotion"],
            alpha_key=state["alpha_key"],
            use_hybrid_metric=state.get("use_hybrid_metric", True),
            minkowski_p=state.get("minkowski_p", 0.5),
            weight_cosine=state.get("weight_cosine", 0.7),
            weight_minkowski=state.get("weight_minkowski", 0.3),
            hybrid_candidates=state.get("hybrid_candidates", 64),
            device=device,
        )
        centers.K.copy_(state["K"].to(device))
        centers.K_context.copy_(state["K_context"].to(device))
        centers.K_terrain.copy_(state["K_terrain"].to(device))
        centers.V.copy_(state["V"].to(device))
        centers.h.copy_(state["h"].to(device))
        centers.e.copy_(state["e"].to(device))
        centers.usage.copy_(state["usage"].to(device))
        centers.age.copy_(state["age"].to(device))
        centers.active.copy_(state["active"].to(device))
        centers.key_texts = list(state.get("key_texts") or [])[:centers.n_centers]
        centers.value_texts = list(state.get("value_texts") or [])[:centers.n_centers]
        centers.memory_ids = list(state.get("memory_ids") or [])[:centers.n_centers]
        centers.provenances = deepcopy(
            list(state.get("provenances") or [])[:centers.n_centers]
        )
        for values in (
            centers.key_texts,
            centers.value_texts,
            centers.memory_ids,
            centers.provenances,
        ):
            values.extend([None] * (centers.n_centers - len(values)))
        seen_memory_ids = set()
        for idx in torch.where(centers.active)[0].tolist():
            if centers.memory_ids[idx] is None or centers.memory_ids[idx] in seen_memory_ids:
                legacy_identity = (
                    f"biomem:{centers.d_key}:{idx}:"
                    f"{centers.key_texts[idx] or ''}:{centers.value_texts[idx] or ''}"
                )
                candidate = uuid5(NAMESPACE_URL, legacy_identity).hex
                attempt = 0
                while candidate in seen_memory_ids:
                    attempt += 1
                    candidate = uuid5(
                        NAMESPACE_URL, f"{legacy_identity}:{attempt}"
                    ).hex
                centers.memory_ids[idx] = candidate
            seen_memory_ids.add(centers.memory_ids[idx])
            if centers.provenances[idx] is None:
                centers.provenances[idx] = {
                    "source_class": "unknown",
                    "origin": None,
                    "session_id": None,
                    "created_at": None,
                    "updated_at": None,
                    "source_history": [{"source_class": "unknown"}],
                }
        centers.weight_context = state.get("weight_context", 0.25)
        centers.weight_semantic = state.get("weight_semantic", 1.0)
        centers.weight_terrain = state.get("weight_terrain", 0.25)
        centers.total_step = state.get("total_step", 0)
        return centers

    def homeostasis_step(self):
        """
        Applies decay to all active centers.

        Emotions decay toward the neutral value 1.0.
        Intensity and values decay toward 0.
        """
        active_mask = self.active

        # Decay of intensity (toward 0)
        self.h[active_mask] *= (1.0 - self.leak)

        # Decay of values (toward 0)
        self.V[active_mask] *= (1.0 - self.leak_value)

        # Decay of emotions TOWARD THE NEUTRAL VALUE 1.0
        # e ← e + λ_e * (1.0 - e)  =  (1-λ_e)*e + λ_e*1.0
        # This ensures exponential approach to 1.0
        self.e[active_mask] = (
            (1.0 - self.leak_emotion) * self.e[active_mask] +
            self.leak_emotion * 1.0
        )

        # Increment age
        self.age[active_mask] += 1

        self.total_step += 1
