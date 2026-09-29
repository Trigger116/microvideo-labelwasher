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
from io_utils import atomic_write_json, read_lines

TERMINAL_STATUSES = ("已核验无修改", "已修改", "待裁决")
ALL_STATUSES = ("未复核",) + TERMINAL_STATUSES
MAX_BACKUPS = 20
BACKUP_EVERY_SAVES = 50

_locks = {}
_locks_guard = threading.Lock()


def _ws_lock(ws_dir):
    with _locks_guard:
        if ws_dir not in _locks:
            _locks[ws_dir] = threading.Lock()
        return _locks[ws_dir]


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
        "version": 1,
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
        "_save_count": 0,
        "_clamp_dropped": clamp_dropped,
    }
    ws_dir = os.path.join(ws_root, ws["id"])
    os.makedirs(ws_dir, exist_ok=True)
    ws["_dir"] = ws_dir
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
    return ws


def save_workspace(ws, ws_dir=None):
    """原子保存 + 滚动备份（每 N 次保存备份一份，保留最近 20 份）。"""
    ws_dir = ws_dir or ws.get("_dir")
    if not ws_dir:
        raise ValueError("save_workspace 缺少工作区目录")
    ws["updated_at"] = time.strftime("%Y%m%d_%H%M%S")
    ws["_save_count"] = ws.get("_save_count", 0) + 1
    os.makedirs(ws_dir, exist_ok=True)
    with _ws_lock(ws_dir):
        atomic_write_json(os.path.join(ws_dir, "workspace.json"), ws)
        if ws["_save_count"] % BACKUP_EVERY_SAVES == 0:
            backup_dir = os.path.join(ws_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(os.path.join(ws_dir, "workspace.json"),
                         os.path.join(backup_dir, f"workspace_{ws['updated_at']}.json"))
            baks = sorted(glob.glob(os.path.join(backup_dir, "workspace_*.json")))
            for old in baks[:-MAX_BACKUPS]:
                try:
                    os.unlink(old)
                except OSError:
                    pass


def get_image(ws, img_id):
    for im in ws["images"]:
        if im["id"] == img_id:
            return im
    raise KeyError(f"图片不存在: {img_id}")


def _image_has_changes(im):
    return any(b.get("changed") or b.get("is_new") for b in im["boxes"]) or \
        len(im["boxes"]) != len(im["orig_boxes"])


def put_boxes(ws, img_id, boxes_in, move_eps=None):
    """全量替换该图当前框（幂等）。服务端分配 id、计算 diff、自动派生状态。

    boxes_in: [{"id":可选,"class_id","cx","cy","w","h"}]（坐标 0-1）
    返回 {"boxes":..., "has_changes":bool, "status":str, "warnings":[...]}
    """
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
        if bid and bid not in seen_ids and any(o["id"] == bid for o in im["orig_boxes"]):
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

    # 状态自动派生
    if diff["has_changes"]:
        if im["status"] == "未复核":
            im["status"] = "已修改"
        elif im["status"] == "已核验无修改":
            im["status"] = "已修改"
            warnings.append("该图已有框改动，状态已由'已核验无修改'自动纠正为'已修改'")
    return {"boxes": new_boxes, "has_changes": diff["has_changes"],
            "status": im["status"], "warnings": warnings}


def put_state(ws, img_id, status, note=None, verified_box_ids=None):
    """设置图片状态/备注/框级核验记录。规则校验见计划。"""
    im = get_image(ws, img_id)
    if status not in ALL_STATUSES:
        raise ValueError(f"非法状态: {status}")
    if note is not None:
        im["note"] = note
    if status == "已核验无修改" and _image_has_changes(im):
        raise ValueError("该图已存在框改动，不能标记为'已核验无修改'；请改为'已修改'或撤销改动")
    im["status"] = status
    if verified_box_ids is not None:
        valid = {b["id"] for b in im["boxes"]}
        im["verified_box_ids"] = [v for v in verified_box_ids if v in valid]
    return {"status": im["status"], "note": im["note"],
            "has_changes": _image_has_changes(im)}


def reset_image(ws, img_id):
    im = get_image(ws, img_id)
    im["boxes"] = copy.deepcopy(im["orig_boxes"])
    for b in im["boxes"]:
        b.update(verified=False, changed=False, is_new=False)
    im["verified_box_ids"] = []
    if im["status"] == "已修改" and not im["note"]:
        im["status"] = "未复核"
    return {"status": im["status"], "boxes": im["boxes"], "has_changes": False}


def ws_stats(ws):
    """进度/状态计数/类别分布（部分缓存可失效重算）。"""
    n = len(ws["images"])
    status_counts = {}
    terminal = 0
    diff_images = 0
    cur_class_counts = {}
    orig_class_counts = {}
    for im in ws["images"]:
        status_counts[im["status"]] = status_counts.get(im["status"], 0) + 1
        if im["status"] in TERMINAL_STATUSES:
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
