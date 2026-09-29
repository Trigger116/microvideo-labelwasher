# -*- coding: utf-8 -*-
"""导出 done 包：图片复制 + 清洗后 labels + 复核记录.csv + 审计报告。

输入目录只读；输出为 <root>_done 完整目录（结构一致）。
"""
import os
import shutil
import time

from io_utils import (atomic_write_text, copy_file_bytes, fmt_float,
                      is_within, read_lines)
from workspace import TERMINAL_STATUSES, ws_stats


def _image_by_ref(ws, split, filename):
    for im in ws["images"]:
        if im["split"] == split and im["name"] == filename:
            return im
    return None


def _note_summary(im, diff):
    """已修改且备注为空时自动填变更摘要。"""
    parts = []
    cc = diff["counts"]["class_changed"]
    if cc:
        parts.append(f"改类{cc}")
    if diff["counts"]["added"]:
        parts.append(f"新增{diff['counts']['added']}")
    if diff["counts"]["removed"]:
        parts.append(f"删除{diff['counts']['removed']}")
    if diff["counts"]["moved"]:
        parts.append(f"移动{diff['counts']['moved']}")
    return "/".join(parts)


def _export_csv(ws, pkg_dir, out_subdir, csv_bom, report):
    """生成复核记录.csv（BOM + CRLF，行序同输入原序）。"""
    from diffs import diff_boxes

    header = ["文件名", "split", "原始框数", "状态", "修改说明或疑问"]
    lines = [",".join(header)]
    pending_warnings = []
    seen = set()
    for row in ws["package"].get("csv_rows") or []:
        im = _image_by_ref(ws, row["split"], row["filename"])
        if im is None:
            lines.append(",".join([row["filename"], row["split"], str(row["orig_box_count"]),
                                   row["status"], row["note"]]))
            continue
        seen.add(im["id"])
        note = im["note"] or ""
        diff = diff_boxes(im["orig_boxes"], im["boxes"])
        if im["status"] == "已修改" and not note:
            note = _note_summary(im, diff)
        elif im["status"] == "待裁决" and not note:
            pending_warnings.append(f"{im['name']}: 待裁决但无原因备注")
        lines.append(",".join([im["name"], im["split"], str(im["orig_box_count"]),
                               im["status"], note.replace(",", "，").replace("\r", "").replace("\n", "；")]))
    for im in ws["images"]:
        if im["id"] in seen:
            continue
        diff = diff_boxes(im["orig_boxes"], im["boxes"])
        note = im["note"] or ("/" if False else "")
        if im["status"] == "已修改" and not note:
            note = _note_summary(im, diff)
        lines.append(",".join([im["name"], im["split"], str(im["orig_box_count"]),
                               im["status"], note.replace(",", "，")]))
    csv_text = "\r\n".join(lines) + "\r\n"
    out_path = os.path.join(out_subdir, "复核记录.csv")
    atomic_write_bytes_bom(out_path, csv_text)
    return pending_warnings


def atomic_write_bytes_bom(path, text):
    from io_utils import atomic_write_bytes

    atomic_write_bytes(path, ("﻿" + text).encode("utf-8"))


def _export_labels(ws, out_subdir, decimals):
    from diffs import clamp_box

    for im in ws["images"]:
        out_path = os.path.join(out_subdir, im["rel_label"])
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out_lines = []
        for b in im["boxes"]:
            out_lines.append(f"{b['class_id']} "
                             f"{fmt_float(b['cx'], decimals)} {fmt_float(b['cy'], decimals)} "
                             f"{fmt_float(b['w'], decimals)} {fmt_float(b['h'], decimals)}")
        atomic_write_text(out_path, "\n".join(out_lines) + ("\n" if out_lines else ""))


def export_workspace(ws, output_dir=None, overwrite=False, strict=None):
    """执行导出。返回 {output_dir, report, warnings}。"""
    cfg = ws.get("config") or {}
    exp_cfg = cfg.get("export") or {}
    strict = exp_cfg.get("strict", False) if strict is None else strict
    decimals = int(exp_cfg.get("decimals", 6))
    csv_bom = bool(exp_cfg.get("csv_bom", True))

    pkg_dir = ws["package"]["pkg_dir"]
    out_root = os.path.abspath(output_dir or ws["package"]["output_dir"])
    # 路径守卫：输出不得位于输入包内
    if is_within(out_root, pkg_dir):
        raise ValueError(f"输出目录不得位于输入数据包内: {out_root} 在 {pkg_dir} 内")
    if os.path.abspath(out_root) == os.path.abspath(pkg_dir):
        raise ValueError("输出目录与输入数据包相同，拒绝覆盖")

    st = ws_stats(ws)
    warnings = []
    pending = st["status_counts"].get("未复核", 0)
    if pending > 0:
        msg = f"仍有 {pending} 张图片处于'未复核'状态"
        if strict:
            raise ValueError(msg + "（strict 模式拒绝导出）")
        warnings.append(msg + "（已在报告中列出）")

    if os.path.exists(out_root) and not overwrite:
        raise FileExistsError(f"输出目录已存在（需确认覆盖）: {out_root}")

    subdir = ws["package"]["subdir"]
    out_subdir = os.path.join(out_root, subdir)
    os.makedirs(out_subdir, exist_ok=True)

    # 1) 元文件字节复制（保 BOM/CRLF）
    meta_names = ["classes.txt", "类别说明.csv"] + [m for m in ws["package"]["meta_files"]
                                                    if not m.startswith("复核记录")]
    for n in meta_names:
        src = os.path.join(pkg_dir, n)
        if os.path.exists(src):
            copy_file_bytes(src, os.path.join(out_subdir, n))
    for n in ws["package"]["meta_files"]:
        if n.startswith("复核记录"):
            continue
        src = os.path.join(pkg_dir, n)
        if os.path.exists(src) and n not in meta_names:
            copy_file_bytes(src, os.path.join(out_subdir, n))

    # 2) 图片复制（mtime+size 相同跳过，二次导出秒级）
    t0 = time.time()
    copied = skipped = 0
    for im in ws["images"]:
        src = os.path.join(pkg_dir, im["rel_image"])
        dst = os.path.join(out_subdir, im["rel_image"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            ss, ds = os.stat(src), os.stat(dst)
            if ss.st_size == ds.st_size and int(ss.st_mtime) == int(ds.st_mtime):
                skipped += 1
                continue
        shutil.copy2(src, dst)
        copied += 1

    # 3) labels
    _export_labels(ws, out_subdir, decimals)

    # 4) 复核记录.csv
    csv_warnings = _export_csv(ws, pkg_dir, out_subdir, csv_bom, None)
    warnings.extend(csv_warnings)

    # 5) 审计报告
    report = build_report(ws)
    report["export"] = {
        "output_dir": out_root,
        "images_copied": copied, "images_skipped": skipped,
        "seconds": round(time.time() - t0, 2),
        "pending_unreviewed": pending,
    }
    return {"output_dir": out_root, "report": report, "warnings": warnings}


def build_report(ws):
    """修改审计报告：转换对/增删框/待裁决清单（F-1 要求）。"""
    from diffs import diff_boxes

    names = {c["id"]: f"{c['zh']}({c['name']})" for c in ws["package"]["classes"]}
    conversions = {}
    counts = {"class_changed": 0, "moved": 0, "added": 0, "removed": 0}
    diff_images = 0
    changed_detail = []
    pending_list = []
    unreviewed = []
    for im in ws["images"]:
        d = diff_boxes(im["orig_boxes"], im["boxes"])
        if d["has_changes"]:
            diff_images += 1
            for k, v in d["conversions"].items():
                conversions[k] = conversions.get(k, 0) + v
            for c in counts:
                counts[c] += d["counts"][c]
            changed_detail.append({
                "name": im["name"], "split": im["split"], "status": im["status"],
                "counts": d["counts"],
                "conversions": [{"from": names.get(a, str(a)), "to": names.get(b, str(b)), "n": n}
                                for (a, b), n in sorted(d["conversions"].items())],
            })
        if im["status"] == "待裁决":
            pending_list.append({"name": im["name"], "split": im["split"], "note": im["note"]})
        if im["status"] == "未复核":
            unreviewed.append(f"{im['split']}/{im['name']}")
    st = ws_stats(ws)
    return {
        "conversions": [{"from": names.get(a, str(a)), "to": names.get(b, str(b)), "n": n}
                        for (a, b), n in sorted(conversions.items(), key=lambda x: -x[1])],
        "counts": counts,
        "diff_images": diff_images,
        "status_counts": st["status_counts"],
        "pending_list": pending_list,
        "unreviewed": unreviewed[:200],
        "unreviewed_total": len(unreviewed),
        "changed_detail": changed_detail,
    }
