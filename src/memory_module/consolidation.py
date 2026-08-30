'''
STM → LTM consolidation (sleep).

Implements:
- Fatigue detection (STM saturation)
- Selection of significant STM centers
- Transfer to LTM (16D → 64D)
- STM 3D → LTM 3D transfer (blur)
- STM normalization (log compression instead of erasure)

Math from the spec (§7.2–7.4):
- F ← (1 − λ_F)F + Σ ω_s^stm         (fatigue as an exponential sum of writes)
- When F > Θ → sleep
- Select top-M centers by h_i^s (h ≥ consolidation_min_intensity)
- Map 16D → 64D: q^64 = norm(U @ K^s)
- ω_i = κ·h_i; write pseudo-segments into LTM
- STM terrain → LTM terrain: H³ ← H³ + ξ_H·blur(H_s³), E³ ← E³ + ξ_E·blur(E_s³)
- Normalization: h^s ← log(1 + h^s), V^s ← V^s/(1 + ||V^s||/c_V), e^s ← tanh(e^s)
- F ← ρ_F·F
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, Optional, Tuple

from .terrain_3d import Terrain3D
from .memory_centers import MemoryCenters
from .projections import ConsolidationProjection


class SleepConsolidator(nn.Module):
    """
    Consolidator for transferring memories from STM to LTM.
    """

    def __init__(self, d_stm_key: 'int' = 16, d_ltm_key: 'int' = 64, d_value: 'int' = 128, d_emotion: 'int' = 4, fatigue_leak: 'float' = 0.007, fatigue_threshold: 'float' = 2.5, consolidation_top_m: 'int' = 128, consolidation_kappa: 'float' = 0.8, consolidation_min_intensity: 'float' = 0.3, consolidation_xi_h: 'float' = 0.005, consolidation_xi_e: 'float' = 0.003, normalization_rho_f: 'float' = 0.2, normalization_c_v: 'float' = 2.0, blur_sigma: 'float' = 2.0, ltm_new_center_threshold: 'float' = 0.78):
        super().__init__()
        self.d_stm_key = d_stm_key
        self.d_ltm_key = d_ltm_key
        self.d_value = d_value
        self.d_emotion = d_emotion
        self.fatigue_leak = fatigue_leak
        self.fatigue_threshold = fatigue_threshold
        self.consolidation_top_m = consolidation_top_m
        self.consolidation_kappa = consolidation_kappa
        self.consolidation_min_intensity = consolidation_min_intensity
        self.consolidation_xi_h = consolidation_xi_h
        self.consolidation_xi_e = consolidation_xi_e
        self.normalization_rho_f = normalization_rho_f
        self.normalization_c_v = normalization_c_v
        self.blur_sigma = blur_sigma
        self.ltm_new_center_threshold = ltm_new_center_threshold

        # 16D → 64D projection (ConsolidationProjection: U: K_stm → norm(U @ K_stm))
        self.stm_to_ltm = ConsolidationProjection(d_stm_key, d_ltm_key)

        # Current fatigue (scalar buffer)
        self.register_buffer('fatigue', torch.tensor(0.0))

    def update_fatigue(self, write_strength: 'float'):
        """
        Updates the fatigue level.

        Args:
            write_strength: sum of write strengths since the last update
        """
        # Accumulate a bounded fraction of write strength after applying leak:
        # F ← (1 − λ_F)·F + 0.1·write_strength.
        self.fatigue = (1.0 - self.fatigue_leak) * self.fatigue + 0.1 * write_strength

    def should_sleep(self) -> 'bool':
        """Decides whether it is time to sleep (consolidate)."""
        return self.fatigue.item() > self.fatigue_threshold

    def consolidate(self, stm_centers: 'MemoryCenters', ltm_centers: 'MemoryCenters', stm_terrain: 'Terrain3D', ltm_terrain: 'Terrain3D') -> 'Dict':
        """
        Performs STM → LTM consolidation, including texts.

        Args:
            stm_centers: STM memory centers
            ltm_centers: LTM memory centers
            stm_terrain: STM 3D terrain
            ltm_terrain: LTM 3D terrain

        Returns:
            Dict with consolidation statistics
        """
        stats = {
            'pre_fatigue': self.fatigue.item(),
            'consolidated_centers': 0,
            'new_ltm_centers': 0,
            'integrated_centers': 0,
        }

        # ========================================
        # Step A: Select significant STM centers
        # ========================================
        # Active centers with intensity above the minimum threshold (STM saturation).
        # Top-M by intensity among active centers. The minimum write intensity
        # is applied later when a selected center is transferred to LTM.
        active_indices = torch.where(stm_centers.active)[0]
        if active_indices.shape[0] == 0:
            stats['status'] = 'no_active_stm_centers'
            return stats

        # Sort by intensity, take the top M
        h_active = stm_centers.h[active_indices]
        n_to_consolidate = min(self.consolidation_top_m, active_indices.shape[0])
        top_indices = torch.topk(h_active, n_to_consolidate).indices  # [N] (1-D input)
        selected_indices = active_indices[top_indices]  # [N]

        stats['consolidated_centers'] = selected_indices.shape[0]

        # ========================================
        # Step B: Map 16D → 64D and write into LTM
        # ========================================
        keys_ltm = []
        values_stm = []
        emotions_stm = []
        intensities_stm = []
        context_keys_stm = []
        ages_stm = []
        key_texts_stm = []
        value_texts_stm = []
        memory_ids_stm = []
        provenances_stm = []

        # Transferred centers advance by one consolidation generation.
        add_age = 1

        for idx in selected_indices:
            # Get STM data
            K_stm = stm_centers.K[idx]  # [d_stm_key]
            V_stm = stm_centers.V[idx]  # [d_value]
            e_stm = stm_centers.e[idx]  # [d_emotion]
            h_stm = stm_centers.h[idx]  # scalar

            # Project into LTM space
            K_ltm = self.stm_to_ltm(K_stm.unsqueeze(0)).squeeze(0)  # [d_ltm_key]

            # Write strength: ω = κ·h, floored at the configured
            # consolidation minimum.
            omega = max(
                self.consolidation_kappa * h_stm.item(),
                self.consolidation_min_intensity,
            )

            keys_ltm.append(K_ltm)
            values_stm.append(V_stm)
            emotions_stm.append(e_stm)
            intensities_stm.append(omega)
            context_keys_stm.append(stm_centers.K_context[idx])
            ages_stm.append(int(stm_centers.age[idx].item()) + add_age)
            key_texts_stm.append(stm_centers.key_texts[idx.item()])
            value_texts_stm.append(stm_centers.value_texts[idx.item()])
            memory_ids_stm.append(stm_centers.memory_ids[idx.item()])
            provenances_stm.append(stm_centers.provenances[idx.item()])

        keys_ltm_tensor = F.normalize(torch.stack(keys_ltm), dim=-1)  # [N, 64]
        values_stm_tensor = torch.stack(values_stm)  # [N, d_value]
        emotions_stm_tensor = torch.stack(emotions_stm)  # [N, d_emotion]
        intensities_stm_tensor = torch.tensor(intensities_stm, dtype=torch.float32)  # [N]
        context_keys_tensor = torch.stack(context_keys_stm)  # [N, d_context]
        ages_stm_tensor = torch.tensor(ages_stm, dtype=torch.long)  # [N]
        terrain_positions_ltm = stm_centers.K_terrain[selected_indices]  # [N, 3]

        # Write pseudo-segments into LTM (same rule as a regular write).
        created = ltm_centers.write(
            keys=keys_ltm_tensor,
            values=values_stm_tensor,
            emotions=emotions_stm_tensor,
            intensities=intensities_stm_tensor,
            new_center_threshold=self.ltm_new_center_threshold,
            context_keys=context_keys_tensor,
            terrain_positions=terrain_positions_ltm,
            key_texts=key_texts_stm,
            value_texts=value_texts_stm,
            ages=ages_stm_tensor,
            memory_ids=memory_ids_stm,
            provenances=provenances_stm,
        )
        stats['new_ltm_centers'] = created
        stats['integrated_centers'] = selected_indices.shape[0] - created

        # ========================================
        # Step C: Normalize LTM (log compression after write)
        # ========================================
        # Apply the same logarithmic intensity compression to LTM writes.
        ltm_centers.apply_normalization(c_v=self.normalization_c_v)

        # ========================================
        # Step D: Transfer STM terrain → LTM terrain (blur)
        # ========================================
        ltm_terrain.merge_from(
            stm_terrain,
            xi_h=self.consolidation_xi_h,
            xi_e=self.consolidation_xi_e,
            blur_sigma=self.blur_sigma
        )

        # ========================================
        # Step E: Normalize STM (not erasure!)
        # ========================================
        stm_centers.apply_normalization(c_v=self.normalization_c_v)

        # Reset fatigue: F ← ρ_F·F
        self.fatigue = self.fatigue.mul(self.normalization_rho_f)

        stats['post_fatigue'] = self.fatigue.item()
        stats['status'] = 'success'

        return stats

    def get_fatigue_level(self) -> 'float':
        """Returns the current fatigue level (0-1 relative to the threshold)."""
        return min(1.0, self.fatigue.item() / self.fatigue_threshold)


class AutomaticConsolidator:
    """
    Automatic consolidator that tracks the fatigue level
    and triggers consolidation when needed.
    """

    def __init__(self, consolidator: 'SleepConsolidator', min_interval: 'int' = 100):
        self.consolidator = consolidator
        self.min_interval = min_interval
        # Minimum number of steps between consolidations
        self.steps_since_consolidation = 0

    def step(self, write_strength: 'float', stm_centers: 'MemoryCenters', ltm_centers: 'MemoryCenters', stm_terrain: 'Terrain3D', ltm_terrain: 'Terrain3D') -> 'Optional[Dict]':
        """
        One step - updates fatigue and consolidates if needed.

        Returns:
            Dict with consolidation statistics, or None
        """
        w_new = write_strength
        self.consolidator.update_fatigue(w_new)
        self.steps_since_consolidation += 1

        if (self.consolidator.should_sleep() and
                self.steps_since_consolidation >= self.min_interval):
            stats = self.consolidator.consolidate(
                stm_centers, ltm_centers,
                stm_terrain, ltm_terrain
            )
            self.steps_since_consolidation = 0
            return stats

        return None
