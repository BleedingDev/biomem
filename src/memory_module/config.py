# memory_module/config.py — biomem MemoryConfig
"""
Configuration for the memory module — standalone text memory system.

Two-layer architecture:
- LTM (Long-Term Memory): 64D kernel field with a half-life of ~1 year
- STM (Short-Term Memory): 16D buffer with a half-life of days to weeks
"""
import math
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class MemoryConfig:
    """Main configuration of the memory system."""

    # ========================================
    # Model & dimensions
    # ========================================
    embedding_model: str = 'paraphrase-multilingual-MiniLM-L12-v2'
    embedding_dim: int = 384
    d_ltm_key: int = 64          # Dimension of LTM keys
    d_stm_key: int = 16          # Dimension of STM keys
    d_value: int = 128           # Dimension of values (d_v)
    d_emotion: int = 4           # Hormones: dopamine, serotonin, cortisol, oxytocin

    # 3D terrain
    terrain_resolution: int = 48  # 3D grid resolution (48^3)

    # Number of centers
    n_ltm_centers: int = 4096
    n_stm_centers: int = 512

    # ========================================
    # LTM (64D) — half-life ~1 year
    # ========================================
    ltm_leak: float = 2.66e-05
    ltm_leak_emotion: float = 3.5e-05
    ltm_leak_value: float = 2.1e-05

    ltm_alpha_value: float = 0.03
    ltm_alpha_emotion: float = 0.01

    ltm_sigma_read: float = 0.5
    ltm_sigma_write: float = 0.15

    ltm_top_k_read: int = 32
    ltm_top_k_write: int = 16

    ltm_new_center_threshold: float = 0.78

    # ========================================
    # LTM 3D terrain
    # ========================================
    terrain_ltm_eta: float = 0.005
    terrain_ltm_lambda: float = 3.5e-05
    terrain_ltm_alpha_h: float = 0.002
    terrain_ltm_alpha_e: float = 0.001
    terrain_ltm_sigma: float = 0.1

    # ========================================
    # STM (16D) — half-life of days to weeks
    # ========================================
    stm_leak: float = 0.0035
    stm_leak_emotion: float = 0.0049
    stm_leak_value: float = 0.0028

    stm_alpha_value: float = 0.1
    stm_alpha_emotion: float = 0.08

    stm_sigma_read: float = 0.4
    stm_sigma_write: float = 0.2

    stm_top_k_read: int = 16
    stm_top_k_write: int = 8

    stm_new_center_threshold: float = 0.5

    # ========================================
    # Associations / STM 3D terrain (faster)
    # ========================================
    max_associations: int = 5

    terrain_stm_eta: float = 0.02
    terrain_stm_lambda: float = 0.0007
    terrain_stm_alpha_h: float = 0.02
    terrain_stm_alpha_e: float = 0.01

    # ========================================
    # Fatigue & Consolidation (Sleep)
    # ========================================
    fatigue_leak: float = 0.007
    fatigue_threshold: float = 2.5

    consolidation_top_m: int = 128
    consolidation_kappa: float = 0.8
    consolidation_min_intensity: float = 0.3
    consolidation_xi_h: float = 0.005
    consolidation_xi_e: float = 0.003

    normalization_rho_f: float = 0.2
    normalization_c_v: float = 2.0

    # ========================================
    # Merge/Prune (capacity management)
    # ========================================
    merge_similarity_threshold: float = 0.95
    prune_intensity_threshold: float = 0.001
    prune_min_age: int = 300

    # ========================================
    # Writing
    # ========================================
    write_strength_base: float = 1.0
    write_novelty_weight: float = 2.0
    write_surprise_weight: float = 0.3
    write_emotion_weight: float = 0.3
    write_bias: float = -1.0

    # 3D→64D reinforcement on write
    terrain_boost: float = 0.1
    terrain_gamma: float = 1.0

    # ========================================
    # Persistence
    # ========================================
    state_file: str = 'memory_state.bdbm'
    legacy_state_file: str = 'memory_state.pt'
    auto_save: bool = True
    auto_save_interval: int = 100

    # ========================================
    # Server / session
    # ========================================
    ws_host: str = '127.0.0.1'
    ws_port: int = 8765
    ws_allowed_origins: tuple = (
        'https://biomem.app',
        'https://gemini.google.com',
        'https://notebooklm.google.com',
        'https://chatgpt.com',
        'https://chat.openai.com',
        'https://claude.ai',
        'https://www.perplexity.ai',
        'https://perplexity.ai',
        'https://grok.com',
        'https://www.grok.com',
        'http://localhost',
        'http://127.0.0.1',
    )
    session_ttl: int = 600
    session_cleanup_interval: int = 60

    data_dir: str = ''


def compute_leak_from_halflife(halflife_steps: int) -> float:
    """Compute the leak coefficient from a half-life in steps."""
    return 1.0 - math.pow(2.0, -1.0 / halflife_steps)


DEFAULT_CONFIG = MemoryConfig()
