# -*- coding: utf-8 -*-
"""任务生成与进度派生。

T1（框级类别核验）：由 config.t1_tasks 定义，任务图集 = 含目标类当前框的图。
T2（全图漏标扫视）：优先读取 batch_dir 下批次清单文件（与人工流程同源）；
  缺省时按 t2_rules（低框数/高密度/批大小）自算批次。

v2 per-task 状态：图状态/备注按任务槽位存储（ws["task_states"]），
  ensure_task_states 负责 v1 工作区的一次性迁移。
"""
import glob
import os
import shutil

from workspace import TERMINAL_STATUSES, get_task_state


def _class_names(ws):
    return {c["id"]: c for c in ws["package"]["classes"]}


def _task_image_ids(ws, task):
    """T1：返回 [(img_id, target_box_ids)]；T2：由批次清单给定 img_id 列表。"""
    target_class_ids = set(task.get("class_ids") or [])
    if task["kind"] == "t2":
        return task["_image_ids"], None
    pairs = []
    for im in ws["images"]:
        tids = [b["id"] for b in im["boxes"] if b["class_id"] in target_class_ids]
        if tids:
            pairs.append((im["id"], tids))
    return pairs, None


def all_tasks_flat(ws):
    """生成全部任务（T1+T2），不写任何索引进 ws。"""
    return gen_t1_tasks(ws) + gen_t2_tasks(ws)


def task_membership(ws):
    """img_id → 所属任务 id 列表（T1 按框归属 + T2 按批次清单）。"""
    mem = {}
    for t in all_tasks_flat(ws):
        pairs, _ = _task_image_ids(ws, t)
        if t["kind"] == "t2":
            for img_id in pairs:
                mem.setdefault(img_id, []).append(t["id"])
        else:
            for img_id, _ in pairs:
                mem.setdefault(img_id, []).append(t["id"])
    return mem


def ensure_task_states(ws):
    """v1→v2 迁移（幂等）：旧全局终态状态复制到该图所属每个 T1 任务槽位；
    T2 槽位只建空 dict（全新开始）。返回是否发生了实际迁移。"""
    if ws.get("task_states_migrated"):
        return False
    ws.setdefault("task_states", {})
    ts = ws["task_states"]
    # 迁移前把原 workspace.json 备份一份（只读保护，防迁移异常破坏现有工作区）
    if ws.get("version", 1) < 2 and ws.get("_dir"):
        src = os.path.join(ws["_dir"], "workspace.json")
        bak = os.path.join(ws["_dir"], "workspace.pre_v2.json")
        if os.path.exists(src) and not os.path.exists(bak):
            shutil.copy2(src, bak)
    img_by_id = {im["id"]: im for im in ws["images"]}
    for t in gen_t1_tasks(ws):
        slot = ts.setdefault(t["id"], {})
        pairs, _ = _task_image_ids(ws, t)
        for img_id, _ in pairs:
            im = img_by_id[img_id]
            if im["status"] in TERMINAL_STATUSES and img_id not in slot:
                slot[img_id] = {"status": im["status"], "note": im.get("note", "")}
    for t in gen_t2_tasks(ws):
        ts.setdefault(t["id"], {})
    ws["version"] = 2
    ws["task_states_migrated"] = True
    return True


def gen_t1_tasks(ws):
    cfg = ws.get("config") or {}
    names = _class_names(ws)
    tasks = []
    for t in cfg.get("t1_tasks") or []:
        label = " / ".join(names[c]["zh"] for c in t["class_ids"] if c in names)
        tasks.append({
            "id": t["id"], "kind": "t1", "name": t.get("name", label),
            "zh": label,
            "banner": t.get("banner", ""),
            "criteria": t.get("criteria", ""),
            "class_ids": list(t["class_ids"]),
            "degrade": list(t.get("degrade") or []),
        })
    return tasks


def gen_t2_tasks(ws):
    cfg = ws.get("config") or {}
    rules = cfg.get("t2_rules") or {}
    batch_dir = ws.get("batch_dir")
    tasks = []
    if batch_dir and os.path.isdir(batch_dir):
        files = sorted(glob.glob(os.path.join(batch_dir, "*.txt")))
        for f in files:
            ids = _batch_file_to_image_ids(ws, f)
            if not ids:
                continue
            tasks.append({
                "id": "t2-" + os.path.splitext(os.path.basename(f))[0],
                "kind": "t2", "name": os.path.splitext(os.path.basename(f))[0],
                "banner": "", "criteria": "", "class_ids": [], "degrade": [],
                "batch_file": f, "_image_ids": ids,
            })
        if tasks:
            return tasks
    # 自算批次
    return _auto_t2_tasks(ws, rules)


def _batch_file_to_image_ids(ws, path):
    rel_map = {}
    for im in ws["images"]:
        rel_map[im["rel_image"].replace("\\", "/")] = im["id"]
    ids = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rel = line.split("\t")[0].strip().replace("\\", "/")
            if rel in rel_map:
                ids.append(rel_map[rel])
    return ids


def _auto_t2_tasks(ws, rules):
    low_max = int(rules.get("low_box_max", 3))
    high_min = int(rules.get("high_box_min", 30))
    batch_size = int(rules.get("batch_size", 50))
    low, high, rest = [], [], []
    for im in ws["images"]:
        n = im["orig_box_count"]
        if n <= low_max:
            low.append(im["id"])
        elif n > high_min:
            high.append(im["id"])
        else:
            rest.append(im["id"])
    tasks = []
    if low:
        tasks.append({"id": "t2-auto-low", "kind": "t2", "name": f"低框数·细扫（≤{low_max}框）",
                      "banner": "标得越少越可能漏标，逐图细扫", "criteria": "",
                      "class_ids": [], "degrade": [], "_image_ids": low})
    if high:
        tasks.append({"id": "t2-auto-high", "kind": "t2", "name": f"高密度·细扫（>{high_min}框）",
                      "banner": "框多易漏，重点查边缘截断目标与小目标", "criteria": "",
                      "class_ids": [], "degrade": [], "_image_ids": high})
    for i in range(0, len(rest), batch_size):
        chunk = rest[i:i + batch_size]
        tasks.append({"id": f"t2-auto-{i // batch_size + 1}", "kind": "t2",
                      "name": f"常规·快扫 {i // batch_size + 1}（{len(chunk)}图）",
                      "banner": "", "criteria": "",
                      "class_ids": [], "degrade": [], "_image_ids": chunk})
    return tasks


def task_progress(ws, task, img_by_id=None):
    pairs, _ = _task_image_ids(ws, task)
    img_by_id = img_by_id or {im["id"]: im for im in ws["images"]}
    if task["kind"] == "t2":
        ids = pairs
        terminal = sum(1 for i in ids
                       if get_task_state(ws, task["id"], i)["status"] in TERMINAL_STATUSES)
        return {"total_images": len(ids), "terminal_images": terminal,
                "total_boxes": 0, "verified_boxes": 0}
    target_class_ids = set(task["class_ids"])
    total_boxes = verified_boxes = 0
    terminal = 0
    for img_id, tids in pairs:
        im = img_by_id[img_id]
        vb = set(im.get("verified_box_ids") or [])
        for b in im["boxes"]:
            if b["class_id"] in target_class_ids:
                total_boxes += 1
                if b["id"] in vb:
                    verified_boxes += 1
        if get_task_state(ws, task["id"], img_id)["status"] in TERMINAL_STATUSES:
            terminal += 1
    return {"total_images": len(pairs), "terminal_images": terminal,
            "total_boxes": total_boxes, "verified_boxes": verified_boxes}


def all_tasks(ws):
    """返回 {t1:[...], t2:[...]}，每个任务带进度统计。不写任何索引进 ws。"""
    img_by_id = {im["id"]: im for im in ws["images"]}
    out = {"t1": [], "t2": []}
    for t in gen_t1_tasks(ws):
        t["progress"] = task_progress(ws, t, img_by_id)
        out["t1"].append(t)
    for t in gen_t2_tasks(ws):
        t["progress"] = task_progress(ws, t, img_by_id)
        out["t2"].append(t)
    return out


def task_images(ws, task_id, offset=0, limit=50):
    """任务图队列：未终态图排前（per-task 槽位视角）。返回 {total, images:[...]}。"""
    img_by_id = {im["id"]: im for im in ws["images"]}
    tasks = all_tasks_flat(ws)
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise KeyError(f"任务不存在: {task_id}")
    pairs, _ = _task_image_ids(ws, task)
    if task["kind"] == "t2":
        rows = [(pid, []) for pid in pairs]
    else:
        rows = pairs
    rows.sort(key=lambda r: 0 if get_task_state(ws, task_id, r[0])["status"] in TERMINAL_STATUSES else 1)
    total = len(rows)
    page = rows[offset:offset + limit]
    out = []
    for img_id, tids in page:
        im = img_by_id[img_id]
        st = get_task_state(ws, task_id, img_id)
        # 框级核验进度：仅 T1（任务目标框中已核验数）；T2 无目标框概念
        if task["kind"] == "t2":
            verified_target_count = None
        else:
            vb = set(im.get("verified_box_ids") or [])
            verified_target_count = sum(1 for bid in tids if bid in vb)
        out.append({
            "img_id": img_id, "name": im["name"], "split": im["split"],
            "box_count": len(im["boxes"]),
            "target_box_ids": tids,
            "target_count": len(tids),
            "verified_target_count": verified_target_count,
            "status": st["status"], "note": st["note"],
            "has_changes": _img_changed(im),
        })
    return {"total": total, "images": out}


def _img_changed(im):
    return any(b.get("changed") or b.get("is_new") for b in im["boxes"]) or \
        len(im["boxes"]) != len(im["orig_boxes"])
