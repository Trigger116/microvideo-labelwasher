/* crop_panel.js —— 裁剪缩略面板：纯前端 96×96 裁剪、T1 过滤、快速降级、✓ 核验标记
 *
 * v1.3.0 性能版：keyed diff 更新——itemMap(boxId→{div,canvas,meta...})，
 * 框几何/类未变时只更新文本/颜色/✓/选中 class，不重画缩略图（drawImage 零重采样）。
 */
"use strict";

const CropPanel = (() => {
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

  const SIZE = 96;      // 裁剪画布边长
  const PAD_RATIO = 1.8; // 框外扩倍数

  let gridEl, toolbarEl;
  let callbacks = {};
  let img = null, boxes = [], classes = [], classById = {};
  let opts = { task: null, verifiedIds: new Set() };
  let selectedBoxId = null;

  /* keyed 缓存：boxId → {div, canvas, nameEl, subEl, vEl, sig} */
  const itemMap = new Map();
  let taskKey = "";   // 当前任务 id（变化时全量重建，降级按钮随任务规则）

  function init(grid, toolbar, cb) {
    gridEl = grid; toolbarEl = toolbar; callbacks = cb || {};
    renderToolbar();
  }

  function renderToolbar() {
    // 展示模式指示：T1（按类别）自动由任务决定，无需手动切换
    toolbarEl.innerHTML = "";
  }

  function setClasses(arr) {
    classes = arr; classById = {};
    for (const c of arr) classById[c.id] = c;
  }

  function render(im, boxArr, clsArr, o) {
    img = im; boxes = boxArr;
    const newTaskKey = o && o.task ? o.task.id : "";
    if (newTaskKey !== taskKey) {   // 任务切换：降级按钮集/过滤规则变化 → 全量重建
      gridEl.innerHTML = "";
      itemMap.clear();
      taskKey = newTaskKey;
    }
    opts = o || opts;
    if (clsArr) setClasses(clsArr);

    const shown = visibleBoxes();
    if (!shown.length) {
      gridEl.innerHTML = `<div class="crop-empty">${
        opts.task ? "本图无目标类别框，按 Tab 跳到下一图" : "本图无标注框"}</div>`;
      itemMap.clear();
      return;
    }

    // diff：新增/保留/移除
    const seen = new Set();
    for (const b of shown) {
      seen.add(b.id);
      const it = itemMap.get(b.id);
      if (it) updateItem(it, b);
      else itemMap.set(b.id, buildItem(b));
    }
    for (const [id, it] of itemMap) {
      if (!seen.has(id)) { it.div.remove(); itemMap.delete(id); }
    }
    // 顺序重排（appendChild 移动节点）：T2 focus 排序/框序变化时面板顺序同步
    for (const b of shown) gridEl.appendChild(itemMap.get(b.id).div);
  }

  function sigOf(b) {   // 几何+类签名：变化才重画缩略图
    return `${b.class_id}|${b.cx.toFixed(4)}|${b.cy.toFixed(4)}|${b.w.toFixed(4)}|${b.h.toFixed(4)}|${b.is_new ? 1 : 0}|${b.changed ? 1 : 0}`;
  }

  function updateItem(it, b) {
    const sig = sigOf(b);
    if (it.sig !== sig) {
      it.sig = sig;
      drawCrop(it.canvas, b);
      const c = classById[b.class_id];
      it.nameEl.style.color = c && c.color ? c.color : cssVar("--text", "#E5E7EB");
      it.nameEl.textContent = (c ? c.name : "?") + (b.is_new ? " ✨新" : "");
    }
    const e = (b.cx - b.w / 2 < 0 || b.cy - b.h / 2 < 0 ||
               b.cx + b.w / 2 > 1 || b.cy + b.h / 2 > 1);
    it.subEl.textContent = e ? "⚠越界 " : "";
    const v = opts.verifiedIds.has(b.id);
    // R3：✓ 核验标记仅 T1 核心类显示；T2 与 T1 非核心类一律不渲染
    it.vEl.className = (v ? "verified" : "unverified") + (showVerifyMark(b) ? "" : " hidden");
    it.vEl.textContent = v ? "✓" : "○";
    it.div.classList.toggle("selected", b.id === selectedBoxId);
  }

  /* ✓ 标记可见性：仅 T1 任务且框属于核心类别（R3） */
  function showVerifyMark(b) {
    const t = opts.task;
    return !!(t && t.kind === "t1" && t.class_ids.includes(b.class_id));
  }

  function buildItem(b) {
    const div = document.createElement("div");
    div.className = "crop-item" + (b.id === selectedBoxId ? " selected" : "");
    div.dataset.boxId = b.id;

    const cv = document.createElement("canvas");
    cv.width = SIZE; cv.height = SIZE;
    drawCrop(cv, b);
    div.appendChild(cv);

    const meta = document.createElement("div");
    meta.className = "cmeta";
    const c = classById[b.class_id];
    const name = document.createElement("div");
    name.className = "cname";
    name.style.color = c && c.color ? c.color : cssVar("--text", "#E5E7EB");
    name.textContent = (c ? c.name : "?") + (b.is_new ? " ✨新" : "");
    meta.appendChild(name);
    const sub = document.createElement("div");
    sub.className = "csub";
    meta.appendChild(sub);

    const v = document.createElement("span");
    v.title = "标记框已核验（点击切换）";
    v.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (callbacks.onToggleVerified) callbacks.onToggleVerified(b.id);
    });
    meta.appendChild(v);

    // T1 快速降级按钮
    const task = opts.task;
    if (task && task.degrade && task.degrade.length) {
      const dg = document.createElement("div");
      dg.className = "degrade-btns";
      for (const tcid of task.degrade) {
        const tc = classById[tcid];
        if (!tc) continue;
        const btn = document.createElement("button");
        btn.textContent = "↓" + tc.name;
        btn.title = `快速降级为 ${tc.name}`;
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (callbacks.onDegrade) callbacks.onDegrade(b.id, tcid);
        });
        dg.appendChild(btn);
      }
      meta.appendChild(dg);
    }

    div.appendChild(meta);

    div.addEventListener("click", () => {
      selectedBoxId = b.id;
      for (const [id, it] of itemMap) {
        it.div.classList.toggle("selected", id === b.id);
      }
      if (callbacks.onPick) callbacks.onPick(b.id);
    });

    gridEl.appendChild(div);

    const it = { div, canvas: cv, nameEl: name, subEl: sub, vEl: v,
                 sig: sigOf(b) };
    updateItem(it, b);   // 统一初始化 ✓/越界/选中状态
    return it;
  }

  function drawCrop(cv, b) {
    const g = cv.getContext("2d");
    g.fillStyle = "#000";
    g.fillRect(0, 0, SIZE, SIZE);
    if (!img) return;
    const bw = b.w * img.width, bh = b.h * img.height;
    const bx = (b.cx - b.w / 2) * img.width, by = (b.cy - b.h / 2) * img.height;
    // 外扩 1.8 倍，钳制到图像
    const sw = Math.min(img.width, bw * PAD_RATIO);
    const sh = Math.min(img.height, bh * PAD_RATIO);
    let sx = bx + bw / 2 - sw / 2;
    let sy = by + bh / 2 - sh / 2;
    sx = Math.max(0, Math.min(sx, img.width - sw));
    sy = Math.max(0, Math.min(sy, img.height - sh));
    // 等比适配 96×96（letterbox）
    const k = Math.min(SIZE / sw, SIZE / sh);
    const dw = sw * k, dh = sh * k;
    const dx = (SIZE - dw) / 2, dy = (SIZE - dh) / 2;
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = "high";
    g.drawImage(img.bitmap, sx, sy, sw, sh, dx, dy, dw, dh);
    // 框位置叠加
    const c = classById[b.class_id];
    g.strokeStyle = c && c.color ? c.color : cssVar("--accent", "#3B82F6");
    g.lineWidth = 1.5;
    const fx = dx + (bx - sx) * k, fy = dy + (by - sy) * k;
    const fw = bw * k, fh = bh * k;
    g.strokeRect(fx, fy, fw, fh);
    if (b.is_new) {
      g.fillStyle = cssVar("--changed", "#FB923C");
      g.fillRect(fx, fy, 7, 7);
    }
  }

  function setSelected(id) {
    selectedBoxId = id;
    for (const [bid, it] of itemMap) {
      it.div.classList.toggle("selected", bid === id);
    }
  }

  /* 可见框列表：T1 只含任务关注类别；T2 全部框且重点类（focus）排前。轮播/定位/面板共用此顺序 */
  function visibleBoxes() {
    const task = opts.task;
    let arr = boxes.filter(b => !task || !task.class_ids.length || task.class_ids.includes(b.class_id));
    if (task && task.kind === "t2") {
      arr = arr.slice().sort((a, b) => {
        const fa = classById[a.class_id] && classById[a.class_id].focus ? 0 : 1;
        const fb = classById[b.class_id] && classById[b.class_id].focus ? 0 : 1;
        return fa - fb;
      });
    }
    return arr;
  }

  /* Tab：下一个未核验框（当前任务过滤范围内） */
  function nextUnverified() {
    for (const b of visibleBoxes()) {
      if (!opts.verifiedIds.has(b.id)) return b;
    }
    return null;
  }

  return {
    init, render, setClasses, setSelected, nextUnverified,
    get shownBoxIds() { return visibleBoxes().map(b => b.id); },
  };
})();
