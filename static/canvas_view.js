/* canvas_view.js —— 主画布：渲染 + 浏览/标注双模式编辑 + 定位轮播模式
 *
 * 坐标约定：视口 = {scale, ox, oy}，屏幕→图像: (sx-ox)/scale
 * 框坐标全部 0-1 归一化。渲染顺序：底图 → 框(描边2px/填充12%) → OOB虚线
 * → 已修改橙框 → 选中高亮+手柄 → 类名chip。
 * 命中顺序：resize手柄(8px) → 框内部 → 空白。
 * 定位轮播（viewMode="focus"）：始终渲染整幅图，仅视口定位放大到当前框
 * （app.js 调 focusBox），可自由缩放/平移（W 回全图，Q/E 换框）。
 */
"use strict";

const CanvasView = (() => {
  const MIN_SCALE = 0.05, MAX_SCALE = 8;
  const HANDLE_R = 5;      // 手柄半径(屏幕px)
  const HIT_HANDLE_R = 9;  // 手柄命中半径
  const MIN_BOX_PX = 4;    // 最小框尺寸(图像px)
  const CHIP_FONT = "11px 'Segoe UI','Microsoft YaHei',sans-serif";

  let canvas, wrap, ctx;
  let dpr = 1;
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

  /* ---------------- 初始化 ---------------- */
  function init(cv, wrapEl, cb) {
    canvas = cv; wrap = wrapEl; callbacks = cb || {};
    ctx = canvas.getContext("2d");
    hintEl = document.getElementById("canvas-hint");
    const ro = new ResizeObserver(() => { resize(); render(); });
    ro.observe(wrap);
    resize();
    bindEvents();
  }

  function resize() {
    dpr = window.devicePixelRatio || 1;
    const r = wrap.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    canvas.style.width = r.width + "px";
    canvas.style.height = r.height + "px";
  }

  /* ---------------- 状态设置 ---------------- */
  function setMode(m) {
    mode = m;
    if (m === "annotate" && viewMode === "focus") setViewMode("full");  // 标注操作基于全图坐标
    canvas.style.cursor = mode === "annotate" ? "crosshair" : "grab";
    updateHint();
    render();
  }
  function setViewMode(m, boxId, index, total) {
    viewMode = m;
    if (m === "focus") {
      cropBoxId = boxId || cropBoxId;
      cropInfo = { index: index || cropInfo.index, total: total || cropInfo.total, nav: cropInfo.nav };
    }
    updateHint();
    render();
  }
  function setCropBox(boxId, index, total, nav) {
    cropBoxId = boxId;
    cropInfo = {
      index: index || cropInfo.index, total: total || cropInfo.total,
      nav: nav || cropInfo.nav || "Q/E 上一/下一框",
    };
    if (viewMode === "focus") { updateHint(); render(); }
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
  function setImage(im) { img = im; selectedId = null; updateHint(); fit(); }
  function setBoxes(arr) { boxes = arr; }
  function setClasses(arr) {
    classes = arr; classById = {};
    for (const c of arr) classById[c.id] = c;
  }
  function setSelected(id) { selectedId = id; render(); }
  function getSelected() { return selectedId; }
  function getMode() { return mode; }

  /* ---------------- 视口 ---------------- */
  function fit() {
    if (!img) { scale = 1; ox = oy = 0; render(); return; }
    const r = wrap.getBoundingClientRect();
    const pad = 24;
    scale = Math.max(MIN_SCALE, Math.min((r.width - pad) / img.width, (r.height - pad) / img.height));
    scale = Math.min(MAX_SCALE, scale);
    ox = (r.width - img.width * scale) / 2;
    oy = (r.height - img.height * scale) / 2;
    render();
  }
  function clampScale(s) { return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s)); }
  function zoomAt(factor, sx, sy) {
    const r = wrap.getBoundingClientRect();
    const px = sx ?? r.width / 2, py = sy ?? r.height / 2;
    const imgX = (px - ox) / scale, imgY = (py - oy) / scale;
    scale = clampScale(scale * factor);
    ox = px - imgX * scale; oy = py - imgY * scale;
    render();
  }
  function zoomIn() { zoomAt(1.25); }
  function zoomOut() { zoomAt(0.8); }
  function focusBox(id, animate) {
    const b = boxes.find(x => x.id === id);
    if (!b || !img) return;
    const cx = b.cx * img.width, cy = b.cy * img.height;
    const bw = Math.max(b.w * img.width, 16), bh = Math.max(b.h * img.height, 16);
    const r = wrap.getBoundingClientRect();
    const targetScale = clampScale(Math.max(scale, Math.min(6, Math.min(r.width / (bw * 2.2), r.height / (bh * 2.2)))));
    const targetOx = r.width / 2 - cx * targetScale;
    const targetOy = r.height / 2 - cy * targetScale;
    if (animate) {
      const s0 = scale, x0 = ox, y0 = oy;
      const t0 = performance.now(), dur = 220;
      if (anim) cancelAnimationFrame(anim);
      const step = (t) => {
        const k = Math.min(1, (t - t0) / dur);
        const e = 1 - Math.pow(1 - k, 3);
        scale = s0 + (targetScale - s0) * e;
        ox = x0 + (targetOx - x0) * e; oy = y0 + (targetOy - y0) * e;
        render();
        if (k < 1) anim = requestAnimationFrame(step);
      };
      anim = requestAnimationFrame(step);
    } else {
      scale = targetScale; ox = targetOx; oy = targetOy; render();
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
    return c && c.color ? c.color : "#64748B";
  }
  function luminance(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return 0.5;
    const n = parseInt(m[1], 16);
    return (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  }

  function render() {
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const r = wrap.getBoundingClientRect();
    ctx.fillStyle = "#0a0d11";
    ctx.fillRect(0, 0, r.width, r.height);
    if (!img) return;
    const iw = img.width * scale, ih = img.height * scale;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    try {
      ctx.drawImage(img.bitmap, ox, oy, iw, ih);
    } catch (err) { /* 旧图 bitmap 已被 ImageStore 回收：跳过绘制，等 setImage 换新图 */ }

    const chipOk = scale >= 0.35;
    for (const b of boxes) {
      drawBox(b, chipOk);
    }
    if (selectedId) {
      const b = boxes.find(x => x.id === selectedId);
      if (b) drawHandles(b);
    }
  }

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
    ctx.strokeStyle = isChanged && !isSel ? "#fb923c" : color;
    if (boxOob(b) && !isSel) ctx.strokeStyle = "#f87171";
    ctx.lineWidth = lw;
    if (boxOob(b) && !isSel) ctx.setLineDash([5, 4]); else ctx.setLineDash([]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    // 已修改角标
    if (isChanged && w > 14 && h > 14) {
      ctx.fillStyle = "#fb923c";
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
      cx0 = Math.max(0, Math.min(cx0, r().width - cw));
      const dark = luminance(color) > 0.55;
      ctx.fillStyle = color;
      ctx.fillRect(cx0, cy0, cw, ch);
      ctx.fillStyle = dark ? "#0b1220" : "#ffffff";
      ctx.textBaseline = "middle";
      ctx.fillText(label, cx0 + 5, cy0 + ch / 2 + 0.5);
    }
  }

  function r() { return wrap.getBoundingClientRect(); }

  function drawHandles(b) {
    const e = boxEdges(b);
    const x1 = e.x1 * img.width * scale + ox, y1 = e.y1 * img.height * scale + oy;
    const x2 = e.x2 * img.width * scale + ox, y2 = e.y2 * img.height * scale + oy;
    const pts = [
      [x1, y1, "nwse"], [(x1 + x2) / 2, y1, "ns"], [x2, y1, "nesw"],
      [x2, (y1 + y2) / 2, "ew"], [x2, y2, "nwse"], [(x1 + x2) / 2, y2, "ns"],
      [x1, y2, "nesw"], [x1, (y1 + y2) / 2, "ew"],
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

  /* ---------------- 交互事件 ---------------- */
  function bindEvents() {
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointerleave", () => {
      if (drag) onUp({ clientX: -1e4, clientY: -1e4 });
    });
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const p = mousePos(e);
      const b = hitBox(p.x, p.y);
      if (b) { selectedId = b.id; render(); }
      if (callbacks.onContextMenu) callbacks.onContextMenu(b ? b.id : null, e.clientX, e.clientY);
    });
    canvas.addEventListener("dblclick", (e) => {
      const p = mousePos(e);
      const b = hitBox(p.x, p.y);
      if (b) { selectedId = b.id; render(); }
      if (b && callbacks.onDblClickBox) callbacks.onDblClickBox(b.id);
    });
  }

  function onWheel(e) {
    e.preventDefault();
    const p = mousePos(e);
    zoomAt(e.deltaY < 0 ? 1.18 : 1 / 1.18, p.x, p.y);
  }

  function onDown(e) {
    if (e.button !== 0) return;
    if (!img) return;
    const p = mousePos(e);
    const handle = mode === "annotate" ? hitHandle(p.x, p.y) : null;
    const b = mode === "annotate" || mode === "browse" ? hitBox(p.x, p.y) : null;

    if (handle) {
      drag = startResize(b, handle, p);
      if (callbacks.onEditStart) callbacks.onEditStart();
    } else if (b) {
      selectedId = b.id;
      if (callbacks.onSelectBox) callbacks.onSelectBox(b.id);
      if (mode === "annotate") {
        if (callbacks.onEditStart) callbacks.onEditStart();
        const iv = toImg(p.x, p.y);
        drag = {
          kind: "move",
          box: b,
          orig: { cx: b.cx, cy: b.cy },
          startImg: iv, startScreen: p,
        };
      } else {
        drag = { kind: "pan", startScreen: p, moved: false, startOx: ox, startOy: oy };
      }
    } else {
      selectedId = null;
      if (callbacks.onSelectBox) callbacks.onSelectBox(null);
      if (mode === "annotate") {
        const iv = toImg(p.x, p.y);
        drag = {
          kind: "draw",
          startImg: iv, curImg: iv, startScreen: p,
          classId: callbacks.defaultNewClass ? callbacks.defaultNewClass() : 2,
        };
      } else {
        drag = { kind: "pan", startScreen: p, moved: false, startOx: ox, startOy: oy };
      }
    }
    canvas.setPointerCapture(e.pointerId);
    render();
  }

  function onMove(e) {
    const p = mousePos(e);
    // 光标样式
    if (!drag) updateCursor(p);
    if (!drag) return;
    const dx = p.x - drag.startScreen.x, dy = p.y - drag.startScreen.y;

    if (drag.kind === "pan") {
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) { ox = drag.startOx + dx; oy = drag.startOy + dy; render(); }
      return;
    }
    if (drag.kind === "move") {
      const ix = drag.startImg.x + dx / scale, iy = drag.startImg.y + dy / scale;
      const b = drag.box;
      const hw = b.w / 2, hh = b.h / 2;
      b.cx = Math.min(1 - hw, Math.max(hw, drag.orig.cx + (ix - drag.startImg.x) / img.width));
      b.cy = Math.min(1 - hh, Math.max(hh, drag.orig.cy + (iy - drag.startImg.y) / img.height));
      render();
      return;
    }
    if (drag.kind === "resize") {
      applyResize(drag, p);
      render();
      return;
    }
    if (drag.kind === "draw") {
      const iv = toImg(p.x, p.y);
      drag.curImg = iv;
      render();
      // 临时绘制新框预览
      drawPreview(drag);
    }
  }

  function drawPreview(d) {
    const x1 = Math.min(d.startImg.x, d.curImg.x), x2 = Math.max(d.startImg.x, d.curImg.x);
    const y1 = Math.min(d.startImg.y, d.curImg.y), y2 = Math.max(d.startImg.y, d.curImg.y);
    const sx = x1 * scale + ox, sy = y1 * scale + oy;
    const w = (x2 - x1) * scale, h = (y2 - y1) * scale;
    const color = classById[d.classId] ? classById[d.classId].color : "#4f8cff";
    ctx.fillStyle = color + "22";
    ctx.fillRect(sx, sy, w, h);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(sx, sy, w, h);
    ctx.setLineDash([]);
  }

  function onUp(e) {
    if (!drag) return;
    const d = drag;
    drag = null;

    if (d.kind === "pan" && d.moved) { render(); return; }
    if (d.kind === "move") {
      if (callbacks.onBoxesChanged) callbacks.onBoxesChanged([d.box]);
      return;
    }
    if (d.kind === "resize") {
      if (callbacks.onBoxesChanged) callbacks.onBoxesChanged([d.box]);
      return;
    }
    if (d.kind === "draw") {
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
      render();
    }
  }

  function startResize(b, dir, p) {
    return {
      kind: "resize", box: b, dir,
      startImg: toImg(p.x, p.y), startScreen: p,
      orig: { cx: b.cx, cy: b.cy, w: b.w, h: b.h },
    };
  }

  function applyResize(d, p) {
    const b = d.box;
    const ix = d.startImg.x + (p.x - d.startScreen.x) / scale;
    const iy = d.startImg.y + (p.y - d.startScreen.y) / scale;
    // 图像坐标像素
    const px = Math.max(0, Math.min(ix, img.width)), py = Math.max(0, Math.min(iy, img.height));
    const o = d.orig;
    let x1 = (o.cx - o.w / 2) * img.width, y1 = (o.cy - o.h / 2) * img.height;
    let x2 = (o.cx + o.w / 2) * img.width, y2 = (o.cy + o.h / 2) * img.height;

    const fixedX1 = d.dir.includes("e"), fixedY1 = d.dir.includes("s");
    const fixedX2 = d.dir.includes("w"), fixedY2 = d.dir.includes("n");

    if (!fixedX1) x1 = Math.min(px, x2 - MIN_BOX_PX);
    if (!fixedY1) y1 = Math.min(py, y2 - MIN_BOX_PX);
    if (!fixedX2) x2 = Math.max(px, x1 + MIN_BOX_PX);
    if (!fixedY2) y2 = Math.max(py, y1 + MIN_BOX_PX);

    // 钳制到图像
    if (x1 < 0) { if (!fixedX1) x1 = 0; }
    if (y1 < 0) { if (!fixedY1) y1 = 0; }
    if (x2 > img.width) { if (!fixedX2) x2 = img.width; }
    if (y2 > img.height) { if (!fixedY2) y2 = img.height; }

    b.cx = ((x1 + x2) / 2) / img.width;
    b.cy = ((y1 + y2) / 2) / img.height;
    b.w = (x2 - x1) / img.width;
    b.h = (y2 - y1) / img.height;
  }

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
    setViewMode, setCropBox, getViewMode, getCropBox,
    get boxes() { return boxes; },
  };
})();
