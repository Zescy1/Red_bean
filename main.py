"""红豆泥 - FastAPI 后端。

功能：用户注册/登录 (SQLite + PBKDF2 密码哈希 + HMAC 无状态 token)、
221 色拼豆色库、图片网格采样与颜色识别。
数据库仅保存用户信息，不保存作品/图片数据。
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
from typing import List, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from core.color_match import ArtkalPalette
from core.grid_detect import detect_content_box
from core.grid_sampler import recognize
from core.vision_reader import read_cell_codes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "users.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI(title="红豆泥", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

palette = ArtkalPalette()


# ---------- 密钥 (用于 HMAC token 签名，持久化避免重启失效) ----------
def _get_secret() -> bytes:
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "rb") as f:
            return f.read()
    s = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(s)
    return s


SECRET = _get_secret()


# ---------- 数据库 (仅 user 表) ----------
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " username TEXT UNIQUE NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS work ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " username TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " canvas_size INTEGER NOT NULL,"
            " rows INTEGER NOT NULL,"
            " cols INTEGER NOT NULL,"
            " grid TEXT NOT NULL,"
            " created_at INTEGER NOT NULL)"
        )
        # 用户名唯一性：表上的 UNIQUE 是大小写敏感的（test1 / Test1 会被当成两个人），
        # 再加一个大小写不敏感的唯一索引，确保用户名（登录 key）真正唯一。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_username_nocase "
            "ON user (username COLLATE NOCASE)"
        )


_init_db()


# ---------- 密码哈希 (PBKDF2-SHA256 + 随机盐) ----------
def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, dk_hex = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------- 无状态 token (HMAC 签名，不落库) ----------
def _make_token(username: str, ttl: int = 7 * 24 * 3600) -> str:
    payload = f"{username}:{int(time.time()) + ttl}"
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _parse_token(token: str) -> Optional[str]:
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expect = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        username, exp = payload.rsplit(":", 1)
    except ValueError:
        return None
    return username if int(exp) >= time.time() else None


def _require_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    username = _parse_token(authorization[7:].strip())
    if not username:
        raise HTTPException(401, "登录已失效，请重新登录")
    return username


# ---------- 页面 ----------
def _asset_version() -> str:
    """前端静态资源版本号（取 app.js/style.css 最新修改时间）。

    /static 由 StaticFiles 直接挂载，浏览器（尤其手机/微信内置浏览器）会强缓存
    app.js、style.css，导致改了前端也看不到效果。给引用加上 ?v=<mtime> 后，
    文件一改版本号就变，浏览器必然重新拉取。
    """
    latest = 0.0
    for name in ("app.js", "style.css"):
        path = os.path.join(BASE_DIR, "static", name)
        try:
            latest = max(latest, os.path.getmtime(path))
        except OSError:
            pass
    return str(int(latest))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    resp = templates.TemplateResponse(request, "index.html", {"ver": _asset_version()})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ---------- 用户 ----------
# 智能识别（视觉模型读字兜底）白名单：只有这些用户可用，其余用户强制走直接识别。
# 读字兜底会消耗反代 API 额度，故限制授权范围。
_SMART_USERS = {"test1"}


def _can_smart(username: str) -> bool:
    return username.lower() in _SMART_USERS


@app.post("/api/register")
async def register(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not 3 <= len(username) <= 32:
        raise HTTPException(400, "用户名需 3-32 个字符")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    # 用户名唯一性检查（大小写不敏感，避免 test1 / Test1 视为两个账号）
    with _get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM user WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    if exists:
        raise HTTPException(409, "用户名已存在")
    try:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO user (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, _hash_password(password), int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "用户名已存在")
    return {"token": _make_token(username), "username": username,
            "smart": _can_smart(username)}


@app.post("/api/login")
async def login(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    # 大小写不敏感查找，但一律以库中记录的用户名为准（保证 token / 作品归属 key 唯一）
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    canonical = row["username"]
    return {"token": _make_token(canonical), "username": canonical,
            "smart": _can_smart(canonical)}


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    return {"username": username, "smart": _can_smart(username)}


# ---------- 作品（个人缓存） ----------
def _work_summary(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "canvas_size": row["canvas_size"],
        "rows": row["rows"],
        "cols": row["cols"],
        "created_at": row["created_at"],
    }


@app.post("/api/works")
async def save_work(data: dict, authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    name = (data.get("name") or "").strip() or "未命名作品"
    canvas_size = data.get("canvas_size")
    rows = data.get("rows")
    cols = data.get("cols")
    grid = data.get("grid")
    if canvas_size not in (52, 104):
        raise HTTPException(400, "画布尺寸只能是 52 或 104")
    if not isinstance(rows, int) or not isinstance(cols, int) \
            or not (1 <= rows <= canvas_size and 1 <= cols <= canvas_size):
        raise HTTPException(400, "行列数不合法")
    if not isinstance(grid, list) or len(grid) != canvas_size:
        raise HTTPException(400, "网格数据格式错误")
    grid_json = json.dumps(grid, ensure_ascii=False)
    if len(grid_json) > 5_000_000:
        raise HTTPException(400, "作品数据过大")
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO work (username, name, canvas_size, rows, cols, grid, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, name[:64], canvas_size, rows, cols, grid_json, int(time.time())),
        )
        wid = cur.lastrowid
    return {"id": wid}


@app.get("/api/works")
async def list_works(authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, canvas_size, rows, cols, created_at FROM work "
            "WHERE username = ? ORDER BY created_at DESC, id DESC",
            (username,),
        ).fetchall()
    return {"works": [_work_summary(r) for r in rows]}


@app.get("/api/works/{wid}")
async def get_work(wid: int, authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM work WHERE id = ? AND username = ?", (wid, username)
        ).fetchone()
    if not row:
        raise HTTPException(404, "作品不存在")
    return {**_work_summary(row), "grid": json.loads(row["grid"])}


@app.patch("/api/works/{wid}")
async def rename_work(wid: int, data: dict, authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "作品名不能为空")
    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE work SET name = ? WHERE id = ? AND username = ?",
            (name[:64], wid, username),
        )
    if cur.rowcount == 0:
        raise HTTPException(404, "作品不存在")
    return {"ok": True, "name": name[:64]}


@app.delete("/api/works/{wid}")
async def delete_work(wid: int, authorization: Optional[str] = Header(None)):
    username = _require_user(authorization)
    with _get_db() as conn:
        cur = conn.execute(
            "DELETE FROM work WHERE id = ? AND username = ?", (wid, username)
        )
    if cur.rowcount == 0:
        raise HTTPException(404, "作品不存在")
    return {"ok": True}


# ---------- 作品社区（所有人的作品，公开可见） ----------
@app.get("/api/community")
async def community_works(limit: int = 60, offset: int = 0,
                          authorization: Optional[str] = Header(None)):
    """社区作品列表：所有用户的作品，按时间倒序。只返回名字/作者/大小。"""
    _require_user(authorization)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, name, canvas_size, rows, cols "
            "FROM work ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM work").fetchone()["n"]
    works = [{
        "id": r["id"],
        "author": r["username"],
        "name": r["name"],
        "canvas_size": r["canvas_size"],
        "rows": r["rows"],
        "cols": r["cols"],
    } for r in rows]
    return {"works": works, "total": total, "offset": offset, "limit": limit}


@app.get("/api/community/{wid}")
async def community_work_detail(wid: int, authorization: Optional[str] = Header(None)):
    """载入社区里任意作品到工作台（公开可读）。"""
    _require_user(authorization)
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM work WHERE id = ?", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, "作品不存在")
    return {
        "id": row["id"],
        "author": row["username"],
        "name": row["name"],
        "canvas_size": row["canvas_size"],
        "rows": row["rows"],
        "cols": row["cols"],
        "grid": json.loads(row["grid"]),
    }


@app.get("/api/works/{wid}/thumb")
async def work_thumb(wid: int):
    """作品缩略图 PNG（公开，供社区/作品列表 <img> 直接引用）。

    按 grid 里实际有内容的格子边界（bounding box）渲染，不依赖保存时记录的
    rows/cols，保证作品内容完整展示、不被裁切。
    """
    with _get_db() as conn:
        row = conn.execute("SELECT grid FROM work WHERE id = ?", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, "作品不存在")
    try:
        grid = json.loads(row["grid"])
    except Exception:  # noqa: BLE001
        raise HTTPException(500, "作品数据损坏")

    # 有内容格子的边界
    r0, r1, c0, c1 = len(grid), -1, len(grid[0]) if grid else 0, -1
    for r, line in enumerate(grid):
        for c, code in enumerate(line):
            if code:
                if r < r0: r0 = r
                if r > r1: r1 = r
                if c < c0: c0 = c
                if c > c1: c1 = c
    if r1 < 0:  # 空作品
        img = Image.new("RGB", (16, 16), (255, 255, 255))
    else:
        h, w = r1 - r0 + 1, c1 - c0 + 1
        cell = max(1, min(8, 320 // max(h, w)))
        img = Image.new("RGB", (w * cell, h * cell), (255, 255, 255))
        px = img.load()
        for r in range(r0, r1 + 1):
            line = grid[r]
            for c in range(c0, min(c1 + 1, len(line))):
                code = line[c]
                if not code:
                    continue
                color = palette.get_by_code(code)
                if not color:
                    continue
                rgb = tuple(color["rgb"])
                bx, by = (c - c0) * cell, (r - r0) * cell
                for dy in range(cell):
                    for dx in range(cell):
                        px[bx + dx, by + dy] = rgb
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------- 色库 ----------
_SERIES_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "M"]


@app.get("/api/palette")
async def get_palette():
    colors = [
        {"code": c["code"], "series": c["series"], "hex": c["hex"], "rgb": c["rgb"]}
        for c in palette.colors
    ]
    series = sorted(
        {c["series"] for c in colors},
        key=lambda s: _SERIES_ORDER.index(s) if s in _SERIES_ORDER else 99,
    )
    return {
        "colors": colors,
        "series": series,
    }


# ---------- 识别 ----------
@app.post("/api/recognize")
async def recognize_pattern(
    file: UploadFile = File(...),
    canvas_size: int = Form(52),
    rows: Optional[int] = Form(None),
    cols: Optional[int] = Form(None),
    codes: str = Form("[]"),
    max_colors: int = Form(0),
    text_threshold: int = Form(6),
    dark_threshold: int = Form(130),
    direct: int = Form(0),
    authorization: Optional[str] = Header(None),
):
    username = _require_user(authorization)
    # 智能识别（读字兜底）仅授权用户可用，其余强制走直接识别（服务端强制，不信前端）
    if not _can_smart(username):
        direct = 1

    if canvas_size not in (52, 104):
        raise HTTPException(400, "画布尺寸只能是 52 或 104")
    if rows is None or cols is None or not (1 <= rows <= canvas_size and 1 <= cols <= canvas_size):
        raise HTTPException(400, f"实际图案行列数需在 1~{canvas_size} 之间，且行和列需同时填写")
    if max_colors < 0:
        raise HTTPException(400, "颜色种类上限需为非负整数")
    if not (0 <= text_threshold <= 10):
        raise HTTPException(400, "深色判定阈值需在 0~10 之间")
    if not (80 <= dark_threshold <= 220):
        raise HTTPException(400, "颜色深度阈值需在 80~220 之间")
    try:
        allowed_codes: List[str] = json.loads(codes or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "颜色预选参数格式错误")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "请上传图片文件")
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:
        raise HTTPException(400, "图片超过 20MB")
    try:
        image = Image.open(io.BytesIO(contents))
        image.load()
    except Exception:
        raise HTTPException(400, "无法解析图片")

    # 检测网格内容区边界（跳过四周白边）：用非白像素检测 detect_content_box。
    # 暗线检测 detect_grid_box 对浅灰网格线失效，且会把深色图案中心误当网格原点
    # （实测 104 深色中心图 x0/y0 被误判为 519），导致图片被"变窄/偏移"后读取。
    box_info = detect_content_box(image, rows, cols)
    box = None
    if box_info is not None:
        box = (box_info["x0"], box_info["y0"], box_info["cell_w"], box_info["cell_h"])

    try:
        grid, ambiguous = recognize(image, palette, canvas_size, rows, cols,
                                    allowed_codes, max_colors=max_colors, box=box,
                                    text_threshold=text_threshold,
                                    dark_threshold=dark_threshold)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"识别失败: {e}")

    # 颜色匹配「模糊」的格子（米白/浅黄等白色系）→ 视觉模型读印的色号文字兜底。
    # direct=1 时为「直接识别」：跳过读字兜底，只按图纸颜色匹配（快、不依赖大模型）。
    vision_read = 0
    vision_error = None
    if not direct and ambiguous and box is not None:
        try:
            read = read_cell_codes(image, box, ambiguous, palette, rows, cols)
            for (r, c), code in read.items():
                grid[r][c] = code
                vision_read += 1
        except Exception as e:  # noqa: BLE001
            vision_error = str(e)
            # 打印到 journalctl，避免 key 失效/网络故障被静默吞掉、无从排查
            print(f"[vision] 读字兜底失败（{len(ambiguous)} 个模糊格）: {e}", flush=True)

    return {
        "canvas_size": canvas_size,
        "rows": rows,
        "cols": cols,
        "grid": grid,
        "detected": box_info,
        "ambiguous_count": len(ambiguous),
        "vision_read": vision_read,
        "vision_error": vision_error,
        "direct": bool(direct),
    }
