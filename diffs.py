# -*- coding: utf-8 -*-
"""框 diff 与变更统计：按稳定框 id 对齐 orig/current。"""


def _geom(b):
    return {k: b[k] for k in ("cx", "cy", "w", "h")}


def _same_geom(a, b, eps):
    return (abs(a["cx"] - b["cx"]) <= eps and abs(a["cy"] - b["cy"]) <= eps
            and abs(a["w"] - b["w"]) <= eps and abs(a["h"] - b["h"]) <= eps)


def diff_boxes(orig_boxes, cur_boxes, move_eps=0.0005):
    """返回变更明细与统计。

    orig/cur 均带 id。结果：
      changed_class:  [{"id","class_from","class_to"}]
      moved:          [{"id","from":{cx,cy,w,h},"to":{...}}]  中心点变化（|dcx| 或 |dcy| > eps）
      boundary_adjust:[{"id","from":{...},"to":{...}}]        仅尺寸变化（中心点固定，|dcx|、|dcy| ≤ eps）
      added:          [box]
      removed:        [box]
      counts:         {"class_changed":n,"moved":n,"boundary_adjust":n,"added":n,"removed":n,"total_diff":n}
      has_changes:    bool
      conversions:    {(class_from,class_to): n}

    语义：class_changed 与几何类别独立计数（同框可计两类，与历史口径一致）；
    moved 与 boundary_adjust 互斥（中心点是否变化）。
    """
    oid = {b["id"]: b for b in orig_boxes}
    cid = {b["id"]: b for b in cur_boxes}
    changed_class, moved, boundary_adjust, added, removed = [], [], [], [], []
    conversions = {}
    for bid, cb in cid.items():
        ob = oid.get(bid)
        if ob is None:
            added.append(cb)
            continue
        if ob["class_id"] != cb["class_id"]:
            changed_class.append({"id": bid, "class_from": ob["class_id"], "class_to": cb["class_id"]})
            k = (ob["class_id"], cb["class_id"])
            conversions[k] = conversions.get(k, 0) + 1
        if not _same_geom(ob, cb, move_eps):
            rec = {"id": bid, "from": _geom(ob), "to": _geom(cb)}
            if abs(ob["cx"] - cb["cx"]) <= move_eps and abs(ob["cy"] - cb["cy"]) <= move_eps:
                boundary_adjust.append(rec)
            else:
                moved.append(rec)
    for bid, ob in oid.items():
        if bid not in cid:
            removed.append(ob)
    n = len(changed_class) + len(moved) + len(boundary_adjust) + len(added) + len(removed)
    return {
        "changed_class": changed_class,
        "moved": moved,
        "boundary_adjust": boundary_adjust,
        "added": added,
        "removed": removed,
        "counts": {"class_changed": len(changed_class), "moved": len(moved),
                   "boundary_adjust": len(boundary_adjust),
                   "added": len(added), "removed": len(removed), "total_diff": n},
        "has_changes": n > 0,
        "conversions": conversions,
    }


def diff_events(orig_boxes, cur_boxes, move_eps=0.0005):
    """逐框变更事件（CSV 明细列用）。

    与 diff_boxes 同源对齐；顺序确定性：当前框序的 added/~ 在前，orig 序的 removed 在后。
    事件: {"id", "type": added|removed|class_changed|boundary_adjust|moved,
           "class_id"（增删）, "class_from"/"class_to"（改类）, "from"/"to"（几何）}
    一条框可有多条事件（class_changed 与几何类别可叠加）。
    """
    oid = {b["id"]: b for b in orig_boxes}
    cid = {b["id"]: b for b in cur_boxes}
    events = []
    for bid, cb in cid.items():
        ob = oid.get(bid)
        if ob is None:
            events.append({"id": bid, "type": "added", "class_id": cb["class_id"], "to": _geom(cb)})
            continue
        if ob["class_id"] != cb["class_id"]:
            events.append({"id": bid, "type": "class_changed",
                           "class_from": ob["class_id"], "class_to": cb["class_id"]})
        if not _same_geom(ob, cb, move_eps):
            if abs(ob["cx"] - cb["cx"]) <= move_eps and abs(ob["cy"] - cb["cy"]) <= move_eps:
                events.append({"id": bid, "type": "boundary_adjust", "from": _geom(ob), "to": _geom(cb)})
            else:
                events.append({"id": bid, "type": "moved", "from": _geom(ob), "to": _geom(cb)})
    for bid, ob in oid.items():
        if bid not in cid:
            events.append({"id": bid, "type": "removed", "class_id": ob["class_id"], "from": _geom(ob)})
    return events


def boxes_out_of_bounds(boxes, width, height, tol_px=2):
    """返回越界框列表（带像素容差，剔除取整噪声）。"""
    if width <= 0 or height <= 0:
        return []
    tx, ty = tol_px / width, tol_px / height
    out = []
    for b in boxes:
        if (b["cx"] - b["w"] / 2 < -tx or b["cy"] - b["h"] / 2 < -ty
                or b["cx"] + b["w"] / 2 > 1 + tx or b["cy"] + b["h"] / 2 > 1 + ty):
            out.append(b)
    return out


def clamp_box(box):
    """将框钳制到 [0,1] 内（导入时的越界噪声修正）。"""
    cx, cy, w, h = box["cx"], box["cy"], box["w"], box["h"]
    x1, y1 = cx - w / 2, cy - h / 2
    x2, y2 = cx + w / 2, cy + h / 2
    x1c, y1c = max(0.0, x1), max(0.0, y1)
    x2c, y2c = min(1.0, x2), min(1.0, y2)
    if x2c <= x1c or y2c <= y1c:
        return None  # 完全越界的废框
    return {"cx": (x1c + x2c) / 2, "cy": (y1c + y2c) / 2,
            "w": x2c - x1c, "h": y2c - y1c}
