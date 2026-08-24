/* 拼豆图案识别与编辑系统 - 前端逻辑 */
'use strict';

// ---------- 状态 ----------
const state = {
  token: localStorage.getItem('pb_token') || null,
  username: localStorage.getItem('pb_username') || null,
  palette: null,          // {colors, series}
  codeToColor: {},        // code -> {hex, series, rgb}
  seriesColors: {},       // series -> [codes]
  selectedCodes: new Set(),
  currentSeries: null,
  canvasSize: 52,
  grid: null,             // canvasSize x canvasSize, code | null
  tool: 'view',           // 'brush' | 'eraser' | 'view' | 'picker'（默认点击查看）
  activeCode: null,       // 画笔颜色 + 高亮目标
  cellSize: 12,
  imageFile: null,
  authMode: 'login',
  maxColors: 0,           // 自动识别颜色种类上限，0=不限制
  lastRecognizedCodes: [], // 本次识别传入的预选色快照（用于统计含 0 颗显示）
  recognizeMode: 'smart', // 'smart' 智能识别（含大模型读字兜底）| 'direct' 直接识别（纯颜色匹配）
};

const $ = (id) => document.getElementById(id);

// ---------- 工具函数 ----------
function hexToRgb(hex) {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function grayOut(hex) {  // 高亮时其余颜色转灰
  const [r, g, b] = hexToRgb(hex);
  const v = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
  return `rgb(${v},${v},${v})`;
}
function textColorFor(hex) {  // 深色底色色号文字用白色，浅色用黑色（固定阈值 180）
  const [r, g, b] = hexToRgb(hex);
  const lum = 0.299 * r + 0.587 * g + 0.114 * b;
  const depth = 255 - lum;  // 深度：越大越深
  return depth > 180 ? '#ffffff' : '#000000';
}
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
  return data;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function isMobile() { return window.matchMedia('(max-width: 1024px)').matches; }
function toggleSidebar() { $('sidebar').classList.toggle('collapsed'); }
function collapseSidebar() { $('sidebar').classList.add('collapsed'); }
function computeCellSize(n) {
  // 移动端：以 52 为基准按屏宽缩放，104 画布格子与 52 一样大（104 会横向溢出、可滚动）
  if (isMobile()) {
    const avail = window.innerWidth - 40;
    return Math.max(2, Math.floor(avail / 52));
  }
  // 桌面端：52 和 104 画布格子一样大
  return 12;
}

// ---------- 抽屉（移动端左右滑出面板）----------
function openDrawer(which) {
  $(which).classList.add('open');
  $('drawer-mask').classList.add('show');
}
function closeDrawers() {
  $('sidebar').classList.remove('open');
  $('stats').classList.remove('open');
  $('drawer-mask').classList.remove('show');
}
function toggleDrawer(which) {
  const el = $(which);
  const open = el.classList.toggle('open');
  $('drawer-mask').classList.toggle('show', open);
}

// ---------- 认证 ----------
function openAuth(mode) {
  state.authMode = mode;
  $('tab-login').classList.toggle('active', mode === 'login');
  $('tab-register').classList.toggle('active', mode === 'register');
  $('btn-auth-submit').textContent = mode === 'login' ? '登录' : '注册';
  $('auth-msg').textContent = '';
  $('auth-gate').style.display = 'flex';
  $('layout').style.display = 'none';
  $('user-area').style.display = 'none';
}
function applyAuth(token, username) {
  state.token = token; state.username = username;
  localStorage.setItem('pb_token', token);
  localStorage.setItem('pb_username', username);
  $('user-info').textContent = `👤 ${username}`;
  $('auth-gate').style.display = 'none';
  $('layout').style.display = 'grid';
  $('user-area').style.display = 'flex';
}
function logout() {
  state.token = null; state.username = null;
  localStorage.removeItem('pb_token'); localStorage.removeItem('pb_username');
  state.grid = null; state.imageFile = null;
  $('preview-wrap').style.display = 'none';
  renderCanvas(); renderColorBar(); renderStats(); updateBeadCount();
  openAuth('login');
}
async function submitAuth() {
  const username = $('auth-username').value.trim();
  const password = $('auth-password').value;
  const msg = $('auth-msg');
  msg.textContent = '';
  try {
    const path = state.authMode === 'login' ? '/api/login' : '/api/register';
    const data = await api(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    applyAuth(data.token, data.username);
  } catch (e) { msg.textContent = e.message; }
}

// ---------- 色库 ----------
async function loadPalette() {
  const data = await fetch('/api/palette').then(r => r.json());
  state.palette = data;
  state.codeToColor = {};
  state.seriesColors = {};
  for (const c of data.colors) {
    state.codeToColor[c.code] = c;
    (state.seriesColors[c.series] = state.seriesColors[c.series] || []).push(c.code);
  }
  renderSeriesTabs();
}
function renderSeriesTabs() {
  const wrap = $('series-tabs');
  wrap.innerHTML = '';
  for (const s of state.palette.series) {
    const b = document.createElement('button');
    b.textContent = s;
    b.onclick = () => {
      state.currentSeries = s;
      wrap.querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
      renderColorPicker();
    };
    wrap.appendChild(b);
  }
  state.currentSeries = state.palette.series[0];
  wrap.querySelector('button').classList.add('active');
  renderColorPicker();
}
function renderColorPicker() {
  const wrap = $('color-picker');
  wrap.innerHTML = '';
  const codes = state.seriesColors[state.currentSeries] || [];
  for (const code of codes) {
    const c = state.codeToColor[code];
    const sw = document.createElement('div');
    sw.className = 'swatch' + (state.selectedCodes.has(code) ? ' selected' : '');
    sw.style.background = c.hex;
    sw.style.color = textColorFor(c.hex);
    sw.textContent = code;
    sw.title = `${code} ${c.hex}`;
    sw.onclick = () => {
      if (state.selectedCodes.has(code)) state.selectedCodes.delete(code);
      else state.selectedCodes.add(code);
      sw.classList.toggle('selected');
      updateSelCount();
    };
    wrap.appendChild(sw);
  }
}
function updateSelCount() { $('sel-count').textContent = `已选 ${state.selectedCodes.size} 色`; }

// ---------- 上传 ----------
function onFileChange(e) {
  const f = e.target.files[0];
  if (!f) return;
  state.imageFile = f;
  const reader = new FileReader();
  reader.onload = (ev) => {
    $('preview-img').src = ev.target.result;
    $('preview-wrap').style.display = '';
  };
  reader.readAsDataURL(f);
}

// ---------- 识别 ----------
async function doRecognize() {
  if (!state.token) { openAuth('login'); return; }
  if (!state.imageFile) { alert('请先上传图纸图片'); return; }
  const rows = parseInt($('rows').value, 10);
  const cols = parseInt($('cols').value, 10);
  if (!(rows >= 1 && cols >= 1 && rows <= state.canvasSize && cols <= state.canvasSize)) {
    alert(`实际图案行列数需在 1~${state.canvasSize} 之间`); return;
  }
  const maxColors = parseInt($('max-colors').value, 10) || 0;
  const fd = new FormData();
  fd.append('file', state.imageFile);
  fd.append('canvas_size', state.canvasSize);
  fd.append('rows', rows);
  fd.append('cols', cols);
  fd.append('max_colors', maxColors);
  fd.append('text_threshold', parseInt($('text-threshold').value, 10) || 0);
  fd.append('dark_threshold', parseInt($('dark-threshold').value, 10) || 130);
  fd.append('direct', state.recognizeMode === 'direct' ? '1' : '0');
  fd.append('codes', JSON.stringify([...state.selectedCodes]));

  const btn = $('btn-recognize');
  btn.disabled = true;
  btn.textContent = '识别中…';
  try {
    const data = await api('/api/recognize', { method: 'POST', body: fd });
    state.canvasSize = data.canvas_size;
    state.grid = data.grid;
    state.activeCode = null;
    state.lastRecognizedCodes = [...state.selectedCodes];  // 预选色快照
    state.cellSize = computeCellSize(data.canvas_size);
    renderCanvas(); renderColorBar(); renderStats(); updateBeadCount();
    if (isMobile()) closeDrawers();
  } catch (e) { alert(e.message); }
  finally { btn.disabled = false; btn.textContent = '🎯 开始识别'; }
}

// ---------- 画布渲染 ----------
const AXIS = 20;  // 画布外围坐标轴（上方列号 / 左侧行号）宽度

function majorLines(n) {
  // 104: 2 | 10x10 | 2 的粗线分段边界；52: 1 | 5x10 | 1
  if (n === 104) return [2, 12, 22, 32, 42, 52, 62, 72, 82, 92, 102];
  if (n === 52) return [1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 51];
  return [];
}
function renderCanvas() {
  const n = state.canvasSize;
  const canvas = $('canvas');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = (n * state.cellSize + AXIS) * dpr;
  canvas.height = (n * state.cellSize + AXIS) * dpr;
  canvas.style.width = (n * state.cellSize + AXIS) + 'px';
  canvas.style.height = (n * state.cellSize + AXIS) + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const s = state.cellSize;
  ctx.fillStyle = '#fafafa'; ctx.fillRect(0, 0, n * s + AXIS, n * s + AXIS);

  // 外围坐标轴：上方列号、左侧行号（格子小时降频，避免数字重叠）
  ctx.fillStyle = '#333';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '9px sans-serif';
  const step = s >= 9 ? 5 : 10;
  for (let i = 0; i < n; i++) {
    if (i % step === 0 || i === n - 1) {
      const center = AXIS + i * s + s / 2;
      ctx.fillText(i + 1, center, AXIS / 2);   // 列号（横轴，画布上方）
      ctx.fillText(i + 1, AXIS / 2, center);   // 行号（纵轴，画布左侧）
    }
  }

  // 格子区域整体向右下平移 AXIS，后续绘制坐标仍用 c*s / r*s 原样
  ctx.save();
  ctx.translate(AXIS, AXIS);

  // 格子颜色（选中色号时：非高亮格转浅黑灰化，高亮格保持原色）
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      const code = state.grid ? state.grid[r][c] : null;
      const x = c * s, y = r * s;
      if (code) {
        const hex = state.codeToColor[code].hex;
        ctx.fillStyle = (state.activeCode && code !== state.activeCode) ? grayOut(hex) : hex;
      } else {
        ctx.fillStyle = '#ffffff';
      }
      ctx.fillRect(x, y, s, s);
    }
  }

  // 细网格线（内部小方块细间隔）
  if (s >= 4) {
    ctx.strokeStyle = '#b9bec7';  // 加深颜色，保证 0.5px 细线仍可见
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    for (let i = 1; i < n; i++) {
      ctx.moveTo(i * s + 0.5, 0.5); ctx.lineTo(i * s + 0.5, n * s - 0.5);
      ctx.moveTo(0.5, i * s + 0.5); ctx.lineTo(n * s - 0.5, i * s + 0.5);
    }
    ctx.stroke();
  }

  // 色号文字（居中；高亮模式下只显示选中颜色的文字，其他转灰不显示文字）
  // 文字颜色按格子底色自适应：深色(深度>阈值)用白字，浅色用黑字
  if (state.grid) {
    ctx.font = '2.25px sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        const code = state.grid[r][c];
        if (!code) continue;
        // 高亮模式：只显示被选中颜色的文字
        if (state.activeCode && code !== state.activeCode) continue;
        ctx.fillStyle = textColorFor(state.codeToColor[code].hex);
        // 手动按文字实际宽度居中，并微调（右移0.5px）
        const tw = ctx.measureText(code).width;
        ctx.fillText(code, c * s + (s - tw) / 2 + 0.5, r * s + s / 2);
      }
    }
  }

  // 高亮：仅靠「高亮格原色 + 其他格灰化」区分，不额外画框


  // 粗线（分段边界 + 外边框）
  const majors = majorLines(n);
  if (majors.length && s >= 3) {
    ctx.strokeStyle = '#5b6472';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    for (const i of majors) {
      ctx.moveTo(i * s, 0); ctx.lineTo(i * s, n * s);
      ctx.moveTo(0, i * s); ctx.lineTo(n * s, i * s);
    }
    ctx.stroke();
    ctx.strokeRect(1, 1, n * s - 2, n * s - 2);
  }

  // 颜色描边：高亮某颜色时，用橙色描出该颜色所围成的外边界（线宽与粗线一致）
  if (state.activeCode && state.grid) {
    const cells = new Set();
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (state.grid[r][c] === state.activeCode) cells.add(r + ',' + c);
      }
    }
    if (cells.size) {
      ctx.strokeStyle = '#ff9500';
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      for (const key of cells) {
        const p = key.split(',');
        const r = +p[0], c = +p[1];
        // 上方不是同色 → 画上边
        if (!cells.has((r - 1) + ',' + c)) {
          ctx.moveTo(c * s, r * s); ctx.lineTo((c + 1) * s, r * s);
        }
        // 下方
        if (!cells.has((r + 1) + ',' + c)) {
          ctx.moveTo(c * s, (r + 1) * s); ctx.lineTo((c + 1) * s, (r + 1) * s);
        }
        // 左方
        if (!cells.has(r + ',' + (c - 1))) {
          ctx.moveTo(c * s, r * s); ctx.lineTo(c * s, (r + 1) * s);
        }
        // 右方
        if (!cells.has(r + ',' + (c + 1))) {
          ctx.moveTo((c + 1) * s, r * s); ctx.lineTo((c + 1) * s, (r + 1) * s);
        }
      }
      ctx.stroke();
    }
  }

  ctx.restore();
}

// 画布坐标 -> 格子
function cellAt(e) {
  const rect = $('canvas').getBoundingClientRect();
  const x = e.clientX - rect.left - AXIS, y = e.clientY - rect.top - AXIS;
  const col = Math.floor(x / state.cellSize);
  const row = Math.floor(y / state.cellSize);
  if (row < 0 || col < 0 || row >= state.canvasSize || col >= state.canvasSize) return null;
  return { row, col };
}

function paintAt(e) {
  if (!state.grid) return;
  const p = cellAt(e);
  if (!p) return;
  if (state.tool === 'view') {
    return; // 点击模式：纯查看，不擦除、不改色、不取色
  }
  if (state.tool === 'picker') {
    // 取色器：吸取该格颜色到画笔，不修改色块
    state.activeCode = state.grid[p.row][p.col];
    setTool('view');
  } else if (state.tool === 'eraser') {
    state.grid[p.row][p.col] = null;
  } else {
    state.grid[p.row][p.col] = state.activeCode;
  }
  renderCanvas(); renderColorBar(); renderStats(); updateBeadCount();
}

// ---------- 工具栏 ----------
function setTool(tool) {
  state.tool = tool;
  $('tool-brush').classList.toggle('active', tool === 'brush');
  $('tool-eraser').classList.toggle('active', tool === 'eraser');
  $('tool-view').classList.toggle('active', tool === 'view');
  $('tool-picker').classList.toggle('active', tool === 'picker');
  // 点击模式(view)：允许浏览器单指滚动，保持和原来一样；
  // 画笔/橡皮/取色：拦截触摸，单指操作工具、双指捏合缩放
  $('canvas').style.touchAction = tool === 'view' ? 'auto' : 'none';
}

// ---------- 颜色栏 + 统计 ----------
function computeCounts() {
  const counts = {};
  let total = 0;
  if (state.grid) {
    for (const row of state.grid) for (const code of row) {
      if (code) { counts[code] = (counts[code] || 0) + 1; total++; }
    }
  }
  return { counts, total };
}
function renderColorBar() {
  const bar = $('color-bar');
  bar.innerHTML = '';
  if (!state.grid) { bar.innerHTML = '<span class="muted">识别后这里显示使用中的颜色</span>'; return; }
  const { counts } = computeCounts();
  const codes = Object.keys(counts);
  if (!codes.length) { bar.innerHTML = '<span class="muted">画布为空</span>'; return; }
  for (const code of codes) {
    const c = state.codeToColor[code];
    const cell = document.createElement('div');
    cell.className = 'cell' + (state.activeCode === code ? ' hl' : '');
    cell.innerHTML = `<div class="block" style="background:${c.hex}"></div><div class="code">${code}</div>`;
    cell.onclick = () => {
      state.activeCode = (state.activeCode === code) ? null : code;
      setTool('view');
      renderCanvas(); renderColorBar(); renderStats();
    };
    bar.appendChild(cell);
  }
}
function renderStats() {
  const list = $('stats-list');
  const totalEl = $('stats-total');
  if (!state.grid) { list.innerHTML = '<span class="muted">暂无数据</span>'; totalEl.textContent = ''; return; }
  const { counts, total } = computeCounts();

  // 预选过颜色时，统计显示全部预选色（含 0 颗），否则只显示实际用到的
  const preselected = state.lastRecognizedCodes && state.lastRecognizedCodes.length;
  const displayCodes = preselected
    ? state.lastRecognizedCodes.filter(c => state.codeToColor[c])
    : Object.keys(counts);

  const entries = displayCodes.map(code => [code, counts[code] || 0]);
  entries.sort((a, b) => b[1] - a[1]);
  list.innerHTML = '';
  for (const [code, cnt] of entries) {
    const c = state.codeToColor[code];
    const row = document.createElement('div');
    row.className = 'stat-row' + (state.activeCode === code ? ' hl' : '') + (cnt === 0 ? ' zero' : '');
    row.innerHTML = `<div class="block" style="background:${c.hex}"></div><span class="code">${code}</span><span class="cnt">${cnt} 颗</span>`;
    row.onclick = () => {
      state.activeCode = (state.activeCode === code) ? null : code;
      setTool('view');
      renderCanvas(); renderColorBar(); renderStats();
    };
    list.appendChild(row);
  }
  const usedCount = entries.filter(([, cnt]) => cnt > 0).length;
  totalEl.textContent = preselected
    ? `预选 ${entries.length} 种 · 使用 ${usedCount} 种 · 共 ${total} 颗`
    : `使用颜色：${entries.length} 种 · 总数量：${total} 颗`;
}
function updateBeadCount() {
  const { total } = computeCounts();
  $('bead-count').textContent = `共 ${total} 颗`;
}

// ---------- 导出 ----------
function download(canvas, name) {
  canvas.toBlob((blob) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }, 'image/png');
}
function exportNormal() {
  if (!state.grid) { alert('请先识别'); return; }
  const n = state.canvasSize, cell = 20;
  const cv = document.createElement('canvas');
  cv.width = cv.height = n * cell;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
    const code = state.grid[r][c];
    if (code) { ctx.fillStyle = state.codeToColor[code].hex; ctx.fillRect(c * cell, r * cell, cell, cell); }
  }
  download(cv, '拼豆作品图.png');
}
function exportRef() {
  if (!state.grid) { alert('请先识别'); return; }
  const n = state.canvasSize, cell = n === 52 ? 24 : 14, margin = 30;
  const { counts, total } = computeCounts();
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const statsRows = Math.ceil(entries.length / 7);
  const statsH = 34 + statsRows * 26;
  const W = n * cell + margin * 2, H = n * cell + margin * 2 + statsH;
  const cv = document.createElement('canvas');
  cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);

  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
    const x = margin + c * cell, y = margin + r * cell;
    const code = state.grid[r][c];
    ctx.fillStyle = code ? state.codeToColor[code].hex : '#f5f5f5';
    ctx.fillRect(x, y, cell, cell);
    ctx.strokeStyle = '#b9bec7'; ctx.lineWidth = 1;
    ctx.strokeRect(x + 0.5, y + 0.5, cell, cell);
    if (code && cell >= 12) {
      ctx.fillStyle = '#0008'; ctx.font = `${Math.max(5, cell * 0.36)}px sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(code, x + cell / 2, y + cell / 2);
    }
  }
  // 行列坐标
  ctx.fillStyle = '#333'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = '9px sans-serif';
  for (let c = 0; c < n; c++) if (c % 5 === 0 || c === n - 1) {
    ctx.fillText(c + 1, margin + c * cell + cell / 2, margin / 2);
  }
  for (let r = 0; r < n; r++) if (r % 5 === 0 || r === n - 1) {
    ctx.fillText(r + 1, margin / 2, margin + r * cell + cell / 2);
  }
  // 统计
  const sy = margin + n * cell + 24;
  ctx.textAlign = 'left'; ctx.font = 'bold 12px sans-serif';
  ctx.fillText(`颜色统计 · ${entries.length} 种 · 共 ${total} 颗`, margin, sy);
  entries.forEach(([code, cnt], i) => {
    const cx = margin + (i % 7) * 118, cy = sy + 16 + Math.floor(i / 7) * 24;
    ctx.fillStyle = state.codeToColor[code].hex;
    ctx.fillRect(cx, cy, 14, 14);
    ctx.strokeStyle = '#999'; ctx.strokeRect(cx + 0.5, cy + 0.5, 14, 14);
    ctx.fillStyle = '#333'; ctx.font = '11px sans-serif';
    ctx.fillText(`${code} × ${cnt}`, cx + 18, cy + 11);
  });
  download(cv, '拼豆参考图.png');
}

// ---------- 作品（个人缓存） ----------
async function saveWork() {
  if (!state.grid) { alert('请先识别'); return; }
  const { total } = computeCounts();
  if (!total) { alert('画布为空，无需转存'); return; }
  const btn = $('tool-save');
  const rows = parseInt($('rows').value, 10) || state.canvasSize;
  const cols = parseInt($('cols').value, 10) || state.canvasSize;
  const name = `作品 ${new Date().toLocaleString('zh-CN', { hour12: false })}`;
  btn.disabled = true;
  try {
    await api('/api/works', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, canvas_size: state.canvasSize, rows, cols, grid: state.grid }),
    });
    btn.textContent = '✓ 已转存';
    setTimeout(() => { btn.textContent = '💾 转存'; btn.disabled = false; }, 1500);
  } catch (e) {
    btn.disabled = false;
    alert(e.message);
  }
}

async function openWorks() {
  const dd = $('works-dropdown');
  if (dd.style.display !== 'none') { dd.style.display = 'none'; return; }
  dd.style.display = 'block';
  await renderWorks();
}

async function renderWorks() {
  const list = $('works-list');
  list.innerHTML = '<div class="works-empty">加载中…</div>';
  try {
    const data = await api('/api/works');
    if (!data.works.length) {
      list.innerHTML = '<div class="works-empty">还没有作品。识别满意后点工具栏「💾 转存」即可保存。</div>';
      return;
    }
    list.innerHTML = '';
    for (const w of data.works) {
      const item = document.createElement('div');
      item.className = 'work-item';
      const time = new Date(w.created_at * 1000).toLocaleString('zh-CN', { hour12: false });
      const info = document.createElement('div');
      info.style.cssText = 'flex:1;min-width:0';
      const nm = document.createElement('div');
      nm.className = 'name'; nm.textContent = w.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${w.rows}×${w.cols} · ${w.canvas_size}画布 · ${time}`;
      info.appendChild(nm); info.appendChild(meta);
      const del = document.createElement('button');
      del.className = 'del'; del.textContent = '🗑'; del.title = '删除';
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`删除作品「${w.name}」？`)) return;
        try { await api(`/api/works/${w.id}`, { method: 'DELETE' }); renderWorks(); }
        catch (err) { alert(err.message); }
      };
      item.appendChild(info); item.appendChild(del);
      item.onclick = () => loadWork(w.id);
      list.appendChild(item);
    }
  } catch (e) {
    list.innerHTML = `<div class="works-empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function loadWork(id) {
  try {
    const data = await api(`/api/works/${id}`);
    state.canvasSize = data.canvas_size;
    state.grid = data.grid;
    state.activeCode = null;
    state.lastRecognizedCodes = [];
    $('rows').value = data.rows;
    $('cols').value = data.cols;
    $('rows').max = $('cols').max = state.canvasSize;
    $('canvas-size-seg').querySelectorAll('button').forEach(x =>
      x.classList.toggle('active', parseInt(x.dataset.size, 10) === state.canvasSize));
    state.cellSize = computeCellSize(state.canvasSize);
    renderCanvas(); renderColorBar(); renderStats(); updateBeadCount();
    $('works-dropdown').style.display = 'none';
    if (isMobile()) closeDrawers();
  } catch (e) { alert(e.message); }
}

// ---------- 事件绑定 ----------
function bindEvents() {
  // 认证
  $('btn-logout').onclick = logout;
  $('tab-login').onclick = () => openAuth('login');
  $('tab-register').onclick = () => openAuth('register');
  $('btn-auth-submit').onclick = submitAuth;
  $('auth-password').onkeydown = (e) => { if (e.key === 'Enter') submitAuth(); };
  $('sidebar-toggle').onclick = () => { if (isMobile()) toggleDrawer('sidebar'); else toggleSidebar(); };
  $('stats-toggle').onclick = () => { if (isMobile()) toggleDrawer('stats'); };
  $('drawer-mask').onclick = closeDrawers;

  // 抽屉把手
  $('drawer-settings').onclick = () => openDrawer('sidebar');
  $('drawer-stats').onclick = () => openDrawer('stats');

  // 深色判定阈值滑杆
  $('text-threshold').oninput = () => {
    $('text-threshold-val').textContent = $('text-threshold').value;
  };

  // 颜色深度阈值滑杆（灰度<该值算深色像素，识别时生效）
  $('dark-threshold').oninput = () => {
    $('dark-threshold-val').textContent = $('dark-threshold').value;
  };

  // 画布尺寸
  $('canvas-size-seg').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      state.canvasSize = parseInt(b.dataset.size, 10);
      $('canvas-size-seg').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
      $('rows').max = $('cols').max = state.canvasSize;
      if (state.grid && state.grid.length !== state.canvasSize) state.grid = null;
      state.cellSize = computeCellSize(state.canvasSize);
      renderCanvas(); renderColorBar(); renderStats(); updateBeadCount();
    };
  });

  // 识别模式（智能 / 直接）
  $('recognize-mode-seg').querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      state.recognizeMode = b.dataset.mode;
      $('recognize-mode-seg').querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
    };
  });

  // 上传
  $('file-input').onchange = onFileChange;

  // 工具栏
  $('tool-brush').onclick = () => setTool('brush');
  $('tool-eraser').onclick = () => setTool('eraser');
  $('tool-view').onclick = () => setTool('view');
  $('tool-picker').onclick = () => setTool('picker');

  // 识别 / 清空 / 导出
  $('btn-recognize').onclick = doRecognize;
  $('btn-clear-sel').onclick = () => { state.selectedCodes.clear(); renderColorPicker(); updateSelCount(); };
  $('btn-export-normal').onclick = exportNormal;
  $('btn-export-ref').onclick = exportRef;

  // 作品转存 / 我的作品
  $('tool-save').onclick = saveWork;
  $('btn-works').onclick = openWorks;
  $('works-close').onclick = () => { $('works-dropdown').style.display = 'none'; };
  document.addEventListener('click', (e) => {
    const dd = $('works-dropdown');
    if (dd.style.display !== 'none' && !dd.contains(e.target) && e.target !== $('btn-works')) {
      dd.style.display = 'none';
    }
  });

  // 画布编辑（多指触控：单指操作工具，双指捏合缩放）
  const canvas = $('canvas');
  const pointers = new Map();   // pointerId -> {x, y}
  let painting = false;
  let pinching = false;
  let pinchDist = 0;
  let pinchCellSize = state.cellSize;

  const pinchDistance = () => {
    const pts = [...pointers.values()];
    if (pts.length < 2) return 0;
    return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  };

  canvas.addEventListener('pointerdown', (e) => {
    if (state.tool === 'view') return;  // 点击模式：不拦截，交给浏览器单指滚动
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.size === 2) {
      // 第二根手指落下 → 进入双指缩放，取消单指绘画避免误画
      pinching = true;
      painting = false;
      pinchDist = pinchDistance();
      pinchCellSize = state.cellSize;
    } else if (pointers.size === 1) {
      painting = true;
      pinching = false;
      paintAt(e);
    }
  });

  canvas.addEventListener('pointermove', (e) => {
    if (state.tool === 'view') return;  // 点击模式：交给浏览器单指滚动
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pinching && pointers.size >= 2) {
      const d = pinchDistance();
      if (pinchDist > 0 && d > 0) {
        const ratio = d / pinchDist;
        state.cellSize = Math.min(40, Math.max(3, Math.round(pinchCellSize * ratio)));
        renderCanvas();
      }
    } else if (painting && pointers.size === 1) {
      paintAt(e);
    }
  });

  const endPointer = (e) => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) {
      pinching = false;
      pinchDist = 0;
    }
    if (pointers.size === 0) {
      painting = false;
    } else {
      // 从双指回到单指：剩余手指回到绘画模式（等下一次移动才画，避免误画）
      painting = true;
    }
  };
  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', endPointer);

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 1 : -1;
    state.cellSize = Math.min(40, Math.max(3, state.cellSize + delta));
    renderCanvas();
  }, { passive: false });
}

// ---------- 启动 ----------
async function init() {
  bindEvents();
  await loadPalette();
  updateSelCount();
  renderCanvas();
  updateBeadCount();
  if (state.token) {
    try {
      const me = await api('/api/me');
      applyAuth(state.token, me.username);
      return;
    } catch (e) {
      state.token = null; localStorage.removeItem('pb_token');
    }
  }
  openAuth('login');
}
init();
