'''
3D terrain layer for the Memory Module.

Implements:
- 3D grid for intensity (GS) and emotions (CMYK-like)
- Diffusion (Laplacian operator) - smoothing
- Homeostasis (leak) - slow return to the plane
- Trilinear sampling for reading
- Gaussian splat for writing
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple, Optional
import math


class Terrain3D(nn.Module):
    """
    3D terrain layer with diffusion and homeostasis.

    Contains:
        - H: Intensity/GS terrain [Gx, Gy, Gz]
        - E: Emotion terrain [Gx, Gy, Gz, 4]
    """

    # Bare string-literal statements are kept as `...` placeholders to match
    # the compiled module layout (the optimizer removes them).
    ...
    ...
    ...
    ...
    ...
    ...

    def __init__(self, resolution: int = 48, n_emotions: int = 4, alpha_h: float = 0.002, alpha_e: float = 0.001, leak: float = 5e-05, device: str = 'cpu'):
        super().__init__()
        self.device = device
        self.resolution = resolution
        self.n_emotions = n_emotions
        self.alpha_h = alpha_h
        self.alpha_e = alpha_e
        self.leak = leak
        self.register_buffer('H', torch.zeros(1, 1, resolution, resolution, resolution, device=device))
        self.register_buffer('E', torch.ones(1, n_emotions, resolution, resolution, resolution, device=device))
        laplacian_kernel = torch.zeros(1, 1, 3, 3, 3, device=device)
        laplacian_kernel[(0, 0, 1, 1, 1)] = -6
        laplacian_kernel[(0, 0, 0, 1, 1)] = 1
        laplacian_kernel[(0, 0, 2, 1, 1)] = 1
        laplacian_kernel[(0, 0, 1, 0, 1)] = 1
        laplacian_kernel[(0, 0, 1, 2, 1)] = 1
        laplacian_kernel[(0, 0, 1, 1, 0)] = 1
        laplacian_kernel[(0, 0, 1, 1, 2)] = 1
        self.register_buffer('laplacian_kernel', laplacian_kernel)

    def _compute_laplacian(self, grid: torch.Tensor) -> torch.Tensor:
        """
        Computes the Laplacian operator (without applying it).
        """
        C = grid.shape[1]
        return F.conv3d(F.pad(grid, (1, 1, 1, 1, 1, 1), mode='replicate'), self.laplacian_kernel.expand(C, 1, 3, 3, 3), groups=C)

    def _apply_diffusion(self, grid: torch.Tensor, alpha: float) -> torch.Tensor:
        """
        Applies one diffusion step using the Laplacian operator.

        Stability: α ≤ 1/6 for 6 neighbors
        """
        laplacian = self._compute_laplacian(grid)
        return grid + alpha * laplacian

    def step(self):
        """
        One step of diffusion + homeostasis.

        Implements the formulas from the spec:
            H³ ← (1 − λ₃)H³ + α_H ∇²H³
            E³ ← (1 − λ₃)(E³ - 1) + 1 + α_E ∇²E³

        Emotions decay toward the neutral value 1.0 (like PlantNet hormones).
        Intensity decays toward 0.

        Call after each interaction.
        """
        laplacian_H = self._compute_laplacian(self.H)
        laplacian_E = self._compute_laplacian(self.E)
        self.H.mul_(1 - self.leak).add_(self.alpha_h * laplacian_H)
        self.E.mul_(1 - self.leak).add_(self.leak).add_(self.alpha_e * laplacian_E)
        self.H.clamp_(min=0)
        self.E.clamp_(min=0)

    def sample(self, positions: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Trilinear sampling from the terrain.

        Args:
            positions: [B, T, 3] in the range [-1, 1]

        Returns:
            p_H: [B, T] intensity
            p_E: [B, T, 4] emotions
        """
        B, T, _ = positions.shape
        grid = positions.view(1, B * T, 1, 1, 3)
        H_expanded = self.H.expand(1, 1, -1, -1, -1)
        p_H = F.grid_sample(H_expanded, grid, mode='bilinear', padding_mode='border', align_corners=True)
        p_H = p_H.view(B, T)
        E_expanded = self.E.expand(1, -1, -1, -1, -1)
        p_E = F.grid_sample(E_expanded, grid, mode='bilinear', padding_mode='border', align_corners=True)
        p_E = p_E.view(self.n_emotions, B, T).permute(1, 2, 0)
        return (p_H, p_E)

    ...
    ...
    ...

    def splat(self, positions: torch.Tensor, intensities: torch.Tensor, emotions: Optional[torch.Tensor] = None, sigma: float = 0.1, eta: float = 0.01):
        """
        Gaussian splat write into the terrain.

        Args:
            positions: [N, 3] positions in [-1, 1]
            intensities: [N] write strength (ω)
            emotions: [N, 4] emotion vectors (optional)
            sigma: kernel width
            eta: global write strength
        """
        N = positions.shape[0]
        G = self.resolution
        grid_pos = (positions + 1) * 0.5 * (G - 1)
        radius = max(2, int(3 * sigma * G / 2))
        for i in range(N):
            cx, cy, cz = grid_pos[i].long().clamp(0, G - 1).tolist()
            omega = intensities[i].item() * eta
            x_min = max(0, cx - radius)
            x_max = min(G, cx + radius + 1)
            y_min = max(0, cy - radius)
            y_max = min(G, cy + radius + 1)
            z_min = max(0, cz - radius)
            z_max = min(G, cz + radius + 1)
            x_range = torch.arange(x_min, x_max, device=positions.device, dtype=positions.dtype)
            y_range = torch.arange(y_min, y_max, device=positions.device, dtype=positions.dtype)
            z_range = torch.arange(z_min, z_max, device=positions.device, dtype=positions.dtype)
            xx, yy, zz = torch.meshgrid(x_range, y_range, z_range, indexing='ij')
            dist_sq = (xx - grid_pos[i, 0]) ** 2 + (yy - grid_pos[i, 1]) ** 2 + (zz - grid_pos[i, 2]) ** 2
            sigma_grid = sigma * G / 2
            kernel = torch.exp(-dist_sq / (2 * sigma_grid ** 2))
            self.H[0, 0, x_min:x_max, y_min:y_max, z_min:z_max] += omega * kernel
            if emotions is not None:
                for e_idx in range(self.n_emotions):
                    self.E[0, e_idx, x_min:x_max, y_min:y_max, z_min:z_max] += omega * kernel * emotions[(i, e_idx)].item()

    def blur(self, sigma: float = 1.0) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns a blurred version of the terrain (for STM→LTM consolidation).
        """
        kernel_size = int(6 * sigma) | 1
        kernel_size = max(3, kernel_size)
        x = torch.arange(kernel_size, device=self.H.device, dtype=self.H.dtype)
        x = x - kernel_size // 2
        kernel_1d = torch.exp(-(x ** 2) / (2 * sigma ** 2))
        kernel_1d = kernel_1d / kernel_1d.sum()
        H_blurred = self.H.clone()
        E_blurred = self.E.clone()
        return (H_blurred, E_blurred)

    ...
    ...
    ...

    def merge_from(self, other: 'Terrain3D', xi_h: float = 0.01, xi_e: float = 0.01, blur_sigma: float = 2.0):
        """
        Merges the (blurred) terrain from another terrain (STM→LTM).
        """
        H_blurred, E_blurred = other.blur(blur_sigma)
        self.H.add_(xi_h * H_blurred)
        self.E.add_(xi_e * E_blurred)

    def reset(self):
        'Resets the terrain to its initial state.'
        self.H.zero_()
        self.E.fill_(1)

    def get_stats(self) -> dict:
        'Returns terrain statistics.'
        return {'H_mean': self.H.mean().item(), 'H_max': self.H.max().item(), 'H_std': self.H.std().item(), 'H_nonzero': (self.H > 1e-06).sum().item(), 'E_mean': self.E.mean().item(), 'E_max': self.E.abs().max().item()}

    def state_dict_custom(self) -> dict:
        'Returns the state for saving.'
        return {'H': self.H.cpu(), 'E': self.E.cpu(), 'resolution': self.resolution, 'n_emotions': self.n_emotions, 'alpha_h': self.alpha_h, 'alpha_e': self.alpha_e, 'leak': self.leak}

    @classmethod
    def from_state_dict(cls, state: dict, device: str = 'cpu') -> 'Terrain3D':
        'Loads the terrain from state.'
        terrain = cls(state['resolution'], state['n_emotions'], state['alpha_h'], state['alpha_e'], state['leak'], device=device)
        terrain.H.copy_(state['H'].to(device))
        terrain.E.copy_(state['E'].to(device))
        return terrain
