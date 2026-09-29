/* app.js —— 主状态机：包管理 → 工作区 → 审查（任务/图片/编辑/保存）→ 导出
 *
 * 状态：PACKAGES → SCANNING → WORKSPACES → REVIEW → EXPORTING
 * 保存管线：dirty + 500ms 防抖 → PUT boxes；note 独立 800ms 防抖 → PUT state；
 * 切图/导出前强制 flush。撤销：每次编辑开始前记录已保存状态快照。
 */
"use strict";

const App = (() => {
  const $ = (sel) => document.querySelector(sel);

  /* ============ 状态 ============ */
  const state = {
    view: "packages",
    fsPath: "",
    scanDetail: null,        // 扫描结果
    fsToken: 0,              // 浏览/扫描竞态令牌
    wsId: null, wsMeta: null,
    taskId: null, task: null,
    queue: [], queueIdx: -1,
    image: null,             // {img, boxes, orig_boxes, status, note, verified_box_ids, has_changes}
    dirty: false, noteDirty: false,
    saving: false, noteSaving: false,
    boxSaveTimer: null, noteSaveTimer: null,
    undoStack: [], redoStack: [],
    lastNewClass: 2,         // N 新建框默认类
    prefetchToken: 0,
  };
  const CFG = { autosaveMs: 500, noteMs: 800, undoLimit: 50 };

  /* ============ Toast / Modal 工具 ============ */
  function toast(msg, kind) {
    const root = $("#toast-root");
    const div = document.createElement("div");
    div.className = "toast " + (kind || "");
    div.textContent = msg;
    root.appendChild(div);
    setTimeout(() => { div.style.opacity = "0"; div.style.transition = "opacity .3s"; }, 2600);
    setTimeout(() => div.remove(), 3000);
  }
  function closeModal() { $("#modal-root").innerHTML = ""; }
  function openModal(html) {
    const root = $("#modal-root");
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal">${html}</div>`;
    back.addEventListener("click", (e) => { if (e.target === back) closeModal(); });
    root.appendChild(back);
    return back;
  }

  /* ============ 视图路由 ============ */
  function setView(v) {
    state.view = v;
    for (const tab of document.querySelectorAll("#view-tabs .tab")) {
      tab.classList.toggle("active", tab.dataset.view === v);
    }
    $("#view-packages").classList.toggle("hidden", v !== "packages");
    $("#view-review").classList.toggle("hidden", v !== "review");
    $("#view-export").classList.toggle("hidden", v !== "export");
    if (v === "export") refreshReport();
  }
  function updateTabs() {
    const hasWs = !!state.wsId;
    document.querySelector('#view-tabs .tab[data-view="review"]').disabled = !hasWs;
    document.querySelector('#view-tabs .tab[data-view="export"]').disabled = !hasWs;
    $("#ws-indicator").textContent = hasWs
      ? `工作区 ${state.wsId} · ${state.wsMeta ? state.wsMeta.package.name : ""}`
      : "未打开工作区";
    $("#btn-close-ws").classList.toggle("hidden", !hasWs);
  }

  /* ============ 包管理 ============ */
  async function browseFs(path) {
    const token = ++state.fsToken;
    try {
      const r = await Api.post("/api/fs/browse", { path: path || "" });
      if (token !== state.fsToken) return;  // 已被后续浏览/扫描取代
      state.fsPath = r.path;
      $("#fs-path").textContent = r.path;
      const ul = $("#fs-dirs");
      ul.innerHTML = "";
      if (r.parent) {
        const li = document.createElement("li");
        li.innerHTML = `<span>📁 ..</span>`;
        li.addEventListener("click", () => browseFs(r.parent));
        ul.appendChild(li);
      }
      for (const d of r.dirs) {
        const li = document.createElement("li");
        li.innerHTML = `<span>${d.is_pkg ? "📦" : "📁"} ${d.name}</span>${
          d.is_pkg ? `<span class="arrow">→ 数据包</span>` : ""}`;
        li.dataset.path = d.name;
        li.addEventListener("click", async () => {
          if (d.is_pkg) {
            // 单击数据包 = 直接扫描：root=父目录 + subdir=包名，
            // 输出默认 <root>_done/<subdir>（如 <数据根>_done/<包名>，结构与输入一致）
            const detail = await doScan(r.path, d.name);
            if (detail) showScanResult(detail);
          } else {
            browseFs(d.name ? joinPath(r.path, d.name) : r.path);
          }
        });
        ul.appendChild(li);
      }
      $("#btn-scan").disabled = !r.self_is_pkg;
      $("#scan-result").classList.add("hidden");
    } catch (e) { toast(e.message, "error"); }
  }
  function joinPath(a, b) { return a.endsWith("\\") || a.endsWith("/") ? a + b : a + "\\" + b; }

  async function doScan(root, subdir) {
    try {
      const r = await Api.post("/api/packages/scan", { root, subdir });
      if (r.mode === "candidates") {
        toast(`该目录下发现 ${r.candidates.length} 个数据包，请进入子包`, "warn");
        return null;
      }
      state.scanDetail = r.detail;
      return r.detail;
    } catch (e) { toast(e.message, "error"); return null; }
  }

  function showScanResult(detail) {
    const box = $("#scan-result");
    box.classList.remove("hidden");
    const el = $("#scan-detail");
    const sp = detail.splits || {};
    const spTxt = Object.entries(sp)
      .map(([k, v]) => `${k}:${v.images}图/${v.labels}标签`).join(" · ") || "—";
    el.innerHTML = `
      <div class="kv">
        <dt>数据包</dt><dd>${detail.subdir || "(根目录本身)"} · 图片 ${detail.images.length} 张 · 框 ${detail.box_total} 个 · 类 ${detail.classes.length}</dd>
        <dt>训练/验证</dt><dd>${spTxt}</dd>
        <dt>预警</dt><dd>${detail.warnings.length ? "见下" : "无（扫描干净）"}</dd>
      </div>
      ${detail.warnings.length ? `<div class="warn-box">${detail.warnings.map(w => "⚠ " + w).join("<br>")}</div>` : ""}
      <div class="kv"><dt>默认输出</dt><dd class="mono">${rootName(detail)}_done\\${detail.subdir || ""}</dd></div>`;
  }

  function rootName(detail) { return (detail.root || "").split(/[\\/]/).pop(); }

  async function createWorkspace() {
    if (!state.scanDetail) { toast("请先扫描数据包", "error"); return; }
    const root = state.scanDetail.root, subdir = state.scanDetail.subdir;
    try {
      const r = await Api.post("/api/workspaces", {
        root, subdir,
        batch_dir: $("#batch-dir").value.trim() || undefined,
        output_dir: $("#output-dir").value.trim() || undefined,
      });
      toast(`工作区已创建：${r.id}（${r.stats.total_images} 图，批次目录 ${r.batch_dir || "自算"}）`, "ok");
      await loadWorkspaces();
      await openWorkspace(r.id);
    } catch (e) { toast(e.message, "error"); }
  }

  async function loadWorkspaces() {
    try {
      const r = await Api.get("/api/workspaces");
      const ul = $("#ws-list");
      ul.innerHTML = "";
      if (!r.workspaces.length) {
        ul.innerHTML = `<li class="muted" style="cursor:default">（暂无工作区）</li>`;
        return;
      }
      for (const w of r.workspaces) {
        const li = document.createElement("li");
        const pct = Math.round((w.progress_ratio || 0) * 100);
        li.innerHTML = `
          <span>🗂 ${w.package}/${w.subdir}</span>
          <span class="meta">${w.total_images}图 · ${pct}% · ${w.updated_at}</span>`;
        li.addEventListener("click", () => openWorkspace(w.id));
        const del = document.createElement("button");
        del.className = "ghost"; del.textContent = "🗑"; del.title = "删除工作区";
        del.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm(`删除工作区 ${w.id}？（输入包不受影响）`)) return;
          try {
            await Api.del(`/api/workspaces/${w.id}`);
            if (state.wsId === w.id) closeWorkspace();
            await loadWorkspaces();
            toast("工作区已删除", "ok");
          } catch (err) { toast(err.message, "error"); }
        });
        li.appendChild(del);
        ul.appendChild(li);
      }
    } catch (e) { toast(e.message, "error"); }
  }

  async function openWorkspace(wsId) {
    try {
      const r = await Api.get(`/api/workspaces/${wsId}`);
      state.wsId = wsId; state.wsMeta = r;
      ImageStore.setWorkspace(wsId);
      CanvasView.setClasses(r.classes);
      CropPanel.setClasses(r.classes);
      updateTabs();
      setView("review");
      renderTasks(r.stats, r.tasks);
      // 中断恢复：优先回到上次处理位置（任务 + 图；任务/图已不存在则静默回退）
      const ui = r.ui || {};
      const lastTask = ui.last_task_id
        ? [...r.tasks.t1, ...r.tasks.t2].find(t => t.id === ui.last_task_id) : null;
      if (lastTask) { await setTask(lastTask.id, r.tasks, ui.last_img_id || null); return; }
      // 默认打开第一个未完成任务
      const first = [...r.tasks.t1, ...r.tasks.t2].find(t =>
        (t.kind === "t1" ? t.progress.verified_boxes < t.progress.total_boxes
                         : t.progress.terminal_images < t.progress.total_images));
      if (first) await setTask(first.id, r.tasks);
      else toast("🎉 全部任务已完成", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  async function closeWorkspace() {
    await flush();
    state.wsId = null; state.wsMeta = null;
    state.taskId = null; state.task = null;
    state.queue = []; state.queueIdx = -1;
    state.image = null; state.undoStack = []; state.redoStack = [];
    ImageStore.clear();
    $("#task-list").innerHTML = "";
    $("#task-banner").innerHTML = "";
    $("#crop-grid").innerHTML = "";
    $("#img-name").textContent = "";
    updateTabs();
    setView("packages");
    await loadWorkspaces();
  }

  /* ============ 任务与进度 ============ */
  function renderTasks(stats, tasks) {
    const fill = $("#global-progress-fill");
    const n = stats.total_images;
    fill.style.width = (n ? stats.terminal_images / n * 100 : 0) + "%";
    $("#global-progress-text").textContent = `${stats.terminal_images}/${n}`;

    const ul = $("#task-list");
    ul.innerHTML = "";
    const group = (title, arr, key) => {
      if (!arr.length) return;
      const h = document.createElement("div");
      h.className = "muted"; h.style.cssText = "font-size:12px;margin:8px 0 4px;";
      h.textContent = title;
      ul.appendChild(h);
      for (const t of arr) {
        const done = key === "verified_boxes" ? t.progress.verified_boxes : t.progress.terminal_images;
        const total = key === "verified_boxes" ? t.progress.total_boxes : t.progress.total_images;
        const li = document.createElement("div");
        li.className = "task-item" + (t.id === state.taskId ? " active" : "") + (done >= total ? " done" : "");
        const pct = total ? Math.round(done / total * 100) : 100;
        const lastTaskId = (state.wsMeta && state.wsMeta.ui && state.wsMeta.ui.last_task_id) || null;
        li.innerHTML = `
          <div class="tname">${t.id === lastTaskId ? "📌 " : ""}${t.name}</div>
          <div class="tmeta">${t.kind === "t1"
            ? `框 ${t.progress.verified_boxes}/${t.progress.total_boxes} · 图 ${t.progress.terminal_images}/${t.progress.total_images}`
            : `图 ${t.progress.terminal_images}/${t.progress.total_images}`}</div>
          <div class="tbar"><div class="fill" style="width:${pct}%"></div></div>`;
        li.addEventListener("click", () => setTask(t.id, tasks));
        ul.appendChild(li);
      }
    };
    group("T1 · 框级类别核验", tasks.t1, "verified_boxes");
    group("T2 · 全图漏标扫视", tasks.t2, "terminal_images");
  }

  function normalizeTasks(meta) { // {t1:[],t2:[]}
    return { t1: meta.t1 || [], t2: meta.t2 || [] };
  }

  async function setTask(tid, tasksArg, focusImgId) {
    await flush();
    try {
      const r = await Api.get(`/api/workspaces/${state.wsId}/tasks/${tid}/images?limit=500`);
      const all = tasksArg || normalizeTasks(state.wsMeta.tasks);
      const task = [...all.t1, ...all.t2].find(t => t.id === tid);
      if (!task) throw new Error("任务不存在: " + tid);
      state.taskId = tid; state.task = task;
      state.queue = r.images; state.queueIdx = -1;
      $("#task-banner").innerHTML = `
        <div>🎯 <b>${task.name}</b>
          ${task.banner ? ` — <span class="focus">${task.banner}</span>` : ""}</div>
        ${task.criteria ? `<div class="criteria">判类证据：${task.criteria}</div>` : ""}
        ${task.degrade && task.degrade.length
          ? `<div class="focus">可疑目标可快速降级 ↓（裁剪面板按钮）</div>` : ""}`;
      renderTasks(state.wsMeta.stats, all);
      if (!r.images.length) { toast("该任务无图片", "warn"); return; }
      // 中断恢复：按 img_id 定位（队列排序可能变化，不能用 index）；不在队列则回退第一张
      let idx = 0;
      if (focusImgId) {
        const i = r.images.findIndex(row => row.img_id === focusImgId);
        if (i >= 0) idx = i;
      }
      await openQueueImage(idx);
    } catch (e) { toast(e.message, "error"); }
  }

  async function refreshProgress() {
    try {
      const r = await Api.get(`/api/workspaces/${state.wsId}`);
      state.wsMeta = r;
      renderTasks(r.stats, r.tasks);
      updateTabs();
    } catch (e) { /* 忽略 */ }
  }

  /* ============ 图片加载与导航 ============ */
  function updateNavUI() {
    $("#img-pos").textContent = `${state.queueIdx + 1} / ${state.queue.length}`;
    const row = state.queue[state.queueIdx];
    if (row) {
      $("#img-name").textContent = row.name;
      $("#img-name").title = (state.image ? state.image.note : row.note) || "";
      $("#btn-reset-img").classList.toggle("hidden",
        !(state.image ? state.image.has_changes : row.has_changes));
    }
  }

  /* 状态展示：除"待裁决"外不由人工设置，翻图时自动判定（有差异→已修改 / 无差异→已核验无修改） */
  const STATUS_LABELS = {
    "未复核": "○ 未复核", "已核验无修改": "✅ 已核验无修改",
    "已修改": "✏️ 已修改", "待裁决": "⚖️ 待裁决",
  };
  function updateStatusUI() {
    const st = state.image ? state.image.status : "";
    const el = $("#status-text");
    if (el) {
      el.textContent = state.image ? (STATUS_LABELS[st] || st) : "—";
      el.className = "status-text " + st;
    }
    const arb = $("#btn-arb");
    if (arb) {
      arb.classList.toggle("active", st === "待裁决");
      arb.textContent = st === "待裁决" ? "⚖️ 待裁决（S 取消）" : "⚖️ 待裁决";
    }
    if (state.image) {
      $("#img-note").value = state.image.note || "";
    }
  }

  /* 翻图自动判定：待裁决保留不动（force=true 时强制按差异判定，用于 S 取消待裁决）；其余按差异自动 已修改/已核验无修改 */
  async function autoResolveStatus(force = false) {
    if (!state.image || (state.image.status === "待裁决" && !force)) return;
    const target = state.image.has_changes ? "已修改" : "已核验无修改";
    if (state.image.status === target) return;
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status: target,
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.status = r.status;
      state.image.note = r.note;
      state.image.has_changes = r.has_changes;
      updateStatusUI();
      await refreshProgress();
    } catch (e) { /* 忽略：下次翻图自动重试 */ }
  }

  /* S：待裁决 toggle；取消时按差异自动判定 */
  async function toggleArb() {
    if (!state.image) return;
    await flush();
    if (state.image.status === "待裁决") {
      await autoResolveStatus(true);   // 取消待裁决 → 按差异自动判定
      toast(state.image.status === "待裁决" ? "取消失败，请重试"
        : `已取消待裁决 ⚖️（自动判定：${state.image.status}）`, state.image.status === "待裁决" ? "error" : "ok");
      return;
    }
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status: "待裁决",
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.status = r.status;
      state.image.note = r.note;
      updateStatusUI();
      await refreshProgress();
      toast("图片已标为待裁决 ⚖️（再按 S 取消）", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  async function openQueueImage(idx) {
    if (idx < 0 || idx >= state.queue.length) return;
    await flush();
    await autoResolveStatus();   // 翻走前自动判定上一张状态（待裁决保留）
    const row = state.queue[idx];
    state.queueIdx = idx;
    updateNavUI();
    try {
      const r = await Api.get(`/api/workspaces/${state.wsId}/images/${row.img_id}`);
      state.image = {
        img: r.img, boxes: r.boxes, orig_boxes: r.orig_boxes,
        status: r.status, note: r.note,
        verified_box_ids: r.verified_box_ids, has_changes: r.has_changes,
      };
      const token = ++state.prefetchToken;
      const im = await ImageStore.load(row.img_id, row.name);
      if (token !== state.prefetchToken) return; // 已切走
      const keepFocus = CanvasView.getViewMode() === "focus";
      CanvasView.setImage(im);
      CanvasView.setBoxes(state.image.boxes);
      // 新图就位后再退出定位（旧图 bitmap 已被 ImageStore 回收，不能提前渲染）
      if (keepFocus) CanvasView.setViewMode("full");
      CanvasView.render();
      renderCrops();
      updateStatusUI();
      setBadge("saved");
      saveUiPosition();   // 中断恢复：记录当前任务+图位置（异步静默）
      // 预取下一张
      if (state.queue[idx + 1]) ImageStore.prefetch(state.queue[idx + 1].img_id);
      // 定位轮播保持：新图从第一个轮播框开始（无可见框则回全图）
      if (keepFocus) {
        const ids = CropPanel.shownBoxIds;
        if (ids.length) enterCrop(ids[0]);
        else CanvasView.setViewMode("full");
      }
      // T1：全图模式下自动选中第一个未核验目标框
      if (CanvasView.getViewMode() !== "focus" && state.task && state.task.kind === "t1") {
        const vb = new Set(state.image.verified_box_ids);
        const t = state.image.boxes.find(b =>
          state.task.class_ids.includes(b.class_id) && !vb.has(b.id));
        if (t) { CanvasView.setSelected(t.id); CropPanel.setSelected(t.id); }
      }
    } catch (e) { toast(e.message, "error"); }
  }

  /* 保存上次处理位置（中断恢复用）；失败静默，下次翻图会重试 */
  function saveUiPosition() {
    if (!state.wsId || !state.taskId || !state.image) return;
    fetch(`/api/workspaces/${state.wsId}/ui`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_task_id: state.taskId, last_img_id: state.image.img.id }),
      keepalive: true,
    }).catch(() => {});
  }

  async function navImage(delta) {
    const ni = state.queueIdx + delta;
    if (ni < 0) { toast("已是任务第一张", "warn"); return; }
    if (ni >= state.queue.length) {
      toast("本任务已到末尾 — 可切换下一任务或刷新", "warn");
      return;
    }
    await openQueueImage(ni);
  }

  async function jumpImage() {
    openModal(`
      <h3>跳转图片（任务内序号 1-${state.queue.length}）</h3>
      <input id="jump-input" type="number" min="1" max="${state.queue.length}" value="${state.queueIdx + 1}">
      <div class="modal-btns">
        <button class="ghost" onclick="App.closeModal()">取消</button>
        <button class="primary" id="jump-ok">跳转</button>
      </div>`);
    $("#jump-ok").addEventListener("click", async () => {
      const v = parseInt($("#jump-input").value, 10);
      closeModal();
      if (v >= 1 && v <= state.queue.length) await openQueueImage(v - 1);
      else toast("序号超出范围", "error");
    });
  }

  function renderCrops() {
    if (!state.image) return;
    CropPanel.render(
      ImageStore.current, state.image.boxes, null,
      { task: state.task, verifiedIds: new Set(state.image.verified_box_ids) });
  }

  /* ============ 保存管线 ============ */
  function setBadge(kind) {
    const el = $("#save-badge");
    el.className = "badge " + kind;
    el.textContent = { saved: "已保存", dirty: "未保存", saving: "保存中…" }[kind];
  }
  function scheduleBoxSave() {
    state.dirty = true;
    setBadge("dirty");
    clearTimeout(state.boxSaveTimer);
    state.boxSaveTimer = setTimeout(saveBoxes, CFG.autosaveMs);
  }
  function scheduleNoteSave() {
    state.noteDirty = true;
    setBadge("dirty");
    clearTimeout(state.noteSaveTimer);
    state.noteSaveTimer = setTimeout(saveNote, CFG.noteMs);
  }
  async function flush() {
    clearTimeout(state.boxSaveTimer);
    clearTimeout(state.noteSaveTimer);
    await saveBoxes();
    await saveNote();
  }
  async function saveBoxes() {
    if (!state.dirty || !state.image) return;
    state.dirty = false;
    state.saving = true;
    setBadge("saving");
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/boxes`,
                              { boxes: state.image.boxes });
      state.image.boxes = r.boxes;            // 服务端权威（含新框 id 分配）
      state.image.status = r.status;
      state.image.has_changes = r.has_changes;
      CanvasView.setBoxes(state.image.boxes);
      renderCrops();
      updateStatusUI();
      updateNavUI();
      for (const w of r.warnings || []) toast(w, "warn");
      // 撤销至无差异且无备注 → 自动回"已核验无修改"（该图已看过；翻图时统一自动判定兜底）
      if (!r.has_changes && state.image.status === "已修改" &&
          !($("#img-note").value || "").trim()) {
        const s = await Api.put(
          `/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`,
          { status: "已核验无修改", note: "", verified_box_ids: state.image.verified_box_ids });
        state.image.status = s.status;
        updateStatusUI();
      }
      await refreshProgress();
      setBadge("saved");
    } catch (e) {
      state.dirty = true;
      setBadge("dirty");
      toast("保存失败：" + e.message, "error");
    } finally {
      state.saving = false;
      if (state.dirty) scheduleBoxSave();
    }
  }
  async function saveNote() {
    if (!state.noteDirty || !state.image) return;
    state.noteDirty = false;
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status: state.image.status,
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.note = r.note;
      setBadge(state.dirty ? "dirty" : "saved");
    } catch (e) {
      state.noteDirty = true;
      toast("备注保存失败：" + e.message, "error");
    }
  }

  async function setStatus(status) {
    if (!state.image) return;
    await flush();
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status,
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.status = r.status;
      state.image.note = r.note;
      state.image.has_changes = r.has_changes;
      updateStatusUI();
      updateNavUI();
      await refreshProgress();
      toast(`状态 → ${status}`, "ok");
    } catch (e) {
      toast(e.message, "error");
      if (e.message.includes("已核验无修改")) {
        toast("提示：可先【恢复原框】撤销改动，再标记", "warn");
      }
    }
  }

  async function resetImage() {
    if (!state.image) return;
    if (!confirm("恢复原始标注？当前该图所有框改动将丢弃")) return;
    await flush();
    try {
      const r = await Api.post(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/reset`);
      const img = await Api.get(`/api/workspaces/${state.wsId}/images/${state.image.img.id}`);
      state.image.boxes = img.boxes;
      state.image.status = img.status; state.image.note = img.note;
      state.image.verified_box_ids = img.verified_box_ids;
      state.image.has_changes = img.has_changes;
      CanvasView.setBoxes(state.image.boxes);
      CanvasView.setSelected(null);
      renderCrops();
      updateStatusUI();
      updateNavUI();
      await refreshProgress();
      toast("已恢复原始标注", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  /* ============ 撤销/重做 ============ */
  function pushUndo() {
    if (!state.image) return;
    if (!state.dirty) {   // 记录"编辑前"的已保存状态
      state.undoStack.push(JSON.parse(JSON.stringify(state.image.boxes)));
      if (state.undoStack.length > CFG.undoLimit) state.undoStack.shift();
      state.redoStack = [];
    }
  }
  function onEditStart() { pushUndo(); }
  function onBoxesChanged() { scheduleBoxSave(); }
  function onBoxCreated(box) {
    if (!state.dirty) pushUndo();
    state.lastNewClass = box.class_id;
    scheduleBoxSave();
  }
  function applyUndoRedo(from, to) {
    if (!state.image || !from.length) return;
    const snap = from.pop();
    to.push(JSON.parse(JSON.stringify(state.image.boxes)));
    state.image.boxes = snap;
    CanvasView.setBoxes(snap);
    CanvasView.setSelected(null);
    renderCrops();
    scheduleBoxSave();
  }
  function undo() { applyUndoRedo(state.undoStack, state.redoStack); }
  function redo() { applyUndoRedo(state.redoStack, state.undoStack); }

  /* ============ 框操作 ============ */
  async function changeBoxClass(boxId, classId) {
    const b = state.image.boxes.find(x => x.id === boxId);
    if (!b) return;
    pushUndo();
    b.class_id = classId;
    scheduleBoxSave();
    renderCrops();
    CanvasView.render();
  }

  function deleteBox(boxId) {
    const i = state.image.boxes.findIndex(x => x.id === boxId);
    if (i < 0) return;
    pushUndo();
    state.image.boxes.splice(i, 1);
    CanvasView.setSelected(null);
    renderCrops();
    scheduleBoxSave();
  }

  function classPickModal(boxId, title) {
    const cls = state.wsMeta.classes;
    const cur = state.image.boxes.find(b => b.id === boxId);
    const curId = cur ? cur.class_id : state.lastNewClass;
    openModal(`
      <h3>${title || "选择类别"}（当前：${cls[curId] ? cls[curId].name : "?"}）</h3>
      <div class="class-pick">${cls.map((c, i) =>
        `<button data-cid="${i}" style="border-left-color:${c.color}">${i}·${c.name}${
          c.focus ? " ⭐" : ""}</button>`).join("")}</div>
      <div class="modal-btns"><button class="ghost" onclick="App.closeModal()">取消</button></div>`);
    for (const btn of document.querySelectorAll(".class-pick button")) {
      btn.classList.toggle("selected", +btn.dataset.cid === curId);
      btn.addEventListener("click", async () => {
        closeModal();
        if (boxId) await changeBoxClass(boxId, +btn.dataset.cid);
        else { state.lastNewClass = +btn.dataset.cid; toast(`新框默认类 → ${cls[+btn.dataset.cid].name}`, "ok"); }
      });
    }
  }

  async function toggleVerified(boxId) {
    if (!state.image) return;
    const ids = new Set(state.image.verified_box_ids);
    if (ids.has(boxId)) ids.delete(boxId); else ids.add(boxId);
    state.image.verified_box_ids = [...ids];
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status: state.image.status,
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.note = r.note;
      renderCrops();
      await refreshProgress();
    } catch (e) { toast(e.message, "error"); }
  }

  async function degrade(boxId, targetClassId) {
    await changeBoxClass(boxId, targetClassId);
    await toggleVerified(boxId);
    const tc = state.wsMeta.classes.find(c => c.id === targetClassId);
    toast(`已降级为 ${tc ? tc.name : targetClassId} 并标记核验`, "ok");
  }

  /* 标注模式右键目标框：点击处右侧菜单 = 删除 + 标签变更列表。
     重点类（focus）优先显示易混淆类（confusable），其余类收进"更多"（悬停右侧二级列表）。 */
  function contextMenu(boxId, x, y) {
    document.querySelectorAll(".context-menu").forEach(el => el.remove());
    if (!state.image) return;
    if (CanvasView.getMode() !== "annotate") {
      toast("浏览模式仅查看 — 按空格进入标注模式后右键编辑", "warn");
      return;
    }
    if (!boxId) {
      toast("请右键一个目标框打开菜单（删除 / 改类）", "warn");
      return;
    }
    const cls = state.wsMeta.classes;
    const b = state.image.boxes.find(v => v.id === boxId);
    if (!b) return;
    const c = cls[b.class_id];
    const confIds = (c && c.confusable) || [];
    const conf = confIds.map(cid => cls[cid]).filter(Boolean);   // id → 类对象（cls 按 id 索引）
    const rest = cls.filter(cc => cc.id !== b.class_id && !confIds.includes(cc.id));
    const clsBtn = (cc) => `<button data-cid="${cc.id}" class="${cc.id === b.class_id ? "selected" : ""}"
      style="border-left-color:${cc.color}" title="改为 ${cc.name}">${cc.id}·${cc.name}</button>`;
    const moreBtn = (arr) => `<div class="cm-more">更多 ▸<div class="submenu">${arr.map(clsBtn).join("")}</div></div>`;

    const menu = document.createElement("div");
    menu.className = "context-menu box-menu";
    menu.innerHTML = `
      <div class="cm-title" style="border-left-color:${c.color}">${c.name}${c.focus ? " ⭐" : ""}</div>
      <button id="cm-delete" class="danger">🗑 删除框</button>
      <div class="sep"></div>
      <div class="cm-label">标签变更 →</div>
      <div class="cm-classes">${
        c.focus
          ? conf.map(clsBtn).join("") + (rest.length ? moreBtn(rest) : "")
          : cls.map(clsBtn).join("")
      }</div>`;
    document.body.appendChild(menu);
    // 定位：点击处右侧；放不下则翻到左侧（二级菜单反向）；纵向防出屏
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    let lx = x + 14, ly = y;
    if (lx + mw > window.innerWidth - 8) { lx = x - 14 - mw; menu.classList.add("flip"); }
    if (ly + mh > window.innerHeight - 8) ly = window.innerHeight - mh - 8;
    menu.style.left = Math.max(4, lx) + "px";
    menu.style.top = Math.max(4, ly) + "px";

    const kill = () => menu.remove();
    menu.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      kill();
      if (btn.id === "cm-delete") { deleteBox(boxId); return; }
      if (btn.dataset.cid !== undefined) changeBoxClass(boxId, +btn.dataset.cid);
    });
    const onDocDown = (e) => {
      if (!menu.contains(e.target)) { kill(); document.removeEventListener("pointerdown", onDocDown); }
    };
    document.addEventListener("pointerdown", onDocDown);
  }

  /* ============ 裁剪面板联动 ============ */
  function onPickBox(boxId) {
    if (CanvasView.getViewMode() === "focus") enterCrop(boxId);
    else {
      CanvasView.setSelected(boxId);
      CanvasView.focusBox(boxId, true);
    }
  }

  /* ============ 定位轮播（W 切换 / Q·E 轮播）：视口定位放大到目标框（v1.0.0 式 focusBox），始终渲染整幅图 ============ */
  /* 轮播顺序与裁剪面板一致：T1 只含任务关注类别；T2 全部框且重点类（focus）排前 */
  function cropOrder() { return CropPanel.shownBoxIds; }

  function enterCrop(boxId) {
    const ids = cropOrder();
    const i = Math.max(0, ids.indexOf(boxId));
    CanvasView.setSelected(boxId);
    CropPanel.setSelected(boxId);
    // T1 任务下 E=核验过框，hint 明示；T2/无任务为纯轮播
    const t1 = state.task && state.task.kind === "t1";
    CanvasView.setCropBox(boxId, i + 1, ids.length, t1 ? "E 核验·下一框 · Q 上一框" : "Q/E 上一/下一框");
    CanvasView.setViewMode("focus");
    CanvasView.focusBox(boxId, true);   // 视口定位放大到该框（居中 + 框宽 2.2 倍适配，≤6 倍）
  }

  function toggleCropView() {
    if (!state.image) return;
    if (CanvasView.getViewMode() === "focus") {
      CanvasView.setViewMode("full");
      CanvasView.fit();
      return;
    }
    const ids = cropOrder();
    if (!ids.length) { toast("本图没有可轮播的框", "warn"); return; }
    enterCrop(ids[0]);   // 默认从第一个框开始
  }

  function stepCrop(dir) {
    if (!state.image) return;
    const ids = cropOrder();
    if (!ids.length) return;
    const inFocus = CanvasView.getViewMode() === "focus";
    // T1 定位轮播：E = 当前框处理完毕（核验 ✓，无论是否修改），跳到下一个未核验框
    if (inFocus && dir > 0 && state.task && state.task.kind === "t1") {
      stepCropVerify();
      return;
    }
    let i = ids.indexOf(inFocus ? CanvasView.getCropBox() : CanvasView.getSelected());
    if (i < 0) i = dir > 0 ? -1 : 0;
    i = (i + dir + ids.length) % ids.length;   // 循环轮播
    const id = ids[i];
    if (inFocus) {
      enterCrop(id);
    } else {
      // 全图模式：Q/E 切换选中框并聚焦
      CanvasView.setSelected(id);
      CropPanel.setSelected(id);
      CanvasView.focusBox(id, true);
    }
  }

  /* T1 定位轮播 E：核验当前框（只加不删）→ 跳到下一个未核验框；本图全部核验完 → 自动翻下一图
     并保持定位轮播定位到第一个特写框（图状态自动判定、位置记忆沿用翻图逻辑） */
  async function stepCropVerify() {
    const ids = cropOrder();
    if (!ids.length) return;
    const cur = CanvasView.getCropBox();
    if (!cur || !ids.includes(cur)) return;
    await markVerified(cur);
    const vb = new Set(state.image.verified_box_ids);
    const rest = ids.filter(id => !vb.has(id));
    if (!rest.length) {
      // 本图目标框已全部核验 → 自动翻下一图（navImage 内含状态自动判定/保存/位置记忆/定位轮播保持）
      const prevImg = state.image && state.image.img ? state.image.img.id : null;
      await navImage(1);
      if (state.image && state.image.img && state.image.img.id !== prevImg) {
        toast("本图已核验完毕 ✓ → 下一图", "ok");
      }   // 任务末尾时 navImage 已提示"已到末尾"，此处不重复提示
      return;
    }
    // 从当前框位置向后循环找下一个未核验框
    let i = ids.indexOf(cur), next = null;
    for (let n = 1; n <= ids.length; n++) {
      const cand = ids[(i + n) % ids.length];
      if (!vb.has(cand)) { next = cand; break; }
    }
    if (next) enterCrop(next);
  }

  /* 框级核验（只加不删；撤销核验用裁剪面板 ✓ toggle） */
  async function markVerified(boxId) {
    if (!state.image) return;
    const ids = new Set(state.image.verified_box_ids);
    if (ids.has(boxId)) return;   // 已核验，无需重复保存
    ids.add(boxId);
    state.image.verified_box_ids = [...ids];
    try {
      const r = await Api.put(`/api/workspaces/${state.wsId}/images/${state.image.img.id}/state`, {
        status: state.image.status,
        note: $("#img-note").value,
        verified_box_ids: state.image.verified_box_ids,
      });
      state.image.note = r.note;
      renderCrops();
      await refreshProgress();
    } catch (e) { toast(e.message, "error"); }
  }

  /* ============ 键盘快捷键 ============ */
  function onKey(e) {
    const tag = (document.activeElement || {}).tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || $("#modal-root").children.length) {
      if (e.key === "Escape" && $("#modal-root").children.length) closeModal();
      return;
    }
    if (e.key === "Escape" && document.querySelector(".context-menu")) {
      document.querySelectorAll(".context-menu").forEach(el => el.remove());
      return;
    }
    const k = e.key.toLowerCase();
    const ctrl = e.ctrlKey || e.metaKey;

    if (ctrl && k === "z") { e.preventDefault(); undo(); return; }
    if (ctrl && k === "y") { e.preventDefault(); redo(); return; }
    if (ctrl && k === "s") { e.preventDefault(); flush(); return; }

    if (state.view !== "review" || !state.image) return;

    if (k === "a" || e.key === "ArrowLeft") { navImage(-1); return; }
    if (k === "d" || e.key === "ArrowRight") { navImage(1); return; }
    if (k === "s") { e.preventDefault(); toggleArb(); return; }
    if (k === "w") { toggleCropView(); return; }
    if (k === "q") { stepCrop(-1); return; }
    if (k === "e") { stepCrop(1); return; }
    if (e.key === " ") { e.preventDefault(); toggleMode(); return; }
    if (e.key === "Tab") { e.preventDefault(); nextUnverified(); return; }
    if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); stepSelection(e.key === "ArrowDown" ? 1 : -1); return; }
    if (k === "n") { setMode(true); return; }
    if (e.key === "+" || e.key === "=") { CanvasView.zoomIn(); return; }
    if (e.key === "-") { CanvasView.zoomOut(); return; }
    if (k === "0") { CanvasView.fit(); return; }
    if (k === "f") {
      const w = $("#canvas-wrap");
      if (document.fullscreenElement) document.exitFullscreen();
      else w.requestFullscreen && w.requestFullscreen();
      return;
    }
    if (k === "g") { jumpImage(); return; }
    // 数字键 1-9 改选中框类；Shift+数字设置新框默认类
    const digit = /^[1-9]$/.test(e.key) ? +e.key : null;
    if (digit) {
      const cls = state.wsMeta.classes;
      const cid = digit - 1;
      if (cid >= cls.length) return;
      if (e.shiftKey) {
        state.lastNewClass = cid;
        toast(`新框默认类 → ${cls[cid].name}`, "ok");
      } else {
        const s = CanvasView.getSelected();
        if (s) changeBoxClass(s, cid);
        else { state.lastNewClass = cid; toast(`新框默认类 → ${cls[cid].name}`, "ok"); }
      }
    }
  }

  function setMode(annotate) {
    const isAnnotate = annotate !== undefined ? annotate : CanvasView.getMode() !== "annotate";
    CanvasView.setMode(isAnnotate ? "annotate" : "browse");
    const btn = $("#btn-mode");
    btn.textContent = isAnnotate ? "标注模式" : "浏览模式";
    btn.classList.toggle("annotate", isAnnotate);
  }
  function toggleMode() {
    // 定位轮播中按空格 = 退出定位并直接进入标注模式
    if (CanvasView.getViewMode() === "focus") {
      CanvasView.setViewMode("full");
      setMode(true);
      return;
    }
    setMode();
  }

  async function nextUnverified() {
    // 当前图内下一个未核验目标框
    const task = state.task;
    const vb = new Set(state.image.verified_box_ids);
    const cur = CanvasView.getSelected();
    let started = !cur;
    for (const b of state.image.boxes) {
      if (task && task.kind === "t1" && task.class_ids.length && !task.class_ids.includes(b.class_id)) continue;
      if (vb.has(b.id)) continue;
      if (!started) { if (b.id === cur) started = true; continue; }
      CanvasView.setSelected(b.id);
      CropPanel.setSelected(b.id);
      CanvasView.focusBox(b.id, true);
      return;
    }
    // 跨图
    for (let i = state.queueIdx + 1; i < state.queue.length; i++) {
      const row = state.queue[i];
      const r = await Api.get(`/api/workspaces/${state.wsId}/images/${row.img_id}`);
      const vbs = new Set(r.verified_box_ids);
      const t = r.boxes.find(b =>
        (!task || !task.class_ids.length || task.class_ids.includes(b.class_id)) && !vbs.has(b.id));
      if (t) { await openQueueImage(i); return; }
    }
    toast("当前任务已无未核验框 🎉", "ok");
  }

  function stepSelection(dir) {
    if (CanvasView.getViewMode() === "focus") { stepCrop(dir); return; }
    const ids = cropOrder();
    if (!ids.length) return;
    const cur = CanvasView.getSelected();
    let i = cur ? ids.indexOf(cur) : -1;
    i = Math.max(0, Math.min(ids.length - 1, i + dir));
    const id = ids[i];
    CanvasView.setSelected(id);
    CropPanel.setSelected(id);
  }

  /* ============ 导出 ============ */
  async function refreshReport() {
    if (!state.wsId) return;
    try {
      const r = await Api.get(`/api/workspaces/${state.wsId}/report`);
      renderReport(r);
    } catch (e) { toast(e.message, "error"); }
  }
  function renderReport(r) {
    const el = $("#report-area");
    const conv = r.conversions || [];
    const counts = r.counts || {};
    const html = [];
    html.push(`<div class="report-block">
      <h3>📊 变更统计</h3>
      <table>
        <tr><th>改类</th><th>新增</th><th>删除</th><th>移动</th><th>差异图片</th><th>待裁决</th><th>未复核</th></tr>
        <tr><td>${counts.class_changed ?? 0}</td><td>${counts.added ?? 0}</td><td>${counts.removed ?? 0}</td>
        <td>${counts.moved ?? 0}</td><td>${r.diff_images ?? 0}</td>
        <td>${r.status_counts && r.status_counts["待裁决"] || 0}</td>
        <td>${r.status_counts && r.status_counts["未复核"] || 0}</td></tr>
      </table>
    </div>`);
    if (conv.length) {
      html.push(`<div class="report-block"><h3>🔄 类别转换明细（原类 → 新类）</h3>
        <table><tr><th>原类</th><th>新类</th><th>框数</th></tr>
        ${conv.map(c => `<tr><td>${c.from}</td><td>${c.to}</td><td>${c.n}</td></tr>`).join("")}
        </table></div>`);
    }
    if (r.pending_list && r.pending_list.length) {
      html.push(`<div class="report-block"><h3>⚖️ 待裁决清单（${r.pending_list.length}）</h3>
        <div class="report-pending">${r.pending_list.map(p =>
          `${p.split}/${p.name} — ${p.note || "无备注"}`).join("<br>")}</div></div>`);
    }
    if (r.unreviewed && r.unreviewed.length) {
      html.push(`<div class="report-block"><h3>⚠ 未复核（${r.unreviewed.length}）</h3>
        <div class="report-pending">${r.unreviewed.slice(0, 100).join("<br>")}${
          r.unreviewed.length > 100 ? `<br>… 共 ${r.unreviewed.length} 张` : ""}</div></div>`);
    }
    el.innerHTML = html.join("") || `<div class="muted">暂无数据</div>`;
  }

  async function doExport() {
    if (!state.wsId) return;
    await flush();
    const strict = $("#export-strict").checked;
    if (!strict) {
      const r0 = await Api.get(`/api/workspaces/${state.wsId}/report`);
      const pending = (r0.status_counts || {})["未复核"] || 0;
      if (pending && !confirm(`还有 ${pending} 张未复核，非严格模式将照常导出。继续？`)) return;
    }
    $("#export-overlay").classList.remove("hidden");
    try {
      const r = await Api.post(`/api/workspaces/${state.wsId}/export`, {
        strict, overwrite: false,
      });
      $("#export-output").textContent = "✅ 已导出 → " + r.output_dir;
      renderReport(r.report);
      toast("导出完成：" + r.output_dir, "ok");
      for (const w of r.warnings || []) toast(w, "warn");
    } catch (e) {
      if (e.status === 409 && confirm("输出目录已存在，覆盖？")) {
        try {
          const r = await Api.post(`/api/workspaces/${state.wsId}/export`, {
            strict, overwrite: true,
          });
          $("#export-output").textContent = "✅ 已导出 → " + r.output_dir;
          renderReport(r.report);
          toast("导出完成：" + r.output_dir, "ok");
        } catch (e2) { toast(e2.message, "error"); }
      } else if (e.code === "E_EXPORT" && e.message.includes("未复核")) {
        toast("严格模式拒绝：存在未复核图片。请先完成审核或关闭严格模式", "error");
      } else {
        toast("导出失败：" + e.message, "error");
      }
    } finally {
      $("#export-overlay").classList.add("hidden");
    }
  }

  /* ============ 启动 ============ */
  function bindEvents() {
    document.querySelectorAll("#view-tabs .tab").forEach(t =>
      t.addEventListener("click", () => {
        if (t.dataset.view === "review" && state.wsId) setView("review");
        else if (t.dataset.view === "export" && state.wsId) setView("export");
        else if (t.dataset.view === "packages") setView("packages");
      }));
    $("#btn-close-ws").addEventListener("click", closeWorkspace);
    $("#fs-up").addEventListener("click", async () => {
      const r = await Api.post("/api/fs/browse", { path: state.fsPath });
      if (r.parent) browseFs(r.parent);
    });
    $("#btn-pick").addEventListener("click", async () => {
      try {
        const r = await Api.post("/api/fs/pick");
        if (r.path) browseFs(r.path);
      } catch (e) { toast(e.message, "error"); }
    });
    $("#btn-scan").addEventListener("click", async () => {
      const r = await Api.post("/api/packages/scan", { root: state.fsPath, subdir: "__self__" });
      if (r.mode === "detail") { state.scanDetail = r.detail; showScanResult(r.detail); }
    });
    $("#btn-create-ws").addEventListener("click", createWorkspace);
    $("#btn-prev").addEventListener("click", () => navImage(-1));
    $("#btn-next").addEventListener("click", () => navImage(1));
    $("#btn-crop").addEventListener("click", toggleCropView);
    $("#btn-reset-img").addEventListener("click", resetImage);
    $("#btn-mode").addEventListener("click", toggleMode);
    $("#btn-refresh-report").addEventListener("click", refreshReport);
    $("#btn-export").addEventListener("click", doExport);
    $("#img-note").addEventListener("input", scheduleNoteSave);
    $("#btn-arb").addEventListener("click", toggleArb);
    document.addEventListener("keydown", onKey);
    // 中断恢复兜底：页面关闭/刷新前把当前位置带上（keepalive 同步发出）
    window.addEventListener("beforeunload", () => { saveUiPosition(); });

    // Canvas 回调
    CanvasView.init($("#main-canvas"), $("#canvas-wrap"), {
      onSelectBox: (id) => { if (id !== null) CropPanel.setSelected(id); },
      onBoxesChanged: onBoxesChanged,
      onBoxCreated: onBoxCreated,
      onEditStart: onEditStart,
      onContextMenu: contextMenu,
      onDblClickBox: (id) => classPickModal(id),
      defaultNewClass: () => state.lastNewClass,
    });
    // 裁剪面板回调
    CropPanel.init($("#crop-grid"), $("#crop-toolbar"), {
      onPick: onPickBox,
      onToggleVerified: toggleVerified,
      onDegrade: degrade,
    });
  }

  async function boot() {
    bindEvents();
    await browseFs("");
    await loadWorkspaces();
  }

  return {
    boot, closeModal, setView,
    setStatus, resetImage, changeBoxClass, deleteBox, classPickModal,
    undo, redo, nextUnverified, toggleVerified, degrade, jumpImage, doExport,
  };
})();

document.addEventListener("DOMContentLoaded", () => App.boot());
