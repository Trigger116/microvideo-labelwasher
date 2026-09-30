# -*- coding: utf-8 -*-
"""工作区核心：创建/原子读写/框 diff/状态派生/进度统计/备份。

工作区是唯一可写数据源；输入数据包目录全程只读。
"""
import copy
import glob
import os
import shutil
import threading
import time

from config import load_default_config, merge_config, validate_config, classes_as_map
from diffs import diff_boxes, clamp_box
from io_utils import atomic_write_bytes, read_lines

TERMINAL_STATUSES = ("已核验无修改", "已修改", "待裁决")
ALL_STATUSES = ("未复核",) + TERMINAL_STATUSES
# per-task 状态聚合优先级：任一任务"未复核" → 图未完成；否则 待裁决 > 已修改 > 已核验无修改
AGG_STATUS_ORDER = ("待裁决", "已修改", "已核验无修改")
MAX_BACKUPS = 20
BACKUP_EVERY_SAVES = 50
# 序列化时剔除的内存辅助键（不落盘，防 JSON 体积膨胀）
_NON_SERIALIZED_KEYS = ("_dir", "_mut_seq", "_img_index", "images_by_id")

_locks = {}
_locks_guard = threading.Lock()


def _ws_lock(ws_dir):
    with _locks_guard:
        if ws_dir not in _locks:
            _locks[ws_dir] = threading.Lock()
        return _locks[ws_dir]


def touch(ws):
    """mutation 序号递增（必须在 _ws_lock 内调用；stats 缓存按它失效）。"""
    ws["_mut_seq"] = ws.get("_mut_seq", 0) + 1


def _dumps_workspace(ws):
    """锁内调用：更新时间戳/保存计数并序列化为 bytes（剔除内存辅助键）。"""
    ws["updated_at"] = time.strftime("%Y%m%d_%H%M%S")
    ws["_save_count"] = ws.get("_save_count", 0) + 1
    payload = {k: v for k, v in ws.items() if k not in _NON_SERIALIZED_KEYS}
    import json
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _do_backup(ws_dir, ts):
    """滚动备份（锁外调用，可在后台线程执行）。"""
    backup_dir = os.path.join(ws_dir, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(os.path.join(ws_dir, "workspace.json"),
                 os.path.join(backup_dir, f"workspace_{ts}.json"))
    baks = sorted(glob.glob(os.path.join(backup_dir, "workspace_*.json")))
    for old in baks[:-MAX_BACKUPS]:
        try:
            os.unlink(old)
        except OSError:
            pass


def default_output_dir(root):
    """输出目录默认 = 输入根目录的兄弟：<root>_done。"""
    root = os.path.abspath(root)
    return os.path.join(os.path.dirname(root), os.path.basename(root) + "_done")


def create_workspace(scan, ws_root, config_override=None, batch_dir=None, output_dir=None):
    """由 scan_package 详情创建工作区（读取全部标签为 orig_boxes）。"""
    cfg = load_default_config()
    if config_override:
        cfg = merge_config(cfg, config_override)
    errs = validate_config(cfg)
    if errs:
        raise ValueError("配置校验失败: " + "; ".join(errs))
    cmap = classes_as_map(cfg)
    classes = []
    for c in scan["classes"]:
        ext = cmap.get(c["id"])
        classes.append({
            "id": c["id"], "name": c["name"], "zh": c.get("zh", c["name"]),
            "color": ext["color"] if ext else "#64748B",
            "identity": bool(ext["identity"]) if ext else False,
            "focus": bool(ext["focus"]) if ext else False,
            "confusable": [int(x) for x in (ext.get("confusable") or [])] if ext else [],
        })

    csv_keyed = scan["csv_keyed"]
    images = []
    clamp_dropped = 0
    for i, im in enumerate(scan["images"]):
        label_path = os.path.join(scan["pkg_dir"], im["rel_label"])
        orig_boxes = []
        if os.path.exists(label_path):
            for line in read_lines(label_path):
                parts = line.split()
                if len(parts) < 5:
                    continue
                try:
                    cid = int(float(parts[0]))
                    cx, cy, w, h = (float(v) for v in parts[1:5])
                except ValueError:
                    continue
                box = {"class_id": cid, "cx": cx, "cy": cy, "w": w, "h": h}
                clamped = clamp_box(box)
                if clamped is None:
                    clamp_dropped += 1
                    continue
                clamped["class_id"] = cid
                orig_boxes.append(clamped)
        for bi, b in enumerate(orig_boxes, 1):
            b["id"] = f"b{bi}"
        boxes = copy.deepcopy(orig_boxes)
        for b in boxes:
            b.update(verified=False, changed=False, is_new=False)

        entry = None
        for cand in csv_keyed.get(im["name"], []):
            if cand["split"] == im["split"] or not cand["split"]:
                entry = cand
                break
        status = entry["status"] if entry else "未复核"
        if status not in ALL_STATUSES:
            status = "未复核"
        images.append({
            "id": f"img_{i + 1:06d}",
            "name": im["name"], "split": im["split"],
            "rel_image": im["rel_image"], "rel_label": im["rel_label"],
            "width": im["width"], "height": im["height"],
            "orig_boxes": orig_boxes, "boxes": boxes,
            "orig_box_count": entry["orig_box_count"] if entry else len(orig_boxes),
            "status": status,
            "note": entry["note"] if entry else "",
            "verified_box_ids": [],
        })

    ts = time.strftime("%Y%m%d_%H%M%S")
    ws = {
        "version": 1,           # ensure_task_states 迁移时升为 2（CSV 初始终态一并灌入 T1 槽位）
        "id": f"ws_{ts}",
        "created_at": ts, "updated_at": ts,
        "package": {
            "root": scan["root"], "subdir": scan["subdir"], "pkg_dir": scan["pkg_dir"],
            "name": os.path.basename(os.path.abspath(scan["root"])),
            "output_dir": os.path.abspath(output_dir) if output_dir else default_output_dir(scan["root"]),
            "classes": classes,
            "csv_path": os.path.join(scan["pkg_dir"], "复核记录.csv"),
            "csv_rows": scan["csv_rows"],
            "meta_files": scan["meta_files"],
            "warnings": scan["warnings"],
        },
        "config": cfg,
        "batch_dir": batch_dir,
        "images": images,
        # per-task 状态槽位：{task_id: {img_id: {status, note}}}（v2；图级 status/note 保留为最新值兜底）
        "task_states": {},
        "task_states_migrated": False,
        "ui": {"last_task_id": None, "last_img_id": None},   # 中断恢复：上次处理位置（任务+图片）
        "_save_count": 0,
        "_clamp_dropped": clamp_dropped,
    }
    ws_dir = os.path.join(ws_root, ws["id"])
    os.makedirs(ws_dir, exist_ok=True)
    ws["_dir"] = ws_dir
    from tasks import ensure_task_states   # 延迟 import 防循环；灌入 CSV 初始终态
    ensure_task_states(ws)
    save_workspace(ws, ws_dir)
    return ws


def _ws_dir(ws_root, ws_id):
    return os.path.join(ws_root, ws_id)


def load_workspace(ws_root, ws_id):
    import json

    p = os.path.join(_ws_dir(ws_root, ws_id), "workspace.json")
    with open(p, "r", encoding="utf-8") as f:
        ws = json.load(f)
    ws["_dir"] = _ws_dir(ws_root, ws_id)
    # v1→v2 迁移：旧全局终态状态复制到所属 T1 任务槽位（幂等；发生迁移时立即写盘）
    from tasks import ensure_task_states   # 延迟 import 防循环（tasks 顶层只 import 本模块常量）
    if ensure_task_states(ws):
        save_workspace(ws, ws["_dir"])
    return ws


def save_workspace(ws, ws_dir=None):
    """同步原子保存 + 滚动备份（锁内 dumps 快照、锁外写盘；
    高频保存走 server.SaveScheduler 的异步合并路径）。"""
    ws_dir = ws_dir or ws.get("_dir")
    if not ws_dir:
        raise ValueError("save_workspace 缺少工作区目录")
    os.makedirs(ws_dir, exist_ok=True)
    with _ws_lock(ws_dir):
        payload = _dumps_workspace(ws)
    atomic_write_bytes(os.path.join(ws_dir, "workspace.json"), payload)
    if ws["_save_count"] % BACKUP_EVERY_SAVES == 0:
        _do_backup(ws_dir, ws["updated_at"])
    return payload


def get_image(ws, img_id):
    """按 id 取图（内存索引缓存，img dict 对象在 mutation 中原地更新、引用稳定）。"""
    idx = ws.get("_img_index")
    if idx is None:
        idx = {im["id"]: im for im in ws["images"]}
        ws["_img_index"] = idx
    try:
        return idx[img_id]
    except KeyError:
        raise KeyError(f"图片不存在: {img_id}")


def _image_has_changes(im):
    return any(b.get("changed") or b.get("is_new") for b in im["boxes"]) or \
        len(im["boxes"]) != len(im["orig_boxes"])


def put_boxes(ws, img_id, boxes_in, move_eps=None, task_id=None):
    """全量替换该图当前框（幂等）。服务端分配 id、计算 diff、自动派生状态。

    boxes_in: [{"id":可选,"class_id","cx","cy","w","h"}]（坐标 0-1）
    task_id: 带任务视角时派生状态写入该任务槽位（v2 per-task 状态机）
    返回 {"boxes":..., "has_changes":bool, "status":str, "warnings":[...]}
    """
    with _ws_lock(ws["_dir"]):
        cfg = ws.get("config") or {}
        eps = move_eps if move_eps is not None else cfg.get("move_epsilon", 0.0005)
        im = get_image(ws, img_id)
        warnings = []
        n_classes = len(ws["package"]["classes"])

        seen_ids = set()
        new_boxes = []
        for i, b in enumerate(boxes_in):
            cid = int(b["class_id"])
            if cid < 0 or cid >= n_classes:
                raise ValueError(f"类 ID 越界: {cid}")
            nb = {
                "class_id": cid,
                "cx": max(0.0, min(1.0, float(b["cx"]))),
                "cy": max(0.0, min(1.0, float(b["cy"]))),
                "w": max(0.0, min(1.0, float(b["w"]))),
                "h": max(0.0, min(1.0, float(b["h"]))),
            }
            if nb["w"] <= 0 or nb["h"] <= 0:
                raise ValueError(f"框宽高必须为正: w={nb['w']} h={nb['h']}")
            bid = b.get("id")
            # id 稳定性：orig 框 id 与本次会话已分配的新框 id 都保留（否则 b_new 每次保存换 id，
            # 框级核验记录会失配丢失）
            known_ids = {o["id"] for o in im["orig_boxes"]} | {x["id"] for x in im["boxes"]}
            if bid and bid not in seen_ids and bid in known_ids:
                nb["id"] = bid
            else:
                nb["id"] = f"b_new_{int(time.time() * 1000)}_{i + 1}"
            seen_ids.add(nb["id"])
            new_boxes.append(nb)

        orig = im["orig_boxes"]
        diff = diff_boxes(orig, new_boxes, eps)
        for nb in new_boxes:
            ob = next((o for o in orig if o["id"] == nb["id"]), None)
            nb["is_new"] = ob is None
            nb["verified"] = False if ob is None else nb["id"] in im.get("verified_box_ids", [])
            nb["changed"] = nb["is_new"] or (ob is not None and (
                ob["class_id"] != nb["class_id"] or
                abs(ob["cx"] - nb["cx"]) > eps or abs(ob["cy"] - nb["cy"]) > eps or
                abs(ob["w"] - nb["w"]) > eps or abs(ob["h"] - nb["h"]) > eps))
        im["boxes"] = new_boxes

        # 状态自动派生（v2 带 task_id 时写该任务槽位；图级同步兜底）
        cur_st = get_task_state(ws, task_id, img_id)["status"] if task_id else im["status"]
        if diff["has_changes"]:
            new_st = cur_st
            if cur_st == "未复核":
                new_st = "已修改"
            elif cur_st == "已核验无修改":
                new_st = "已修改"
                warnings.append("该图已有框改动，状态已由'已核验无修改'自动纠正为'已修改'")
            if task_id:
                put_task_state(ws, task_id, img_id, new_st)
                im["status"] = new_st
            elif new_st != im["status"]:
                im["status"] = new_st
        touch(ws)
        return {"boxes": new_boxes, "has_changes": diff["has_changes"],
                "status": get_task_state(ws, task_id, img_id)["status"] if task_id else im["status"],
                "warnings": warnings}


def get_task_state(ws, task_id, img_id):
    """读取某任务下某图的状态（per-task 槽位；缺失 = 未复核）。"""
    slot = (ws.get("task_states") or {}).get(task_id, {})
    st = slot.get(img_id)
    if st:
        return {"status": st.get("status", "未复核"), "note": st.get("note", "")}
    return {"status": "未复核", "note": ""}


def put_task_state(ws, task_id, img_id, status, note=None):
    """写入 per-task 槽位（不触碰图级兜底字段的备注；由 put_state 统一入口）。"""
    slot = ws.setdefault("task_states", {}).setdefault(task_id, {})
    cur = slot.get(img_id) or {}
    slot[img_id] = {"status": status,
                    "note": note if note is not None else cur.get("note", "")}
    return slot[img_id]


def _broadcast_state(ws, img_id, status, note):
    """无 task_id 的旧式写入：同步到该图所属全部任务的槽位（兼容路径）。"""
    from tasks import task_membership   # 延迟 import 防循环
    for tid in task_membership(ws).get(img_id, []):
        put_task_state(ws, tid, img_id, status, note)


def _merge_statuses(statuses):
    """跨任务槽位聚合：任一任务未复核 → 图未完成；否则 待裁决 > 已修改 > 已核验无修改。"""
    if not statuses:
        return None
    if "未复核" in statuses:
        return "未复核"
    for s in AGG_STATUS_ORDER:
        if s in statuses:
            return s
    return "已核验无修改"


def aggregate_states(ws):
    """一次性计算全部图的跨任务聚合状态 {img_id: {"status", "note"}}。
    图不属于任何任务时回退图级最新值。"""
    from tasks import task_membership   # 延迟 import 防循环
    mem = task_membership(ws)
    ts = ws.get("task_states") or {}
    out = {}
    for im in ws["images"]:
        tids = mem.get(im["id"], [])
        if not tids:
            # 图不属于任何任务 → 图级最新值兜底
            out[im["id"]] = {"status": im["status"], "note": im["note"] or ""}
            continue
        raw = [ts.get(tid, {}).get(im["id"]) for tid in tids]
        # 槽位缺失 = 未复核（迁移后 T2 全新开始：旧图级终态不参与该任务）
        status = _merge_statuses([s["status"] if s else "未复核" for s in raw])
        notes = [s.get("note", "").strip() for s in raw if s and s.get("note", "").strip()]
        out[im["id"]] = {"status": status, "note": "；".join(notes)}
    return out


def put_state(ws, img_id, status, note=None, verified_box_ids=None, task_id=None):
    """设置图片状态/备注/框级核验记录。

    v2：带 task_id → 写入该任务槽位（图级 status/note 同步为最新值兜底）；
        不带 task_id → 旧语义：写图级并广播到该图所属全部任务槽位。
    """
    with _ws_lock(ws["_dir"]):
        im = get_image(ws, img_id)
        if status not in ALL_STATUSES:
            raise ValueError(f"非法状态: {status}")
        if status == "已核验无修改" and _image_has_changes(im):
            raise ValueError("该图已存在框改动，不能标记为'已核验无修改'；请改为'已修改'或撤销改动")
        if task_id:
            put_task_state(ws, task_id, img_id, status, note)
            if note is not None:
                im["note"] = note
            im["status"] = status
        else:
            if note is not None:
                im["note"] = note
            im["status"] = status
            _broadcast_state(ws, img_id, status, im["note"] or "")
        if verified_box_ids is not None:
            valid = {b["id"] for b in im["boxes"]}
            im["verified_box_ids"] = [v for v in verified_box_ids if v in valid]
        touch(ws)
        return {"status": im["status"], "note": im["note"],
                "has_changes": _image_has_changes(im)}


def reset_image(ws, img_id, task_id=None):
    """恢复原始标注。带 task_id 时清除该任务槽位状态（回到未复核）。"""
    with _ws_lock(ws["_dir"]):
        im = get_image(ws, img_id)
        im["boxes"] = copy.deepcopy(im["orig_boxes"])
        for b in im["boxes"]:
            b.update(verified=False, changed=False, is_new=False)
        im["verified_box_ids"] = []
        if task_id:
            slot = (ws.setdefault("task_states", {})).get(task_id)
            if slot and img_id in slot:
                del slot[img_id]
        if im["status"] == "已修改" and not im["note"]:
            im["status"] = "未复核"
        touch(ws)
        return {"status": get_task_state(ws, task_id, img_id)["status"] if task_id else im["status"],
                "boxes": im["boxes"], "has_changes": False}


def ws_stats(ws):
    """进度/状态计数/类别分布。v2 工作区用 per-task 聚合口径：
    图"完成" = 其所属全部任务槽位都终态（聚合状态见 _merge_statuses）。"""
    n = len(ws["images"])
    status_counts = {}
    terminal = 0
    diff_images = 0
    cur_class_counts = {}
    orig_class_counts = {}
    agg = aggregate_states(ws) if ws.get("task_states") is not None else None
    for im in ws["images"]:
        st = agg[im["id"]]["status"] if agg else im["status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        if st in TERMINAL_STATUSES:
            terminal += 1
        if _image_has_changes(im):
            diff_images += 1
        for b in im["boxes"]:
            cur_class_counts[b["class_id"]] = cur_class_counts.get(b["class_id"], 0) + 1
        for b in im["orig_boxes"]:
            orig_class_counts[b["class_id"]] = orig_class_counts.get(b["class_id"], 0) + 1
    return {
        "total_images": n,
        "terminal_images": terminal,
        "progress_ratio": round(terminal / n, 4) if n else 0.0,
        "status_counts": status_counts,
        "diff_images": diff_images,
        "cur_class_counts": cur_class_counts,
        "orig_class_counts": orig_class_counts,
        "box_total_current": sum(cur_class_counts.values()),
        "box_total_original": sum(orig_class_counts.values()),
    }


def list_workspaces(ws_root):
    """列出工作区摘要（id/包名/时间/进度）。"""
    out = []
    if not os.path.isdir(ws_root):
        return out
    for name in sorted(os.listdir(ws_root), reverse=True):
        p = os.path.join(ws_root, name, "workspace.json")
        if not os.path.exists(p):
            continue
        try:
            ws = load_workspace(ws_root, name)
            st = ws_stats(ws)
            out.append({
                "id": ws["id"],
                "package": ws["package"]["name"],
                "subdir": ws["package"]["subdir"],
                "created_at": ws["created_at"],
                "updated_at": ws["updated_at"],
                "total_images": st["total_images"],
                "terminal_images": st["terminal_images"],
                "progress_ratio": st["progress_ratio"],
            })
        except Exception:
            continue
    return out


def delete_workspace(ws_root, ws_id):
    ws_dir = _ws_dir(ws_root, ws_id)
    if not os.path.isdir(ws_dir):
        raise FileNotFoundError(f"工作区不存在: {ws_id}")
    with _ws_lock(ws_dir):
        shutil.rmtree(ws_dir)
