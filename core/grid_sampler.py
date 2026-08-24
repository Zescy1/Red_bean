"""网格采样 + 颜色识别模块。

将上传的拼豆图纸图片按实际行列数划分网格，取每个格子的主导色（底色），
再与色库做 RGB 欧氏距离最近色匹配（有内容的格子总是匹配最近色，不留白；
空白格靠深色判定跳过）。

支持「网格外框」检测结果 (x0, y0, cell_w, cell_h)：图纸四周有白边，网格
并非从 (0,0) 开始，据此纠正采样位置；同时跳过没有内容的空白格子。
"""
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from .color_match import ArtkalPalette

# 深色判定的两个阈值（均可由前端滑杆调节）：
# - _TEXT_DARK：颜色深度阈值，灰度低于该值的像素算「深色像素」（可调 80~130）
# - _TEXT_MIN_PIXELS：格子中心深色像素数量阈值，达到该值判「有豆」（可调 0~10）
_TEXT_DARK = 130
_TEXT_MIN_PIXELS = 6

# 颜色匹配「模糊度」阈值：top1/top2 的 ΔE 差距 < 此值时判「分不清」，
# 交视觉模型读印在格子里的色号文字兜底（用户指定阈值 4）。
_AMBIGUITY_MARGIN = 4


def _cell_rect(x0: float, y0: float, cell_w: float, cell_h: float,
               r: int, c: int) -> Tuple[int, int, int, int]:
    """格子 (r, c) 的像素矩形 (left, top, right, bottom)，right/bottom 不含。"""
    left = int(round(x0 + c * cell_w))
    top = int(round(y0 + r * cell_h))
    right = int(round(x0 + (c + 1) * cell_w))
    bottom = int(round(y0 + (r + 1) * cell_h))
    return left, top, right, bottom


def _has_text(gray: np.ndarray, rect: Tuple[int, int, int, int],
              min_pixels: int = _TEXT_MIN_PIXELS,
              dark_threshold: int = _TEXT_DARK) -> bool:
    """判断格子中心区域是否有深色内容（有 = 有豆，空白格无内容跳过）。

    dark_threshold：颜色深度阈值，灰度 < 该值算深色像素。
    min_pixels：中心区域深色像素数量阈值，≥ 该值判有豆。
    """
    left, top, right, bottom = rect
    left = max(0, left)
    top = max(0, top)
    right = min(gray.shape[1], right)
    bottom = min(gray.shape[0], bottom)
    if right - left < 3 or bottom - top < 3:
        return False
    mw = max(1, (right - left) // 4)
    mh = max(1, (bottom - top) // 4)
    region = gray[top + mh: bottom - mh, left + mw: right - mw]
    return int((region < dark_threshold).sum()) >= min_pixels


def _dominant_color(rgb_cell: np.ndarray) -> np.ndarray:
    """取格子内部的主导颜色（众数），用于得到格子底色（豆色），忽略中央的色号文字。

    色号文字是少数像素（深色或白色），量化后取众数即可落在底色上。
    """
    flat = rgb_cell.reshape(-1, 3)
    if len(flat) == 0:
        return np.array([255, 255, 255], dtype=np.float64)
    # 量化到 16 级（每通道 16 档），把相近底色归并
    q = (flat // 16 * 16)
    # 找众数量化色
    keys = q[:, 0] * 65536 + q[:, 1] * 256 + q[:, 2]
    _, idx, counts = np.unique(keys, return_inverse=True, return_counts=True)
    dominant_key = np.argmax(counts)
    mask = idx == dominant_key
    return np.median(flat[mask], axis=0)


def _white_balance(rgb: np.ndarray) -> np.ndarray:
    """全图白平衡校准：用最亮的 1% 像素（通常是白底）反推全局偏色并校正。

    拍摄时的白平衡/灯光偏色（多为每通道的乘性增益）会让白底被染成非中性色，
    连带把米白(M4)/浅米黄(G16)这类浅色一起染偏，导致匹配时互相串色。
    用最亮像素反推每通道增益，还原真实色，再做色号匹配。

    无白底（最亮处仍偏暗）或已接近纯白时跳过校准，避免误伤正常图。
    """
    bright = rgb.mean(axis=2)
    thr = np.percentile(bright, 99)
    mask = bright >= thr
    ref = np.median(rgb[mask], axis=0)
    # 最亮处不够亮（图中无白底/白参考）→ 跳过
    if float(ref.mean()) < 180:
        return rgb
    # 已接近纯白 → 无需校准
    if np.all(ref > 250):
        return rgb
    gain = 255.0 / np.maximum(ref, 1.0)
    return np.clip(rgb * gain, 0.0, 255.0)


def sample_grid(image: Image.Image, rows: int, cols: int,
                box: Optional[Tuple[float, float, float, float]] = None,
                text_threshold: int = _TEXT_MIN_PIXELS,
                dark_threshold: int = _TEXT_DARK,
                ) -> Tuple[np.ndarray, np.ndarray]:
    """把图片划分为 rows x cols 网格，每格取主导颜色（底色）作为该格颜色。

    box = (x0, y0, cell_w, cell_h)：网格外框。为 None 时视为铺满整图。

    Returns:
        (rgb, has_text)：rgb 为 (rows, cols, 3) float64 RGB 数组（底色）；
        has_text 为 (rows, cols) bool，标记该格是否有深色内容（空白格为 False）。
    """
    rgb = np.asarray(image.convert("RGB"), dtype=np.float64)
    rgb = _white_balance(rgb)  # 全图白平衡校准，消除拍摄色偏（对浅色 M4/G16 尤其关键）
    gray = np.asarray(image.convert("L"))
    h, w = rgb.shape[:2]

    if box is not None:
        x0, y0, cell_w, cell_h = box
    else:
        x0, y0, cell_w, cell_h = 0.0, 0.0, w / cols, h / rows

    out = np.zeros((rows, cols, 3), dtype=np.float64)
    has_text = np.zeros((rows, cols), dtype=bool)

    for r in range(rows):
        for c in range(cols):
            rect = _cell_rect(x0, y0, cell_w, cell_h, r, c)
            left, top, right, bottom = rect
            left = max(0, left)
            top = max(0, top)
            right = min(w, right)
            bottom = min(h, bottom)
            has_text[r, c] = _has_text(gray, (left, top, right, bottom),
                                       text_threshold, dark_threshold)

            cell_wpx = right - left
            cell_hpx = bottom - top
            if cell_wpx < 3 or cell_hpx < 3:
                continue

            # 取格子内部（去掉外 2px 网格线）的主导颜色 = 底色
            inner_left = min(right, left + 2)
            inner_top = min(bottom, top + 2)
            inner_right = max(left, right - 2)
            inner_bottom = max(top, bottom - 2)
            inner = rgb[inner_top:inner_bottom, inner_left:inner_right]
            out[r, c] = _dominant_color(inner)
    return out, has_text


def recognize(image: Image.Image, palette: ArtkalPalette, canvas_size: int,
              rows: int, cols: int,
              allowed_codes: List[str],
              max_colors: Optional[int] = None,
              box: Optional[Tuple[float, float, float, float]] = None,
              text_threshold: int = _TEXT_MIN_PIXELS,
              dark_threshold: int = _TEXT_DARK,
              ) -> Tuple[List[List[Optional[str]]], List[Tuple[int, int]]]:
    """完整识别流程：采样 -> CIEDE2000 最近色匹配 -> 跳过空白格 -> (可选)限制颜色种类。

    实际图案 (rows x cols) 放在画布左上角，其余区域为 None (空白)。
    有深色内容的格子总是匹配到最近色号（不做阈值留白）；无深色内容的
    空白格直接置 None，不猜测颜色。

    text_threshold：深色像素数量阈值，格子中心深色像素数 ≥ 该值判有豆（0=全部识别不判空白）。
    dark_threshold：颜色深度阈值，灰度 < 该值的像素算深色像素。

    max_colors：自动识别结果的颜色种类数量上限。>0 时先映射到最近色，
    按出现频次取前 N 种，再重新映射，保证结果颜色种类不超过 N。

    Returns:
        (grid, ambiguous_cells)：
        grid 为 canvas_size x canvas_size 的网格，元素为色号 code 或 None；
        ambiguous_cells 为 [(r, c), ...] 颜色匹配「模糊」（top1/top2 太近，
        如米白/浅黄）需读字兜底的格子。
    """
    rgb, has_text = sample_grid(image, rows, cols, box=box,
                                text_threshold=text_threshold,
                                dark_threshold=dark_threshold)
    flat = rgb.reshape(-1, 3)
    text_flat = has_text.reshape(-1)

    if allowed_codes:
        idx = [palette.get_index_by_code(c) for c in allowed_codes]
        idx = [i for i in idx if i >= 0]
        allowed = np.array(idx, dtype=np.int32) if idx else None
    else:
        allowed = None  # 未预选则用全部色库

    if max_colors and max_colors > 0:
        # 只在「有内容」的格子上统计频次，避免空白格干扰颜色种类选择
        if text_flat.any():
            top_indices = palette.select_top_n_colors(
                flat[text_flat], max_colors, allowed_indices=allowed)
            allowed = top_indices

    # 总是匹配最近色（不做阈值留白）；空白格由 has_text（深色判定）跳过
    matched, margins = palette.find_closest_batch_with_margin(flat, allowed_indices=allowed)
    codes = [palette.colors[i]["code"] for i in matched]

    grid: List[List[Optional[str]]] = [[None] * canvas_size for _ in range(canvas_size)]
    ambiguous: List[Tuple[int, int]] = []
    for r in range(rows):
        base = r * cols
        for c in range(cols):
            if text_flat[base + c] and codes[base + c]:
                grid[r][c] = codes[base + c]
                # 颜色匹配「模糊」（top1/top2 太近）→ 标记交读字兜底
                if margins[base + c] < _AMBIGUITY_MARGIN:
                    ambiguous.append((r, c))
    return grid, ambiguous
