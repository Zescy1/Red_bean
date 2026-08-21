import json
import math
import os
from typing import List, Dict, Tuple, Optional

import numpy as np
from skimage.color import deltaE_ciede2000


# --- CIE Lab Color Space Conversion ---

def _rgb_to_xyz(r: float, g: float, b: float) -> Tuple[float, float, float]:
    """Convert sRGB [0,255] to CIE XYZ."""
    # Normalize to [0, 1]
    r_lin = r / 255.0
    g_lin = g / 255.0
    b_lin = b / 255.0

    # Inverse sRGB companding
    def linearize(c):
        if c > 0.04045:
            return ((c + 0.055) / 1.055) ** 2.4
        else:
            return c / 12.92

    r_lin = linearize(r_lin)
    g_lin = linearize(g_lin)
    b_lin = linearize(b_lin)

    # sRGB to XYZ (D65 illuminant)
    x = r_lin * 0.4124564 + g_lin * 0.3575761 + b_lin * 0.1804375
    y = r_lin * 0.2126729 + g_lin * 0.7151522 + b_lin * 0.0721750
    z = r_lin * 0.0193339 + g_lin * 0.1191920 + b_lin * 0.9503041

    return x, y, z


def _xyz_to_lab(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Convert CIE XYZ to CIE Lab (D65 reference white)."""
    # D65 reference white
    xn, yn, zn = 0.95047, 1.00000, 1.08883

    def f(t):
        delta = 6.0 / 29.0
        if t > delta ** 3:
            return t ** (1.0 / 3.0)
        else:
            return t / (3.0 * delta * delta) + 4.0 / 29.0

    fx = f(x / xn)
    fy = f(y / yn)
    fz = f(z / zn)

    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)

    return L, a, b


def rgb_to_lab(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """Convert RGB [0,255] to CIE Lab."""
    x, y, z = _rgb_to_xyz(float(r), float(g), float(b))
    return _xyz_to_lab(x, y, z)


def rgb_array_to_lab(rgb_array: np.ndarray) -> np.ndarray:
    """Convert an array of RGB values (N, 3) to CIE Lab (N, 3).
    Vectorized version for performance.
    """
    rgb = rgb_array.astype(np.float64) / 255.0

    # Inverse sRGB companding
    mask = rgb > 0.04045
    rgb_lin = np.where(mask, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)

    # sRGB to XYZ matrix (D65)
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041]
    ])
    xyz = rgb_lin @ M.T

    # D65 reference white
    ref_white = np.array([0.95047, 1.00000, 1.08883])
    xyz_norm = xyz / ref_white

    # f function
    delta = 6.0 / 29.0
    delta3 = delta ** 3
    mask = xyz_norm > delta3
    f_xyz = np.where(mask, np.cbrt(xyz_norm), xyz_norm / (3.0 * delta * delta) + 4.0 / 29.0)

    L = 116.0 * f_xyz[:, 1] - 16.0
    a = 500.0 * (f_xyz[:, 0] - f_xyz[:, 1])
    b_val = 200.0 * (f_xyz[:, 1] - f_xyz[:, 2])

    return np.stack([L, a, b_val], axis=-1)


# --- Color Distance (CIEDE2000 simplified as Lab Euclidean) ---

def lab_distance(lab1: Tuple[float, float, float], lab2: Tuple[float, float, float]) -> float:
    """Compute Euclidean distance in CIE Lab space."""
    dL = lab1[0] - lab2[0]
    da = lab1[1] - lab2[1]
    db = lab1[2] - lab2[2]
    return math.sqrt(dL * dL + da * da + db * db)


def lab_distance_batch(lab_pixel: np.ndarray, lab_palette: np.ndarray) -> np.ndarray:
    """Compute Lab Euclidean distance from one pixel to all palette colors.

    Args:
        lab_pixel: (3,) Lab values of a single pixel
        lab_palette: (N, 3) Lab values of the palette

    Returns:
        (N,) distances
    """
    diff = lab_palette - lab_pixel
    return np.sqrt(np.sum(diff * diff, axis=1))


# --- ddny 风格：RGB 欧氏距离 + 阈值匹配 ---
# 对应 github.com/fusuguo/ddny 的 colorMatch.ts：rgbDistance + COLOR_THRESHOLD=20
COLOR_THRESHOLD = 20  # RGB 欧氏距离阈值，距离 < 阈值视为同一颜色


def rgb_distance_batch(rgb_pixels: np.ndarray, rgb_palette: np.ndarray) -> np.ndarray:
    """RGB 欧氏距离矩阵：一批像素到全部色库颜色。

    Args:
        rgb_pixels: (M, 3) RGB 值
        rgb_palette: (N, 3) 色库 RGB 值

    Returns:
        (M, N) 距离矩阵，distance = sqrt((dr)^2 + (dg)^2 + (db)^2)
    """
    diff = rgb_pixels[:, np.newaxis, :] - rgb_palette[np.newaxis, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


# --- Palette Loading and Management ---

class ArtkalPalette:
    """Manages the Artkal M-series color palette."""

    def __init__(self, json_path: Optional[str] = None, presets_path: Optional[str] = None):
        if json_path is None:
            json_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'artkal_m_series.json')
        if presets_path is None:
            presets_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'artkal_presets.json')
        
        with open(json_path, 'r', encoding='utf-8') as f:
            self.colors: List[Dict] = json.load(f)
        
        # Load presets
        with open(presets_path, 'r', encoding='utf-8') as f:
            self.presets: Dict = json.load(f)
        
        # Build lookup structures
        self._code_to_color: Dict[str, Dict] = {}
        self._hex_to_color: Dict[str, Dict] = {}
        self._rgb_array: Optional[np.ndarray] = None
        self._lab_array: Optional[np.ndarray] = None

        self._code_to_index: Dict[str, int] = {}

        for i, c in enumerate(self.colors):
            self._code_to_color[c['code']] = c
            self._hex_to_color[c['hex'].upper()] = c
            self._code_to_index[c['code']] = i

    @property
    def rgb_array(self) -> np.ndarray:
        """Lazy-loaded (N, 3) numpy array of all palette RGB values."""
        if self._rgb_array is None:
            self._rgb_array = np.array([c['rgb'] for c in self.colors], dtype=np.float64)
        return self._rgb_array

    @property
    def lab_array(self) -> np.ndarray:
        """Lazy-loaded (N, 3) numpy array of all palette Lab values."""
        if self._lab_array is None:
            self._lab_array = rgb_array_to_lab(self.rgb_array)
        return self._lab_array

    def get_by_code(self, code: str) -> Optional[Dict]:
        """Get color entry by Artkal code (e.g., 'A1')."""
        return self._code_to_color.get(code)

    def get_preset_indices(self, preset_key: str) -> Optional[np.ndarray]:
        """Get palette indices for a preset subset.

        Args:
            preset_key: "96", "120", "144", "168", or "221"

        Returns:
            numpy array of palette indices, or None for full palette (221).
        """
        preset = self.presets.get(preset_key)
        if preset is None or preset.get('codes') is None:
            return None  # Full palette
        
        codes = preset['codes']
        indices = []
        for code in codes:
            idx = self._code_to_index.get(code, -1)
            if idx >= 0:
                indices.append(idx)
        
        if not indices:
            return None
        return np.array(sorted(indices), dtype=np.int32)

    def get_preset_colors(self, preset_key: str) -> List[Dict]:
        """Get the list of color dicts for a given preset."""
        indices = self.get_preset_indices(preset_key)
        if indices is None:
            return self.colors
        return [self.colors[i] for i in indices]

    def get_index_by_code(self, code: str) -> int:
        """Get palette index by Artkal code. Returns -1 if not found."""
        return self._code_to_index.get(code, -1)

    def get_by_hex(self, hex_val: str) -> Optional[Dict]:
        """Get color entry by hex value."""
        return self._hex_to_color.get(hex_val.upper())

    def find_closest(self, r: int, g: int, b: int, allowed_indices: Optional[np.ndarray] = None) -> Dict:
        """Find the closest palette color to the given RGB value using RGB Euclidean distance.

        Args:
            r, g, b: Input color in RGB [0, 255]
            allowed_indices: Optional array of allowed palette indices.
                             If None, search all colors.

        Returns:
            The closest palette color dict.
        """
        rgb_pixel = np.array([r, g, b], dtype=np.float64)

        if allowed_indices is not None:
            subset = self.rgb_array[allowed_indices]
            distances = rgb_distance_batch(rgb_pixel[None, :], subset)[0]
            min_idx = int(np.argmin(distances))
            return self.colors[allowed_indices[min_idx]]
        else:
            distances = rgb_distance_batch(rgb_pixel[None, :], self.rgb_array)[0]
            min_idx = int(np.argmin(distances))
            return self.colors[min_idx]

    def find_closest_batch(self, rgb_pixels: np.ndarray,
                           allowed_indices: Optional[np.ndarray] = None) -> np.ndarray:
        """Find closest palette colors for a batch of pixels using CIEDE2000.

        Args:
            rgb_pixels: (M, 3) array of RGB values
            allowed_indices: Optional array of allowed palette indices.

        Returns:
            (M,) array of palette indices (into self.colors)
        """
        pixel_lab = rgb_array_to_lab(rgb_pixels)

        if allowed_indices is not None:
            palette_lab = self.lab_array[allowed_indices]
        else:
            palette_lab = self.lab_array

        # (像素数, 1, 3) 与 (1, 色库数, 3) 广播
        distances = deltaE_ciede2000(
            pixel_lab[:, None, :],
            palette_lab[None, :, :]
        )

        local_indices = np.argmin(distances, axis=1)

        if allowed_indices is not None:
            return allowed_indices[local_indices]

        return local_indices

    def find_closest_batch_with_margin(self, rgb_pixels: np.ndarray,
                                       allowed_indices: Optional[np.ndarray] = None
                                       ) -> Tuple[np.ndarray, np.ndarray]:
        """CIEDE2000 最近色匹配，同时返回每格的「模糊度」margin = 次近色距 - 最近色距。

        margin 小说明 top1/top2 两个色很接近（如米白/浅黄这类白色系），颜色匹配不可靠，
        应交给读色号文字兜底；margin 大说明 top1 明确胜出，颜色匹配可信。

        Returns:
            (indices, margins)：indices 为 (M,) 色库索引，margins 为 (M,) ΔE 差值。
        """
        pixel_lab = rgb_array_to_lab(rgb_pixels)

        if allowed_indices is not None:
            palette_lab = self.lab_array[allowed_indices]
        else:
            palette_lab = self.lab_array

        distances = deltaE_ciede2000(pixel_lab[:, None, :], palette_lab[None, :, :])

        local_indices = np.argmin(distances, axis=1)

        # margin = 次近色距离 - 最近色距离（若候选色 <2 个，则无歧义，margin 设为大值）
        if distances.shape[1] >= 2:
            part = np.partition(distances, 1, axis=1)
            margins = part[:, 1] - part[:, 0]
        else:
            margins = np.full(distances.shape[0], np.inf)

        if allowed_indices is not None:
            indices = allowed_indices[local_indices]
        else:
            indices = local_indices

        return indices, margins

    def match_batch_threshold(self, rgb_pixels: np.ndarray,
                              allowed_indices: Optional[np.ndarray] = None,
                              threshold: float = COLOR_THRESHOLD) -> np.ndarray:
        """ddny 风格阈值匹配：RGB 欧氏距离 < threshold 才匹配，否则返回 -1（不匹配）。

        对应 github.com/fusuguo/ddny 的 isSameColor：rgbDistance < COLOR_THRESHOLD(20)。

        Args:
            rgb_pixels: (M, 3) array of RGB values
            allowed_indices: Optional array of allowed palette indices.
            threshold: RGB 欧氏距离阈值（默认 20）

        Returns:
            (M,) array of palette indices，距离 >= threshold 的像素为 -1
        """
        if allowed_indices is not None:
            subset = self.rgb_array[allowed_indices]
            distances = rgb_distance_batch(rgb_pixels, subset)
            local = np.argmin(distances, axis=1)
            result = allowed_indices[local].astype(np.int64)
            result[distances[np.arange(len(rgb_pixels)), local] >= threshold] = -1
            return result
        distances = rgb_distance_batch(rgb_pixels, self.rgb_array)
        local = np.argmin(distances, axis=1)
        result = local.astype(np.int64)
        result[distances[np.arange(len(rgb_pixels)), local] >= threshold] = -1
        return result

    def select_top_n_colors(self, rgb_pixels: np.ndarray, n: int,
                            allowed_indices: Optional[np.ndarray] = None) -> np.ndarray:
        """Select the top N palette colors that best represent the given pixels.

        Strategy: Map each pixel to its closest palette color, then select
        the top N most frequently used colors.

        Args:
            rgb_pixels: (M, 3) array of RGB values from the image
            n: Number of colors to select
            allowed_indices: Optional array of allowed palette indices (preset filtering)

        Returns:
            Array of palette indices for the selected N colors
        """
        # Map all pixels to closest palette colors
        all_indices = self.find_closest_batch(rgb_pixels, allowed_indices=allowed_indices)

        # Count frequency
        unique, counts = np.unique(all_indices, return_counts=True)

        # Sort by frequency (descending) and take top N
        sorted_order = np.argsort(-counts)
        top_n = unique[sorted_order[:n]]

        return np.sort(top_n)
