# -*- coding: utf-8 -*-
"""框 diff 与变更统计：按稳定框 id 对齐 orig/current。"""


def box_key(b):
    return (round(float(b["class_id"]), 6),
            round(float(b["cx"]), 6), round(float(b["cy"]), 6),
            round(float(b["w"]), 6), round(float(b["h"]), 6))


def _same_geom(a, b, eps):
    return (abs(a["cx"] - b["cx"]) <= eps and abs(a["cy"] - b["cy"]) <= eps
            and abs(a["w"] - b["w"]) <= eps and abs(a["h"] - b["h"]) <= eps)


def diff_boxes(orig_boxes, cur_boxes, move_eps=0.0005):
    """返回变更明细与统计。

    orig/cur 均带 id。结果：
      changed_class: [{"id","class_from","class_to"}]
      moved:         [{"id","from":{cx,cy,w,h},"to":{...}}]
      added:         [box]
      removed:       [box]
      counts:        {"class_changed":n,"moved":n,"added":n,"removed":n,"total_diff":n}
      has_changes:   bool
      conversions:   {(class_from,class_to): n}
    """
    oid = {b["id"]: b for b in orig_boxes}
    cid = {b["id"]: b for b in cur_boxes}
    changed_class, moved, added, removed = [], [], [], []
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
            moved.append({"id": bid,
                          "from": {k: ob[k] for k in ("cx", "cy", "w", "h")},
                          "to": {k: cb[k] for k in ("cx", "cy", "w", "h")}})
    for bid, ob in oid.items():
        if bid not in cid:
            removed.append(ob)
    n = len(changed_class) + len(moved) + len(added) + len(removed)
    return {
        "changed_class": changed_class,
        "moved": moved,
        "added": added,
        "removed": removed,
        "counts": {"class_changed": len(changed_class), "moved": len(moved),
                   "added": len(added), "removed": len(removed), "total_diff": n},
        "has_changes": n > 0,
        "conversions": conversions,
    }


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
