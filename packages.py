# -*- coding: utf-8 -*-
"""输入包扫描：解析 review_XX 结构（classes/CSV/图片标签清单/统计/警告）。

约定：对输入目录只读，绝不写入。
"""
import csv
import glob
import os

from PIL import Image

from config import load_default_config
from io_utils import read_lines


def scan_candidates(root):
    """列出 root 下可作为数据包的目录（自身或一级子目录）。"""
    result = []
    if not os.path.isdir(root):
        return result
    if os.path.exists(os.path.join(root, "classes.txt")):
        result.append({"name": os.path.basename(os.path.abspath(root)), "self": True})
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "classes.txt")):
                result.append({"name": name, "self": False})
    except OSError:
        pass
    return result


def _parse_classes(path):
    names = [ln.strip() for ln in read_lines(path) if ln.strip()]
    return names


def _parse_class_zh(path, names):
    zh_map = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    cid = int(row.get("class_id", -1))
                except (TypeError, ValueError):
                    continue
                if 0 <= cid < len(names):
                    zh_map[cid] = (row.get("中文参考") or row.get("english_name") or names[cid]).strip()
    return zh_map


def _parse_review_csv(path):
    """返回 {文件名: {split, orig_box_count, status, note}}，同时保留行序列表。"""
    rows = []
    keyed = {}
    if not path or not os.path.exists(path):
        return rows, keyed
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows, keyed
        fname_col = _pick_column(reader.fieldnames, ["文件名", "filename", "name"])
        split_col = _pick_column(reader.fieldnames, ["split"])
        count_col = _pick_column(reader.fieldnames, ["原始框数", "box_count", "count"])
        status_col = _pick_column(reader.fieldnames, ["状态", "status"])
        note_col = _pick_column(reader.fieldnames, ["修改说明或疑问", "note", "修改说明"])
        for row in reader:
            fname = (row.get(fname_col) or "").strip()
            if not fname:
                continue
            entry = {
                "filename": fname,
                "split": (row.get(split_col) or "").strip(),
                "orig_box_count": _to_int(row.get(count_col)),
                "status": (row.get(status_col) or "未复核").strip() or "未复核",
                "note": (row.get(note_col) or "").strip(),
            }
            rows.append(entry)
            keyed.setdefault(fname, []).append(entry)
    return rows, keyed


def _pick_column(fields, candidates):
    for c in candidates:
        if c in fields:
            return c
    return candidates[0]


def _to_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def scan_package(root, subdir=None, tolerance_px=None):
    """详细扫描数据包。

    返回 dict：classes、splits、csv 状态、images 清单（含分辨率/框数/越界数）、
    类别框数统计、总框数、warnings。
    """
    cfg = load_default_config()
    tol = tolerance_px if tolerance_px is not None else cfg.get("boundary_tolerance_px", 2)
    pkg_dir = root if (subdir in (None, "", ".")) else os.path.join(root, subdir)
    if not os.path.isdir(pkg_dir):
        raise FileNotFoundError(f"数据包目录不存在: {pkg_dir}")

    warnings = []
    classes_path = os.path.join(pkg_dir, "classes.txt")
    class_names = _parse_classes(classes_path)
    zh_map = _parse_class_zh(os.path.join(pkg_dir, "类别说明.csv"), class_names)
    classes = [
        {"id": i, "name": n, "zh": zh_map.get(i, n)} for i, n in enumerate(class_names)
    ]

    csv_rows, csv_keyed = _parse_review_csv(os.path.join(pkg_dir, "复核记录.csv"))

    splits = {}
    images = []  # [{split, name, rel_image, rel_label, width, height, box_count, oob_count}]
    class_box_counts = {}
    box_total = 0
    bad_lines = 0
    oob_boxes = 0
    unknown_class_ids = set()

    for split in ("train", "val"):
        image_dir = os.path.join(pkg_dir, split, "images")
        label_dir = os.path.join(pkg_dir, split, "labels")
        if not os.path.isdir(image_dir):
            continue
        if not os.path.isdir(label_dir):
            warnings.append(f"{split}/labels 目录不存在")
            label_dir = None

        img_by_stem = {}
        for p in glob.glob(os.path.join(image_dir, "*.*")):
            stem, ext = os.path.splitext(os.path.basename(p))
            ext = ext.lower()
            if ext in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
                img_by_stem.setdefault(stem, os.path.basename(p))

        lbl_stems = set()
        if label_dir:
            for p in glob.glob(os.path.join(label_dir, "*.txt")):
                lbl_stems.add(os.path.splitext(os.path.basename(p))[0])

        missing_labels, missing_images = [], []
        for stem in sorted(set(img_by_stem) | set(lbl_stems)):
            if stem not in img_by_stem:
                missing_images.append(stem + ".txt")
            if stem not in lbl_stems:
                missing_labels.append(img_by_stem.get(stem, stem))
        if missing_labels:
            warnings.append(f"{split} 缺标签 {len(missing_labels)} 图: {missing_labels[:5]}{'…' if len(missing_labels) > 5 else ''}")
        if missing_images:
            warnings.append(f"{split} 缺图片 {len(missing_images)} 个标签: {missing_images[:5]}{'…' if len(missing_images) > 5 else ''}")

        n_imgs = len(img_by_stem)
        n_lbls = len(lbl_stems)
        if n_imgs != n_lbls:
            warnings.append(f"{split} 图片/标签数量不一致: {n_imgs}/{n_lbls}")
        splits[split] = {"images": n_imgs, "labels": n_lbls, "image_dir": image_dir, "label_dir": label_dir}

        for stem in sorted(img_by_stem):
            img_name = img_by_stem[stem]
            img_path = os.path.join(image_dir, img_name)
            width = height = 0
            try:
                with Image.open(img_path) as im:
                    width, height = im.size
            except Exception:
                warnings.append(f"无法读取图片尺寸: {img_name}")
            label_path = os.path.join(label_dir, stem + ".txt") if label_dir else None
            boxes = []
            oob = 0
            if label_path and os.path.exists(label_path):
                for line in read_lines(label_path):
                    parts = line.split()
                    if not parts:
                        continue
                    if len(parts) < 5:
                        bad_lines += 1
                        continue
                    try:
                        cid = int(float(parts[0]))
                        cx, cy, w, h = (float(v) for v in parts[1:5])
                    except ValueError:
                        bad_lines += 1
                        continue
                    if cid < 0 or cid >= len(class_names):
                        unknown_class_ids.add(cid)
                        bad_lines += 1
                        continue
                    if w <= 0 or h <= 0:
                        bad_lines += 1
                        continue
                    if width > 0 and _is_oob(cx, cy, w, h, tol, width, height):
                        oob += 1
                        oob_boxes += 1
                    boxes.append((cid, cx, cy, w, h))
                    class_box_counts[cid] = class_box_counts.get(cid, 0) + 1
                    box_total += 1
            images.append({
                "split": split, "name": img_name,
                "rel_image": f"{split}/images/{img_name}",
                "rel_label": f"{split}/labels/{stem}.txt",
                "width": width, "height": height,
                "box_count": len(boxes), "oob_count": oob,
            })

    if bad_lines:
        warnings.append(f"非法标签行 {bad_lines} 行（已被忽略，导出时不会保留）")
    if unknown_class_ids:
        warnings.append(f"标签含超出 classes.txt 范围的类 ID: {sorted(unknown_class_ids)}")
    if oob_boxes:
        warnings.append(f"{oob_boxes} 个框轻微越界（容差 {tol}px 内为取整噪声，导入时已钳制到边界）")

    meta_files = [n for n in os.listdir(pkg_dir) if n.lower().endswith(".md")]
    return {
        "root": os.path.abspath(root),
        "subdir": subdir or os.path.basename(pkg_dir),
        "pkg_dir": os.path.abspath(pkg_dir),
        "classes": classes,
        "splits": splits,
        "csv_rows": csv_rows,
        "csv_keyed": csv_keyed,
        "images": images,
        "box_total": box_total,
        "class_box_counts": class_box_counts,
        "meta_files": meta_files,
        "warnings": warnings,
    }


def _is_oob(cx, cy, w, h, tol_px, W, H):
    """判断框是否越界（带像素容差）。"""
    if W <= 0 or H <= 0:
        return False
    tx, ty = tol_px / W, tol_px / H
    return (
        cx - w / 2 < -tx or cy - h / 2 < -ty
        or cx + w / 2 > 1 + tx or cy + h / 2 > 1 + ty
    )
