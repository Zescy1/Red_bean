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
from fastapi.responses import HTMLResponse
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
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---------- 用户 ----------
@app.post("/api/register")
async def register(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not 3 <= len(username) <= 32:
        raise HTTPException(400, "用户名需 3-32 个字符")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    try:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO user (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, _hash_password(password), int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "用户名已存在")
    return {"token": _make_token(username), "username": username}


@app.post("/api/login")
async def login(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM user WHERE username = ?", (username,)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": _make_token(username), "username": username}


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None)):
    return {"username": _require_user(authorization)}


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
    authorization: Optional[str] = Header(None),
):
    _require_user(authorization)

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

    # 颜色匹配「模糊」的格子（米白/浅黄等白色系）→ 视觉模型读印的色号文字兜底
    vision_read = 0
    if ambiguous and box is not None:
        try:
            read = read_cell_codes(image, box, ambiguous, palette, rows, cols)
            for (r, c), code in read.items():
                grid[r][c] = code
                vision_read += 1
        except Exception:  # noqa: BLE001
            pass  # 读字失败则保留颜色匹配结果

    return {
        "canvas_size": canvas_size,
        "rows": rows,
        "cols": cols,
        "grid": grid,
        "detected": box_info,
        "ambiguous_count": len(ambiguous),
        "vision_read": vision_read,
    }
