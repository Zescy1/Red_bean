"""网格检测：自动定位拼豆图纸的网格外框（原点、格子尺寸）。

图纸四周常有白边/灰边，用户裁切无法百分百准确，偏移不定；不同图片分辨率不同，
格子尺寸也不同（实测 19px ~ 25px）。

核心思路（匹配「检测灰框→裁掉→映射 N×N」的用户预期）：
  1. 网格线 = 全高/全宽的暗线（暗像素占比高），聚类成线
  2. 首条暗线 = 网格左上角原点，末条暗线 = 右/下边界
  3. 格子尺寸 = (末线 - 首线) / (行列数 - 1)，用用户的行列数直接算出精确格子尺寸，
     不再依赖自相关（自相关会被豆子纹理的谐波带偏，如 19px 网格被读成 17px）
"""
from typing import List, Optional

import numpy as np


def _cluster(idx: np.ndarray) -> List[int]:
    """把相邻的暗线索引聚成线，返回线中心位置。"""
    if len(idx) == 0:
        return []
    out = []
    s = p = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - p <= 3:
            p = i
        else:
            out.append((s + p) // 2)
            s = p = i
    out.append((s + p) // 2)
    return out


def _period(prof: np.ndarray) -> int:
    """自相关求主周期（回退用）。"""
    p = prof - prof.mean()
    ac = np.correlate(p, p, "full")[len(p) - 1:]
    ac[:6] = -1e9
    return int(np.argmax(ac[6:60]) + 6)


def detect_grid_box(image, rows: int, cols: int) -> Optional[dict]:
    """检测网格外框，返回 dict(x0, y0, cell_w, cell_h) 或 None。

    rows/cols：用户设置的行列数，用于从网格跨度精确反推格子尺寸。
    """
    try:
        gray = np.asarray(image.convert("L"))
    except Exception:
        return None
    H, W = gray.shape
    if W < 30 or H < 30 or rows < 2 or cols < 2:
        return None

    dark = gray < 130
    col_dark = dark.mean(axis=0)
    row_dark = dark.mean(axis=1)

    vlines = _cluster(np.where(col_dark > 0.2)[0])
    hlines = _cluster(np.where(row_dark > 0.2)[0])

    if len(vlines) >= 3 and len(hlines) >= 3:
        x0, x1 = vlines[0], vlines[-1]
        y0, y1 = hlines[0], hlines[-1]
        cell_w = (x1 - x0) / (cols - 1)
        cell_h = (y1 - y0) / (rows - 1)
        if cell_w < 3 or cell_h < 3 or cell_w > 200 or cell_h > 200:
            return None
        return {
            "x0": x0, "y0": y0,
            "cell_w": round(cell_w, 2), "cell_h": round(cell_h, 2),
        }

    # 回退：自相关求格子尺寸 + 首条暗线定位原点
    bg = float(np.percentile(gray, 95))
    colprof = gray.mean(axis=0)
    rowprof = gray.mean(axis=1)
    cw = _period(colprof)
    ch = _period(rowprof)
    x0 = int(vlines[0]) if vlines else next((i for i in range(W) if colprof[i] < bg - 8), 0)
    y0 = int(hlines[0]) if hlines else next((i for i in range(H) if rowprof[i] < bg - 8), 0)
    return {"x0": x0, "y0": y0, "cell_w": cw, "cell_h": ch}


def detect_content_box(image, rows: int, cols: int, nonwhite_ratio: float = 0.003) -> Optional[dict]:
    """用「非白像素」检测网格内容区边界（跳过四周白边/纯色留白）。

    白边是纯白/近纯白（灰度 >= 250），网格区域有网格线、色号文字、底色（< 250）。
    对每一列/行统计非白像素比例，比例 > nonwhite_ratio 即算内容。相比活动度（梯度），
    非白像素对浅灰线、浅色格子同样敏感，不会把浅色内容误判成白边。

    返回 dict(x0, y0, cell_w, cell_h) 或 None。
    """
    try:
        gray = np.asarray(image.convert("L")).astype(np.float64)
    except Exception:
        return None
    H, W = gray.shape
    if W < 30 or H < 30 or rows < 2 or cols < 2:
        return None

    nonwhite = gray < 250
    col_has = nonwhite.mean(axis=0)  # 每列非白像素比例（网格线/色号穿过的列 > 0）
    row_has = nonwhite.mean(axis=1)  # 每行非白像素比例

    def find_range(prof: np.ndarray):
        idx = np.where(prof > nonwhite_ratio)[0]
        if len(idx) == 0:
            return 0, len(prof)
        return int(idx[0]), int(idx[-1]) + 1

    x0, x1 = find_range(col_has)
    y0, y1 = find_range(row_has)

    cell_w = (x1 - x0) / cols
    cell_h = (y1 - y0) / rows
    if cell_w < 3 or cell_h < 3 or cell_w > 200 or cell_h > 200:
        return None
    return {"x0": x0, "y0": y0, "cell_w": round(cell_w, 2), "cell_h": round(cell_h, 2)}
