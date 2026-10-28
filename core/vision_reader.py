"""视觉读色号文字：对颜色匹配「模糊」（top1/top2 太近）的格子，用视觉模型读印在格子里的色号。

方案：直接读「原图网格」（分块），比把格子重排成 montage 快 5~8 倍——
原网格有网格线、格子连续，gpt-5.6-sol 读得又快又准（~0.1s/格 vs montage 0.65s/格）。
模糊格多时整网格分块读；模糊格少时逐格读。
"""
import base64
import io
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from PIL import Image

from vision_config import API_KEY, BASE

MODEL = "deepseek-v4-flash-vision-exp"

_CODE_RE = re.compile(r"([A-HM])\s*(\d{1,2})")
_TOKEN_RE = re.compile(r"[A-HM]\d{1,2}|-")

_BLOCK = 26        # 分块大小（格）
_OVERLAP = 1       # 每块向外扩 1 格，避免块边界切到文字
_UPSCALE = 3       # 放大倍数

_SINGLE_PROMPT = (
    "这是拼豆图纸的一个格子，格子中央印着一个色号。"
    "色号格式 = 1 个大写字母(A/B/C/D/E/F/G/H/M) + 1~2 位数字（例：M4、G16、H18、A1）。"
    "请只读出这个格子里印的色号，只输出色号本身（例如 G16）。"
    "如果格子里没有印字，只输出一个减号 -。"
)


def _chat(messages, max_tokens=3000, timeout=240) -> Optional[str]:
    body = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()


def _img_b64(img: Image.Image, jpeg: bool = True) -> str:
    buf = io.BytesIO()
    if jpeg:
        img.save(buf, format="JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _crop_cell(image: Image.Image, x0, y0, cell_w, cell_h, r, c) -> Optional[Image.Image]:
    w, h = image.size
    left = max(0, int(round(x0 + c * cell_w)))
    top = max(0, int(round(y0 + r * cell_h)))
    right = min(w, int(round(x0 + (c + 1) * cell_w)))
    bottom = min(h, int(round(y0 + (r + 1) * cell_h)))
    if right - left < 4 or bottom - top < 4:
        return None
    cell = image.crop((left, top, right, bottom))
    return cell.resize((cell.width * _UPSCALE, cell.height * _UPSCALE), Image.LANCZOS)


def _parse_code(text: str) -> Optional[str]:
    m = re.fullmatch(r"([A-HM])(\d{1,2})", text)
    if m:
        return m.group(1) + m.group(2)
    m = _CODE_RE.search(text)
    if m:
        return m.group(1) + m.group(2)
    return None


def _read_single(cell: Image.Image) -> Optional[str]:
    try:
        content = _chat([{"role": "user", "content": [
            {"type": "text", "text": _SINGLE_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _img_b64(cell, jpeg=False)}},
        ]}], max_tokens=20)
    except Exception:
        return None
    return _parse_code(content or "")


def _read_grid_block(image: Image.Image, x0, y0, cell_w, cell_h,
                     rows: int, cols: int,
                     r0: int, r1: int, c0: int, c1: int) -> Dict[Tuple[int, int], str]:
    """读网格的一块 [r0:r1]×[c0:c1]，返回 {(r,c): code}（核心区域）。"""
    # 向外扩 1 格，避免块边界切文字
    er0 = max(0, r0 - _OVERLAP)
    er1 = min(rows, r1 + _OVERLAP)
    ec0 = max(0, c0 - _OVERLAP)
    ec1 = min(cols, c1 + _OVERLAP)

    w, h = image.size
    left = max(0, int(round(x0 + ec0 * cell_w)))
    top = max(0, int(round(y0 + er0 * cell_h)))
    right = min(w, int(round(x0 + ec1 * cell_w)))
    bottom = min(h, int(round(y0 + er1 * cell_h)))
    if right - left < 4 or bottom - top < 4:
        return {}

    block = image.crop((left, top, right, bottom))
    bw, bh = right - left, bottom - top
    block = block.resize((bw * _UPSCALE, bh * _UPSCALE), Image.LANCZOS)

    R = er1 - er0
    C = ec1 - ec0
    prompt = (
        f"这是拼豆图纸网格的一部分，共 {R} 行 {C} 列格子。"
        "每个格子里印着一个色号（格式：1个大写字母A-H或M + 1~2位数字，如 M4、G16、H18），"
        "没字的格子是空白。请从左上到右下，逐格读出每个格子的色号，"
        f"每行输出 {C} 个色号、用空格分隔，共 {R} 行，空白格输出 -。只输出矩阵，不要任何其他文字。"
    )
    try:
        content = _chat([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _img_b64(block)}},
        ]}], max_tokens=3000)
    except Exception:
        return {}

    tokens = _TOKEN_RE.findall(content or "")
    result: Dict[Tuple[int, int], str] = {}
    # 期望 R*C 个 token，按扩展网格映射，只写核心 [r0:r1]×[c0:c1]
    n = min(R * C, len(tokens))
    for i in range(n):
        tok = tokens[i]
        if tok == "-":
            continue
        gr = i // C
        gc = i % C
        r = er0 + gr
        c = ec0 + gc
        if r0 <= r < r1 and c0 <= c < c1:
            result[(r, c)] = tok
    return result


def read_cell_codes(image: Image.Image, box: Tuple[float, float, float, float],
                    cells: List[Tuple[int, int]], palette, rows: int, cols: int,
                    max_workers: int = 4) -> Dict[Tuple[int, int], str]:
    """读取模糊格子的色号文字。

    模糊格多时：把清晰格子涂白遮掉、只留模糊格，整网格分块读（快）；
    少时逐格读（省）。返回 {(r,c): code}（仅合法色号）。
    """
    if not cells:
        return {}

    x0, y0, cell_w, cell_h = box
    cells = sorted(cells)
    want = set(cells)
    results: Dict[Tuple[int, int], str] = {}

    # 模糊格多 → 整网格分块读（原图布局，模型读得最快）
    if len(cells) >= 40:
        blocks = []
        for r0 in range(0, rows, _BLOCK):
            for c0 in range(0, cols, _BLOCK):
                r1 = min(rows, r0 + _BLOCK)
                c1 = min(cols, c0 + _BLOCK)
                if any(r0 <= r < r1 and c0 <= c < c1 for (r, c) in cells):
                    blocks.append((r0, r1, c0, c1))

        def _run(b):
            return _read_grid_block(image, x0, y0, cell_w, cell_h, rows, cols, *b)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(_run, blocks):
                for rc, code in res.items():
                    if rc in want and palette.get_by_code(code):
                        results[rc] = code

    # 剩余的（没读到的）逐格兜底
    missing = [rc for rc in cells if rc not in results]
    if missing:
        def _single(rc):
            r, c = rc
            cell = _crop_cell(image, x0, y0, cell_w, cell_h, r, c)
            if cell is None:
                return None
            code = _read_single(cell)
            if code and palette.get_by_code(code):
                return rc, code
            return None

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(_single, missing):
                if res:
                    results[res[0]] = res[1]

    return results
