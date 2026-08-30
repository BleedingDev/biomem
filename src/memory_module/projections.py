'''
Projection layers for the Memory Module.

Implements:
- 384D → 64D projection (embeddings → LTM keys)
- 384D → 16D projection (embeddings → STM keys)
- 384D → 128D projection (embeddings → values)
- 64D → 3D projection (LTM keys → terrain)
- 16D → 3D projection (STM keys → terrain)
- 16D → 64D projection (STM → LTM during consolidation)
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional


class MemoryProjection(nn.Module):
    """
    Projection from the embedding space into the memory space.

    f_q: X → q = norm(W_q X + b_q)
    """

    def __init__(self, d_input: int, d_output: int, bias: bool = True):
        super().__init__()
        self.proj = nn.Linear(d_input, d_output, bias=bias)
        nn.init.xavier_uniform_(self.proj.weight)
        if bias:
            nn.init.zeros_(self.proj.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: [B, T, d_input] or [N, d_input]

        Returns:
            q: [..., d_output] normalized vector
        """
        projected = self.proj(x)
        return F.normalize(projected, dim=-1)


class TerrainProjection(nn.Module):
    """
    Projection from the memory space into 3D terrain.

    C: k → z = tanh(W_c k + b_c)

    Output in [-1, 1]^3 for grid_sample compatibility.
    """

    ...

    def __init__(self, d_input: int, d_output: int = 3):
        super().__init__()
        self.proj = nn.Linear(d_input, d_output)
        nn.init.xavier_uniform_(self.proj.weight, gain=0.5)
        nn.init.zeros_(self.proj.bias)

    def forward(self, k: torch.Tensor) -> torch.Tensor:
        """
        Args:
            k: [..., d_input] memory key

        Returns:
            z: [..., 3] position in the terrain [-1, 1]^3
        """
        return torch.tanh(self.proj(k))


class ConsolidationProjection(nn.Module):
    """
    Projection from the STM (16D) into the LTM (64D) space.

    U: K_stm → norm(U @ K_stm)
    """

    ...
    ...

    def __init__(self, d_stm: int = 16, d_ltm: int = 64):
        super().__init__()
        self.proj = nn.Linear(d_stm, d_ltm, bias=False)
        nn.init.orthogonal_(self.proj.weight)

    def forward(self, k_stm: torch.Tensor) -> torch.Tensor:
        """
        Args:
            k_stm: [..., d_stm]

        Returns:
            k_ltm: [..., d_ltm] normalized
        """
        return F.normalize(self.proj(k_stm), dim=-1)


class ValueProjection(nn.Module):
    """
    Projection for creating memory values.

    W_v: embedding → v = W_v x + b_v
    """

    def __init__(self, d_input: int, d_value: int):
        super().__init__()
        self.proj = nn.Linear(d_input, d_value)
        nn.init.xavier_uniform_(self.proj.weight)
        nn.init.zeros_(self.proj.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.proj(x)


class ContextProjection(nn.Module):
    """
    Projection for the context fingerprint (compound keys).

    Generates a discriminative "hash" for distinguishing similar items.
    """

    ...

    def __init__(self, d_input: int, d_context: int = 16):
        super().__init__()
        self.proj = nn.Linear(d_input, d_context, bias=False)
        nn.init.orthogonal_(self.proj.weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.normalize(self.proj(x), dim=-1)


class ProjectionBundle(nn.Module):
    """
    All projections for the memory system in one bundle.
    """

    ...
    ...
    ...
    ...
    ...

    def __init__(self, d_embedding: int = 384, d_ltm_key: int = 64, d_stm_key: int = 16, d_value: int = 128, d_context: int = 16):
        super().__init__()
        self.d_embedding = d_embedding
        self.d_ltm_key = d_ltm_key
        self.d_stm_key = d_stm_key
        self.d_value = d_value
        self.to_ltm_key = MemoryProjection(d_embedding, d_ltm_key)
        self.to_stm_key = MemoryProjection(d_embedding, d_stm_key)
        self.to_value = ValueProjection(d_embedding, d_value)
        self.to_context = ContextProjection(d_embedding, d_context)
        self.ltm_to_terrain = TerrainProjection(d_ltm_key, 3)
        self.stm_to_terrain = TerrainProjection(d_stm_key, 3)
        self.stm_to_ltm = ConsolidationProjection(d_stm_key, d_ltm_key)

    def project_to_ltm(self, x: torch.Tensor) -> torch.Tensor:
        'Embedding → LTM keys.'
        return self.to_ltm_key(x)

    def project_to_stm(self, x: torch.Tensor) -> torch.Tensor:
        'Embedding → STM keys.'
        return self.to_stm_key(x)

    def project_to_value(self, x: torch.Tensor) -> torch.Tensor:
        'Embedding → values.'
        return self.to_value(x)

    def project_to_context(self, x: torch.Tensor) -> torch.Tensor:
        'Embedding → context fingerprint.'
        return self.to_context(x)

    def ltm_to_3d(self, k: torch.Tensor) -> torch.Tensor:
        'LTM keys → 3D position.'
        return self.ltm_to_terrain(k)

    def stm_to_3d(self, k: torch.Tensor) -> torch.Tensor:
        'STM keys → 3D position.'
        return self.stm_to_terrain(k)

    def consolidate_key(self, k_stm: torch.Tensor) -> torch.Tensor:
        'STM key → LTM key.'
        return self.stm_to_ltm(k_stm)
