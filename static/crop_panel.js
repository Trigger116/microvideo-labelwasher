/* crop_panel.js —— 裁剪缩略面板：纯前端 96×96 裁剪、T1 过滤、快速降级、✓ 核验标记 */
"use strict";

const CropPanel = (() => {
  const SIZE = 96;      // 裁剪画布边长
  const PAD_RATIO = 1.8; // 框外扩倍数

  let gridEl, toolbarEl;
  let callbacks = {};
  let img = null, boxes = [], classes = [], classById = {};
  let opts = { task: null, verifiedIds: new Set() };
  let selectedBoxId = null;

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
    img = im; boxes = boxArr; opts = o || opts;
    if (clsArr) setClasses(clsArr);
    gridEl.innerHTML = "";

    const shown = visibleBoxes();
    if (!shown.length) {
      gridEl.innerHTML = `<div class="crop-empty">${
        opts.task ? "本图无目标类别框，按 Tab 跳到下一图" : "本图无标注框"}</div>`;
      return;
    }
    for (const b of shown) {
      gridEl.appendChild(buildItem(b));
    }
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
    name.style.color = c && c.color ? c.color : "#e2e8f0";
    name.textContent = (c ? c.name : "?") + (b.is_new ? " ✨新" : "");
    meta.appendChild(name);
    const sub = document.createElement("div");
    sub.className = "csub";
    const e = (b.cx - b.w / 2 < 0 || b.cy - b.h / 2 < 0 ||
               b.cx + b.w / 2 > 1 || b.cy + b.h / 2 > 1);
    sub.textContent = e ? "⚠越界 " : "";
    meta.appendChild(sub);

    const v = document.createElement("span");
    v.className = opts.verifiedIds.has(b.id) ? "verified" : "unverified";
    v.textContent = opts.verifiedIds.has(b.id) ? "✓" : "○";
    v.title = opts.verifiedIds.has(b.id) ? "已核验（点击取消）" : "标记框已核验（点击切换）";
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
      for (const it of gridEl.querySelectorAll(".crop-item")) {
        it.classList.toggle("selected", it.dataset.boxId === b.id);
      }
      if (callbacks.onPick) callbacks.onPick(b.id);
    });
    return div;
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
    g.strokeStyle = c && c.color ? c.color : "#4f8cff";
    g.lineWidth = 1.5;
    const fx = dx + (bx - sx) * k, fy = dy + (by - sy) * k;
    const fw = bw * k, fh = bh * k;
    g.strokeRect(fx, fy, fw, fh);
    if (b.is_new) {
      g.fillStyle = "#fb923c";
      g.fillRect(fx, fy, 7, 7);
    }
  }

  function setSelected(id) {
    selectedBoxId = id;
    for (const it of gridEl.querySelectorAll(".crop-item")) {
      it.classList.toggle("selected", it.dataset.boxId === id);
    }
  }

  /* 可见框列表：T1 只含任务关注类别；T2 全部框且重点类（focus）排前。轮播/特写/面板共用此顺序 */
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
