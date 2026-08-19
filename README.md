# 红豆泥 🫘

拼豆图案识别与编辑系统 —— 把拼豆图纸图片识别成可编辑的色号网格，辅助拼豆制作。

## 功能

- **颜色识别**：上传图纸 → 自动检测网格边界 → 每个格子取主导色 → 匹配 Mard 221 色库最近色号
- **空白格判定**：深色判定（阈值可调），未涂色的格子自动留空，不被误识别成颜色
- **画布编辑**：画笔 / 橡皮 / 取色 / 点击，支持 52×52 与 104×104 画布
- **单色高亮**：选中某色号后其余颜色灰化，该颜色所围成区域的**外边界**用橙色描边，便于拼豆时快速定位
- **坐标轴**：画布外围标注行号 / 列号
- **颜色统计与导出**：实时统计每种色号的数量，导出普通作品图 / 拼豆参考图
- **用户系统**：注册 / 登录（SQLite + PBKDF2 密码哈希 + HMAC 无状态 token）
- **移动端适配**：画笔/橡皮/取色下双指缩放画布，单指操作工具

## 复现

### 环境要求

- Python 3.10+
- 无 GPU 依赖

### 安装

```bash
git clone <仓库地址>
cd bead-studio
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 运行

```bash
uvicorn main:app --host 0.0.0.0 --port 30002
```

浏览器打开 <http://localhost:30002> ，注册登录后即可使用。

## 使用流程

1. 注册 / 登录
2. 上传拼豆图纸（图片）
3. 选择画布尺寸（52×52 / 104×104）
4. 填写实际图案的行列数
5. （可选）颜色预选、调整「深色判定阈值」/「颜色深度阈值」
6. 点击「开始识别」
7. 用画笔 / 橡皮人工修正，单色高亮辅助拼豆
8. 导出图片

## 技术栈

- 后端：Python + FastAPI
- 图像处理：Pillow + NumPy
- 颜色匹配：RGB 欧氏距离最近色（Mard 221 色库）
- 前端：原生 HTML + CSS + JavaScript
- 数据库：SQLite

## 目录结构

```
bead-studio/
├── main.py                  # FastAPI 入口（路由、用户、识别接口）
├── core/
│   ├── color_match.py       # 221 色库加载 + 颜色匹配
│   ├── grid_detect.py       # 网格内容区边界检测
│   └── grid_sampler.py      # 网格采样 + 颜色识别
├── data/
│   ├── artkal_m_series.json # 221 色库数据
│   └── artkal_presets.json  # 预设
├── static/
│   ├── app.js               # 前端逻辑
│   ├── style.css            # 样式
│   └── favicon.svg          # 图标
├── templates/
│   └── index.html           # 页面
└── requirements.txt
```

## 说明

- `data/secret.key` 与 `data/users.db` 在首次运行时自动生成，已加入 `.gitignore`，不会提交。
- 色号格式：系列字母 `A–H、M`（共 9 系，无 `I/J/K/L`）+ 1~2 位数字，例如 `A1`、`G16`、`M15`。
