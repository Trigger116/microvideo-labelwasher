/* canvas_view.js —— 主画布：渲染 + 浏览/标注双模式编辑 + 定位轮播模式
 *
 * 坐标约定：视口 = {scale, ox, oy}，屏幕→图像: (sx-ox)/scale
 * 框坐标全部 0-1 归一化。渲染顺序：底图 → 框(描边2px/填充12%) → OOB虚线
 * → 已修改橙框 → 选中高亮+手柄 → 类名chip。
 * 命中顺序：resize手柄(8px) → 框内部 → 空白。
 * 定位轮播（viewMode="focus"）：始终渲染整幅图，仅视口定位放大到当前框
 * （app.js 调 focusBox），可自由缩放/平移（W 回全图，Q/E 换框）。
 *
 * 渲染管线（v1.3.0 性能版）：
 * - 双层 canvas：#img-canvas 底图（仅平移/缩放/换图重画）+ #main-canvas 标注层
 *   （拖动/编辑热路径只画这层，消除每帧大图重采样）
 * - rAF dirty-flag 合帧：render() 只标记，下一帧统一绘制
 * - viewW/viewH 缓存视口尺寸，drawBox 循环内不再 getBoundingClientRect
 * - 交互中（拖动/缩放/动画）imageSmoothingQuality 降 low，静止后回 high
 */
"use strict";

const CanvasView = (() => {
  /* 从 CSS 变量读取颜色（带缓存 + 回退），保证 Canvas 绘制与深色主题一致（R7） */
  let cssVarCache = {};
  function cssVar(name, fallback) {
    if (cssVarCache[name]) return cssVarCache[name];
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      cssVarCache[name] = v || fallback;
    } catch (e) { cssVarCache[name] = fallback; }
    return cssVarCache[name];
  }

  const MIN_SCALE = 0.05, MAX_SCALE = 8;
  const HANDLE_R = 5;      // 手柄半径(屏幕px)
  const HIT_HANDLE_R = 9;  // 手柄命中半径
  const MIN_BOX_PX = 4;    // 最小框尺寸(图像px)
  const CHIP_FONT = "11px 'Segoe UI','Microsoft YaHei',sans-serif";

  let canvas, imgCanvas, wrap, ctx, imgCtx;
  let dpr = 1;
  let viewW = 0, viewH = 0;      // 视口 CSS 尺寸缓存（resize 时更新）
  let callbacks = {};
  let mode = "browse";            // browse | annotate
  let img = null;                 // {bitmap,width,height}
  let boxes = [];                 // 当前框引用（app.js 主副本）
  let classes = [];
  let classById = {};
  let selectedId = null;
  let scale = 1, ox = 0, oy = 0;
  let drag = null;                // 拖拽状态机
  let anim = null;                // focus 动画
  let hintEl = null;
  let viewMode = "full";          // full | focus（定位轮播）
  let cropBoxId = null;           // 定位轮播当前框
  let cropInfo = { index: 0, total: 0, nav: "Q/E 上一/下一框" };

  /* rAF 合帧：dirty 标记 → 下一帧 draw */
  let dirtyImg = true, dirtyAnno = true, rafPending = false;
  let lowQualityUntil = 0;        // 交互中底图用低质量采样，静止后回 high
  let preview = null;             // 画新框预览 {x1,y1,x2,y2,classId}（图像坐标）

  function markDirty(all) {
    dirtyAnno = true;
    if (all) dirtyImg = true;
    scheduleDraw();
  }
  function scheduleDraw() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; draw(); });
  }
  function markInteracting() { lowQualityUntil = performance.now() + 150; }

  function draw() {
    if (!ctx) return;
    if (dirtyImg) { drawImageLayer(); dirtyImg = false; }
    if (dirtyAnno) { drawAnnoLayer(); dirtyAnno = false; }
  }

  function drawImageLayer() {
    imgCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    imgCtx.fillStyle = cssVar("--canvas-bg", "#16161F");
    imgCtx.fillRect(0, 0, viewW, viewH);
    if (!img) return;
    const iw = img.width * scale, ih = img.height * scale;
    imgCtx.imageSmoothingEnabled = true;
    imgCtx.imageSmoothingQuality = performance.now() < lowQualityUntil ? "low" : "high";
    try {
      imgCtx.drawImage(img.bitmap, ox, oy, iw, ih);
    } catch (err) { /* 旧图 bitmap 已被 ImageStore 回收：跳过绘制，等 setImage 换新图 */ }
  }

  function drawAnnoLayer() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, viewW, viewH);
    if (!img) return;
    const chipOk = scale >= 0.35;
    for (const b of boxes) {
      // 拖拽中画的是本地副本（真实框未提交前保持原样）
      const rb = (drag && drag.boxLocal && b.id === drag.boxLocal.id) ? drag.boxLocal : b;
      drawBox(rb, chipOk);
    }
    if (selectedId) {
      const b = boxes.find(x => x.id === selectedId);
      if (b) {
        const hb = (drag && drag.boxLocal && drag.boxLocal.id === selectedId) ? drag.boxLocal : b;
        drawHandles(hb);
      }
    }
    if (preview) drawPreviewRect(preview);
  }

  /* ---------------- 初始化 ---------------- */
  function init(cv, wrapEl, cb) {
    canvas = cv; wrap = wrapEl; callbacks = cb || {};
    imgCanvas = document.getElementById("img-canvas");
    ctx = canvas.getContext("2d");
    imgCtx = imgCanvas.getContext("2d");
    hintEl = document.getElementById("canvas-hint");
    const ro = new ResizeObserver(() => { resize(); markDirty(true); });
    ro.observe(wrap);
    resize();
    bindEvents();
    draw();
  }

  function resize() {
    dpr = window.devicePixelRatio || 1;
    const r = wrap.getBoundingClientRect();
    viewW = r.width; viewH = r.height;
    for (const c of [canvas, imgCanvas]) {
      c.width = Math.max(1, Math.round(viewW * dpr));
      c.height = Math.max(1, Math.round(viewH * dpr));
      c.style.width = viewW + "px";
      c.style.height = viewH + "px";
    }
  }

  /* ---------------- 状态设置 ---------------- */
  function setMode(m) {
    cancelDrag();   // 拖拽中切模式：丢弃未完成拖拽
    mode = m;
    if (m === "annotate" && viewMode === "focus") setViewMode("full");  // 标注操作基于全图坐标
    canvas.style.cursor = mode === "annotate" ? "crosshair" : "grab";
    updateHint();
    markDirty(false);
  }
  function setViewMode(m, boxId, index, total) {
    viewMode = m;
    if (m === "focus") {
      cropBoxId = boxId || cropBoxId;
      cropInfo = { index: index || cropInfo.index, total: total || cropInfo.total, nav: cropInfo.nav };
    }
    updateHint();
    markDirty(false);
  }
  function setCropBox(boxId, index, total, nav) {
    cropBoxId = boxId;
    cropInfo = {
      index: index || cropInfo.index, total: total || cropInfo.total,
      nav: nav || cropInfo.nav || "Q/E 上一/下一框",
    };
    if (viewMode === "focus") { updateHint(); markDirty(false); }
  }
  function getViewMode() { return viewMode; }
  function getCropBox() { return cropBoxId; }
  function updateHint() {
    if (!hintEl) return;
    if (viewMode === "focus") {
      hintEl.textContent = `🔍 定位轮播 ${cropInfo.index}/${cropInfo.total} · ${cropInfo.nav} · 滚轮缩放 · W 回全图`;
      hintEl.className = "hint focus";
      return;
    }
    hintEl.textContent = mode === "annotate"
      ? "✏️ 标注模式：拖框移动 · 手柄缩放 · 空白拖拽画新框 · 右键=删除/改类"
      : "🔍 浏览模式：滚轮缩放 · 拖拽平移 · 点击选择框 · W 定位";
    hintEl.className = "hint " + mode;
  }
  function setImage(im) { cancelDrag(); img = im; selectedId = null; updateHint(); fit(); }
  function setBoxes(arr) { cancelDrag(); boxes = arr; markDirty(false); }
  function setClasses(arr) {
    classes = arr; classById = {};
    for (const c of arr) classById[c.id] = c;
  }
  function setSelected(id) { selectedId = id; markDirty(false); }
  function getSelected() { return selectedId; }
  function getMode() { return mode; }

  /* ---------------- 视口 ---------------- */
  function fit() {
    if (!img) { scale = 1; ox = oy = 0; markDirty(true); return; }
    const pad = 24;
    scale = Math.max(MIN_SCALE, Math.min((viewW - pad) / img.width, (viewH - pad) / img.height));
    scale = Math.min(MAX_SCALE, scale);
    ox = (viewW - img.width * scale) / 2;
    oy = (viewH - img.height * scale) / 2;
    markDirty(true);
  }
  function clampScale(s) { return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s)); }
  function zoomAt(factor, sx, sy) {
    const px = sx ?? viewW / 2, py = sy ?? viewH / 2;
    const imgX = (px - ox) / scale, imgY = (py - oy) / scale;
    scale = clampScale(scale * factor);
    ox = px - imgX * scale; oy = py - imgY * scale;
    markInteracting();
    markDirty(true);
  }
  function zoomIn() { zoomAt(1.25); }
  function zoomOut() { zoomAt(0.8); }
  function focusBox(id, animate) {
    const b = boxes.find(x => x.id === id);
    if (!b || !img) return;
    const cx = b.cx * img.width, cy = b.cy * img.height;
    const bw = Math.max(b.w * img.width, 16), bh = Math.max(b.h * img.height, 16);
    const targetScale = clampScale(Math.max(scale, Math.min(6, Math.min(viewW / (bw * 2.2), viewH / (bh * 2.2)))));
    const targetOx = viewW / 2 - cx * targetScale;
    const targetOy = viewH / 2 - cy * targetScale;
    if (animate) {
      const s0 = scale, x0 = ox, y0 = oy;
      const t0 = performance.now(), dur = 220;
      if (anim) cancelAnimationFrame(anim);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / dur);
        const e = 1 - Math.pow(1 - k, 3);
        scale = s0 + (targetScale - s0) * e;
        ox = x0 + (targetOx - x0) * e; oy = y0 + (targetOy - y0) * e;
        markDirty(true);
        if (k < 1) anim = requestAnimationFrame(step);
      };
      anim = requestAnimationFrame(step);
    } else {
      scale = targetScale; ox = targetOx; oy = targetOy; markDirty(true);
    }
  }

  /* ---------------- 几何 ---------------- */
  const boxEdges = (b) => ({
    x1: b.cx - b.w / 2, y1: b.cy - b.h / 2,
    x2: b.cx + b.w / 2, y2: b.cy + b.h / 2,
  });
  const boxOob = (b) => { const e = boxEdges(b); return e.x1 < -1e-9 || e.y1 < -1e-9 || e.x2 > 1 + 1e-9 || e.y2 > 1 + 1e-9; };
  const toScreen = (ix, iy) => ({ x: ix * scale + ox, y: iy * scale + oy });
  const toImg = (sx, sy) => ({ x: (sx - ox) / scale, y: (sy - oy) / scale });

  function mousePos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  /* ---------------- 命中测试 ---------------- */
  function hitHandle(sx, sy) {
    if (mode !== "annotate" || !selectedId) return null;
    const b = boxes.find(x => x.id === selectedId);
    if (!b || !img) return null;
    const e = boxEdges(b);
    const pts = [ // 角→边
      ["nw", e.x1, e.y1], ["ne", e.x2, e.y1], ["se", e.x2, e.y2], ["sw", e.x1, e.y2],
      ["n", (e.x1 + e.x2) / 2, e.y1], ["e", e.x2, (e.y1 + e.y2) / 2],
      ["s", (e.x1 + e.x2) / 2, e.y2], ["w", e.x1, (e.y1 + e.y2) / 2],
    ];
    for (const [dir, ix, iy] of pts) {
      const p = toScreen(ix * img.width, iy * img.height);
      if (Math.hypot(sx - p.x, sy - p.y) <= HIT_HANDLE_R) return dir;
    }
    return null;
  }

  function hitBox(sx, sy) {
    if (!img) return null;
    const ix = (sx - ox) / scale, iy = (sy - oy) / scale;
    for (let i = boxes.length - 1; i >= 0; i--) {
      const b = boxes[i];
      const e = boxEdges(b);
      if (ix >= e.x1 * img.width && ix <= e.x2 * img.width &&
          iy >= e.y1 * img.height && iy <= e.y2 * img.height) return b;
    }
    return null;
  }

  /* ---------------- 渲染 ---------------- */
  function chipText(b) {
    const c = classById[b.class_id];
    return c ? c.name : "?" + b.class_id;
  }
  function chipColor(b) {
    const c = classById[b.class_id];
    return c && c.color ? c.color : cssVar("--muted", "#9CA3AF");
  }
  function luminance(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return 0.5;
    const n = parseInt(m[1], 16);
    return (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  }

  /* render()：语义 = 标记脏（rAF 合帧），外部调用无需等待同步绘制 */
  function render() { markDirty(false); }

  function drawBox(b, chipOk) {
    const e = boxEdges(b);
    const x = e.x1 * img.width * scale + ox, y = e.y1 * img.height * scale + oy;
    const w = (e.x2 - e.x1) * img.width * scale, h = (e.y2 - e.y1) * img.height * scale;
    const color = chipColor(b);
    const isSel = b.id === selectedId;
    const isChanged = b.changed || b.is_new;

    // 填充 12%
    ctx.fillStyle = color + "1f";
    ctx.fillRect(x, y, w, h);

    // 描边
    const lw = isSel ? 3 : 2;
    ctx.strokeStyle = isChanged && !isSel ? cssVar("--changed", "#FB923C") : color;
    if (boxOob(b) && !isSel) ctx.strokeStyle = cssVar("--oob", "#F87171");
    ctx.lineWidth = lw;
    if (boxOob(b) && !isSel) ctx.setLineDash([5, 4]); else ctx.setLineDash([]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    // 已修改角标
    if (isChanged && w > 14 && h > 14) {
      ctx.fillStyle = cssVar("--changed", "#FB923C");
      ctx.beginPath();
      ctx.moveTo(x, y); ctx.lineTo(x + 11, y); ctx.lineTo(x, y + 11); ctx.closePath();
      ctx.fill();
    }

    // 类名 chip
    if (chipOk) {
      const label = chipText(b);
      ctx.font = CHIP_FONT;
      const tw = ctx.measureText(label).width;
      const cw = tw + 10, ch = 17;
      let cx0 = x, cy0 = y - ch - 3;
      if (cy0 < 0) cy0 = y + 2;          // 顶部出界 → 画到框内
      cx0 = Math.max(0, Math.min(cx0, viewW - cw));
      const dark = luminance(color) > 0.55;
      ctx.fillStyle = color;
      ctx.fillRect(cx0, cy0, cw, ch);
      ctx.fillStyle = dark ? "#0b1220" : "#ffffff";
      ctx.textBaseline = "middle";
      ctx.fillText(label, cx0 + 5, cy0 + ch / 2 + 0.5);
    }
  }

  function drawHandles(b) {
    const e = boxEdges(b);
    const x1 = e.x1 * img.width * scale + ox, y1 = e.y1 * img.height * scale + oy;
    const x2 = e.x2 * img.width * scale + ox, y2 = e.y2 * img.height * scale + oy;
    const pts = [
      [x1, y1], [(x1 + x2) / 2, y1], [x2, y1],
      [x2, (y1 + y2) / 2], [x2, y2], [(x1 + x2) / 2, y2],
      [x1, y2], [x1, (y1 + y2) / 2],
    ];
    if (scale < 0.5) return;  // 太小不画手柄
    for (const [px, py] of pts) {
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#0b1220";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(px, py, HANDLE_R, 0, Math.PI * 2);
      ctx.fill(); ctx.stroke();
    }
  }

  function drawPreviewRect(d) {
    const x1 = Math.min(d.x1, d.x2), x2 = Math.max(d.x1, d.x2);
    const y1 = Math.min(d.y1, d.y2), y2 = Math.max(d.y1, d.y2);
    const sx = x1 * scale + ox, sy = y1 * scale + oy;
    const w = (x2 - x1) * scale, h = (y2 - y1) * scale;
    const color = classById[d.classId] ? classById[d.classId].color : cssVar("--accent", "#3B82F6");
    ctx.fillStyle = color + "22";
    ctx.fillRect(sx, sy, w, h);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(sx, sy, w, h);
    ctx.setLineDash([]);
  }

  /* ---------------- 交互事件 ---------------- */
  function bindEvents() {
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", () => cancelDrag());
    canvas.addEventListener("lostpointercapture", (e) => {
      if (drag && drag.pointerId === e.pointerId) cancelDrag();  // 未收到 up 即丢失捕获：丢弃副本
    });
    canvas.addEventListener("pointerleave", () => {
      if (drag) onUp({ clientX: -1e4, clientY: -1e4 });  // 兜底提交（有 capture 时几乎不触发）
    });
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = mousePos(e);
      const b = hitBox(p.x, p.y);
      if (b) { selectedId = b.id; markDirty(false); }
      if (callbacks.onContextMenu) callbacks.onContextMenu(b ? b.id : null, e.clientX, e.clientY);
    });
    canvas.addEventListener("dblclick", (e) => {
      const p = mousePos(e);
      const b = hitBox(p.x, p.y);
      if (b) { selectedId = b.id; markDirty(false); }
      if (b && callbacks.onDblClickBox) callbacks.onDblClickBox(b.id);
    });
  }

  function onWheel(e) {
    e.preventDefault();
    if (drag) return;   // 拖拽中忽略缩放（labelimg 同款互斥：防 scale 跳变破坏增量几何）
    const p = mousePos(e);
    zoomAt(e.deltaY < 0 ? 1.18 : 1 / 1.18, p.x, p.y);
  }

  function onDown(e) {
    if (e.button !== 0) return;
    if (drag) return;   // 防多指第二 pointerdown 覆盖状态机
    if (!img) return;
    const p = mousePos(e);
    const handle = mode === "annotate" ? hitHandle(p.x, p.y) : null;
    const b = mode === "annotate" || mode === "browse" ? hitBox(p.x, p.y) : null;

    if (handle) {
      // hitHandle 基于 selectedId 命中；用选中框本体（修复重叠框时拖到非选中框）
      const sb = boxes.find(x => x.id === selectedId);
      if (!sb) return;
      drag = startResize(sb, handle, p, e.pointerId);
      if (callbacks.onEditStart) callbacks.onEditStart();
    } else if (b) {
      selectedId = b.id;
      if (callbacks.onSelectBox) callbacks.onSelectBox(b.id);
      if (mode === "annotate") {
        if (callbacks.onEditStart) callbacks.onEditStart();
        // labelimg 模式：深拷贝 boxLocal，拖拽全程只改副本，onUp 一次性提交
        drag = { kind: "move", box: b, boxLocal: { ...b }, lastScreen: p, pointerId: e.pointerId };
      } else {
        drag = { kind: "pan", startScreen: p, moved: false, startOx: ox, startOy: oy, pointerId: e.pointerId };
      }
    } else {
      selectedId = null;
      if (callbacks.onSelectBox) callbacks.onSelectBox(null);
      if (mode === "annotate") {
        const iv = toImg(p.x, p.y);
        drag = {
          kind: "draw",
          startImg: iv, curImg: iv, startScreen: p, pointerId: e.pointerId,
          classId: callbacks.defaultNewClass ? callbacks.defaultNewClass() : 2,
        };
      } else {
        drag = { kind: "pan", startScreen: p, moved: false, startOx: ox, startOy: oy, pointerId: e.pointerId };
      }
    }
    try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
    markDirty(false);
  }

  function onMove(e) {
    const p = mousePos(e);
    // 光标样式
    if (!drag) { updateCursor(p); return; }
    if ((e.buttons & 1) === 0) { cancelDrag(); return; }  // 左键已松开但未收到 up：丢弃副本

    if (drag.kind === "pan") {
      const dx = p.x - drag.startScreen.x, dy = p.y - drag.startScreen.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        ox = drag.startOx + dx; oy = drag.startOy + dy;
        markInteracting();
        markDirty(true);   // 平移：底图+标注层都重画
      }
      return;
    }
    if (drag.kind === "move") {
      // 增量式：每步 delta 按当前 scale 换算（滚轮/动画中途变 scale 不再跳飞）
      const dx = (p.x - drag.lastScreen.x) / scale, dy = (p.y - drag.lastScreen.y) / scale;
      drag.lastScreen = p;
      const b = drag.boxLocal;
      const hw = b.w / 2, hh = b.h / 2;
      b.cx = Math.min(1 - hw, Math.max(hw, b.cx + dx / img.width));
      b.cy = Math.min(1 - hh, Math.max(hh, b.cy + dy / img.height));
      markDirty(false);   // 热路径：仅标注层
      return;
    }
    if (drag.kind === "resize") {
      applyResizeIncremental(drag, p);
      markDirty(false);
      return;
    }
    if (drag.kind === "draw") {
      const iv = toImg(p.x, p.y);
      drag.curImg = iv;
      preview = { x1: drag.startImg.x, y1: drag.startImg.y, x2: iv.x, y2: iv.y, classId: drag.classId };
      markDirty(false);
    }
  }

  function onUp(e) {
    if (!drag) return;
    const d = drag;
    drag = null;

    if (d.kind === "pan") { if (d.moved) markDirty(true); return; }
    if (d.kind === "move" || d.kind === "resize") {
      // 提交副本回原对象（保持数组内对象同一性）；框已被撤销/删除则丢弃副本
      if (d.boxLocal && d.box && boxes.includes(d.box)) {
        const changed = d.box.cx !== d.boxLocal.cx || d.box.cy !== d.boxLocal.cy ||
          d.box.w !== d.boxLocal.w || d.box.h !== d.boxLocal.h;
        Object.assign(d.box, d.boxLocal);
        if (changed && callbacks.onBoxesChanged) callbacks.onBoxesChanged([d.box]);
      }
      return;
    }
    if (d.kind === "draw") {
      preview = null;
      const x1 = Math.min(d.startImg.x, d.curImg.x), x2 = Math.max(d.startImg.x, d.curImg.x);
      const y1 = Math.min(d.startImg.y, d.curImg.y), y2 = Math.max(d.startImg.y, d.curImg.y);
      const w = x2 - x1, h = y2 - y1;
      if (w >= MIN_BOX_PX && h >= MIN_BOX_PX) {
        const nb = {
          id: null, // 服务端分配
          class_id: d.classId,
          cx: Math.min(1, Math.max(0, (x1 + x2) / 2 / img.width)),
          cy: Math.min(1, Math.max(0, (y1 + y2) / 2 / img.height)),
          w: Math.min(1, w / img.width),
          h: Math.min(1, h / img.height),
          is_new: true, changed: true, verified: false,
        };
        boxes.push(nb);
        selectedId = null;
        if (callbacks.onBoxCreated) callbacks.onBoxCreated(nb);
        if (callbacks.onBoxesChanged) callbacks.onBoxesChanged([nb]);
      }
      markDirty(false);
    }
  }

  function cancelDrag() {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (d.kind === "draw") preview = null;
    try { canvas.releasePointerCapture(d.pointerId); } catch (err) {}
    markDirty(false);   // 副本丢弃，不提交、不触发保存
  }

  function startResize(sb, dir, p, pointerId) {
    const e = boxEdges(sb);
    return {
      kind: "resize", box: sb, boxLocal: { ...sb }, dir, pointerId,
      lastScreen: p,
      // 工作边状态（图像 px，随指针累积；固定边保持不变）
      edges: { x1: e.x1 * img.width, y1: e.y1 * img.height, x2: e.x2 * img.width, y2: e.y2 * img.height },
    };
  }

  function applyResizeIncremental(d, p) {
    // 每步增量按当前 scale 换算为图像 px，在上一帧工作边上累积（固定边不动）
    const dx = (p.x - d.lastScreen.x) / scale, dy = (p.y - d.lastScreen.y) / scale;
    d.lastScreen = p;
    const g = d.edges;

    // dir 为单字母方向：固定边 = 不含该方向字母（如 nw 手柄固定右 e/下 s 边）
    const fixedX1 = !d.dir.includes("w"), fixedY1 = !d.dir.includes("n");
    const fixedX2 = !d.dir.includes("e"), fixedY2 = !d.dir.includes("s");

    if (!fixedX1) g.x1 = Math.max(0, Math.min(g.x2 - MIN_BOX_PX, g.x1 + dx));
    if (!fixedX2) g.x2 = Math.min(img.width, Math.max(g.x1 + MIN_BOX_PX, g.x2 + dx));
    if (!fixedY1) g.y1 = Math.max(0, Math.min(g.y2 - MIN_BOX_PX, g.y1 + dy));
    if (!fixedY2) g.y2 = Math.min(img.height, Math.max(g.y1 + MIN_BOX_PX, g.y2 + dy));

    const b = d.boxLocal;
    b.cx = (g.x1 + g.x2) / 2 / img.width;
    b.cy = (g.y1 + g.y2) / 2 / img.height;
    b.w = (g.x2 - g.x1) / img.width;
    b.h = (g.y2 - g.y1) / img.height;
  }

  function isDragging() { return !!drag; }

  function updateCursor(p) {
    if (!img) { canvas.style.cursor = "default"; return; }
    if (mode === "annotate") {
      const h = hitHandle(p.x, p.y);
      if (h) {
        canvas.style.cursor = { nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize",
          sw: "nesw-resize", n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize" }[h];
        return;
      }
      if (hitBox(p.x, p.y)) { canvas.style.cursor = "move"; return; }
      canvas.style.cursor = "crosshair";
    } else {
      if (hitBox(p.x, p.y)) { canvas.style.cursor = "pointer"; return; }
      canvas.style.cursor = "grab";
    }
  }

  return {
    init, resize, setMode, setImage, setBoxes, setClasses, setSelected,
    getSelected, getMode, fit, zoomAt, zoomIn, zoomOut, focusBox, render,
    setViewMode, setCropBox, getViewMode, getCropBox, isDragging, cancelDrag,
    get boxes() { return boxes; },
  };
})();
