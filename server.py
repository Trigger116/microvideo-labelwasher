# -*- coding: utf-8 -*-
"""Flask app 工厂 + 全部 REST API（薄层：解析参数、调 service、统一错误）。"""
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory

import export as export_mod
import packages as packages_mod
import tasks as tasks_mod
import workspace as workspace_mod
from config import load_default_config, merge_config, resource_path, validate_config

SCAN_CACHE = {}
SCAN_CACHE_LOCK = threading.Lock()


def api_error(code, message, status=400):
    return jsonify({"error": {"code": code, "message": message}}), status


class WsStore:
    """工作区内存缓存 + 磁盘持久化。"""

    def __init__(self, ws_root):
        self.ws_root = ws_root
        self._cache = {}

    def get(self, ws_id):
        ws = self._cache.get(ws_id)
        if ws is None:
            ws = workspace_mod.load_workspace(self.ws_root, ws_id)
            self._cache[ws_id] = ws
        return ws

    def save(self, ws):
        workspace_mod.save_workspace(ws)
        self._cache[ws["id"]] = ws

    def invalidate(self, ws_id):
        self._cache.pop(ws_id, None)


def _get_scan(root, subdir=None, tolerance_px=None):
    key = (os.path.abspath(root), subdir or "")
    with SCAN_CACHE_LOCK:
        if key not in SCAN_CACHE:
            SCAN_CACHE[key] = packages_mod.scan_package(root, subdir, tolerance_px)
            while len(SCAN_CACHE) > 8:
                SCAN_CACHE.pop(next(iter(SCAN_CACHE)))
        return SCAN_CACHE[key]


def create_app(ws_root=None, port_file=None):
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    ws_root = ws_root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces")
    store = WsStore(ws_root)
    static_dir = resource_path("static")

    # ---------- 静态资源 ----------
    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.route("/static/<path:name>")
    def static_files(name):
        return send_from_directory(static_dir, name)

    # ---------- 健康检查 ----------
    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "version": "1.0.0", "time": time.time()})

    # ---------- 文件系统浏览（选输入包） ----------
    @app.post("/api/fs/browse")
    def fs_browse():
        d = request.get_json(silent=True) or {}
        path = d.get("path") or ""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return api_error("E_FS_NOT_DIR", f"目录不存在: {path}")
        dirs = []
        try:
            for name in sorted(os.listdir(path)):
                p = os.path.join(path, name)
                if os.path.isdir(p) and not name.startswith("."):
                    dirs.append({"name": name, "is_pkg": os.path.exists(os.path.join(p, "classes.txt"))})
        except OSError as e:
            return api_error("E_FS_IO", str(e))
        parent = os.path.dirname(path) if path != os.path.dirname(path) else None
        return jsonify({"path": path, "parent": parent,
                        "drives": _list_drives(), "dirs": dirs,
                        "self_is_pkg": os.path.exists(os.path.join(path, "classes.txt"))})

    @app.post("/api/fs/pick")
    def fs_pick():
        """弹出 Windows 原生目录选择对话框，返回所选路径（取消返回 null）。"""
        if os.name != "nt":
            return api_error("E_UNSUPPORTED", "目录选择器仅支持 Windows", 400)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = '选择输入数据包所在目录';"
            "$d.ShowNewFolderButton = $false;"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {"
            " [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $d.SelectedPath }"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, timeout=600, text=True, encoding="utf-8", errors="replace",
            )
        except Exception:
            return api_error("E_PICK_FAILED", "目录选择失败", 500)
        path = (r.stdout or "").strip().strip('"')
        if not path or r.returncode != 0:
            return jsonify({"path": None})
        return jsonify({"path": path})

    # ---------- 包扫描 ----------
    @app.post("/api/packages/scan")
    def packages_scan():
        d = request.get_json(silent=True) or {}
        root = d.get("root") or ""
        if not os.path.isdir(root):
            return api_error("E_PKG_ROOT", f"根目录不存在: {root}")
        subdir = d.get("subdir")
        if subdir in ("", "__self__"):
            subdir = None
        try:
            if subdir is None and not os.path.exists(os.path.join(root, "classes.txt")):
                cands = packages_mod.scan_candidates(root)
                return jsonify({"mode": "candidates", "candidates": cands})
            detail = _get_scan(root, subdir)
            return jsonify({"mode": "detail", "detail": detail})
        except FileNotFoundError as e:
            return api_error("E_PKG_NOT_FOUND", str(e))
        except Exception as e:
            return api_error("E_PKG_SCAN", str(e))

    # ---------- 工作区 CRUD ----------
    @app.post("/api/workspaces")
    def create_ws():
        d = request.get_json(silent=True) or {}
        root = d.get("root") or ""
        subdir = d.get("subdir")
        if subdir in ("", "__self__"):
            subdir = None
        try:
            detail = _get_scan(root, subdir)
            config_override = d.get("config") or None
            if config_override:
                errs = validate_config(merge_config(load_default_config(), config_override))
                if errs:
                    return api_error("E_CONFIG", "; ".join(errs))
            batch_dir = d.get("batch_dir") or None
            if not batch_dir:
                cand = os.path.join(os.path.dirname(os.path.abspath(root)), "plan", "batches")
                batch_dir = cand if os.path.isdir(cand) else None
            ws = workspace_mod.create_workspace(
                detail, ws_root, config_override=config_override,
                batch_dir=batch_dir, output_dir=d.get("output_dir") or None)
            st = workspace_mod.ws_stats(ws)
            return jsonify({"id": ws["id"], "stats": st, "warnings": detail["warnings"],
                            "batch_dir": ws["batch_dir"],
                            "output_dir": ws["package"]["output_dir"]})
        except ValueError as e:
            return api_error("E_WS_CREATE", str(e))
        except Exception as e:
            return api_error("E_WS_CREATE", str(e))

    @app.get("/api/workspaces")
    def list_ws():
        return jsonify({"workspaces": workspace_mod.list_workspaces(ws_root)})

    @app.get("/api/workspaces/<ws_id>")
    def get_ws(ws_id):
        try:
            ws = store.get(ws_id)
        except Exception:
            return api_error("E_WS_NOT_FOUND", f"工作区不存在: {ws_id}", 404)
        st = workspace_mod.ws_stats(ws)
        tasks_all = tasks_mod.all_tasks(ws)
        return jsonify({
            "id": ws["id"], "package": ws["package"], "config": ws["config"],
            "batch_dir": ws["batch_dir"],
            "stats": st,
            "tasks": tasks_all,
            "classes": ws["package"]["classes"],
            "ui": ws.get("ui") or {"last_task_id": None, "last_img_id": None},
        })

    @app.delete("/api/workspaces/<ws_id>")
    def del_ws(ws_id):
        try:
            workspace_mod.delete_workspace(ws_root, ws_id)
        except FileNotFoundError:
            return api_error("E_WS_NOT_FOUND", f"工作区不存在: {ws_id}", 404)
        store.invalidate(ws_id)
        return jsonify({"ok": True})

    @app.get("/api/workspaces/<ws_id>/config")
    def get_ws_config(ws_id):
        ws = store.get(ws_id)
        return jsonify({"config": ws["config"]})

    @app.put("/api/workspaces/<ws_id>/config")
    def put_ws_config(ws_id):
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        new_cfg = merge_config(ws["config"], d.get("config") or {})
        errs = validate_config(new_cfg)
        if errs:
            return api_error("E_CONFIG", "; ".join(errs))
        ws["config"] = new_cfg
        store.save(ws)
        return jsonify({"config": new_cfg})

    @app.put("/api/workspaces/<ws_id>/ui")
    def put_ws_ui(ws_id):
        """中断恢复：保存上次处理位置（last_task_id / last_img_id，任一可空）。"""
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        ui = ws.get("ui") or {"last_task_id": None, "last_img_id": None}
        ui["last_task_id"] = d.get("last_task_id", ui.get("last_task_id"))
        ui["last_img_id"] = d.get("last_img_id", ui.get("last_img_id"))
        ws["ui"] = ui
        store.save(ws)
        return jsonify({"ui": ui})

    # ---------- 任务 ----------
    @app.get("/api/workspaces/<ws_id>/tasks")
    def get_tasks(ws_id):
        ws = store.get(ws_id)
        return jsonify(tasks_mod.all_tasks(ws))

    @app.get("/api/workspaces/<ws_id>/tasks/<tid>/images")
    def get_task_images(ws_id, tid):
        ws = store.get(ws_id)
        try:
            offset = int(request.args.get("offset", 0))
            limit = min(int(request.args.get("limit", 50)), 500)
            return jsonify(tasks_mod.task_images(ws, tid, offset, limit))
        except KeyError as e:
            return api_error("E_TASK_NOT_FOUND", str(e), 404)

    # ---------- 图片 ----------
    @app.get("/api/workspaces/<ws_id>/images")
    def list_images(ws_id):
        ws = store.get(ws_id)
        split = request.args.get("split") or ""
        status = request.args.get("status") or ""
        q = (request.args.get("q") or "").lower()
        agg = workspace_mod.aggregate_states(ws) if ws.get("task_states") is not None else None
        rows = []
        for im in ws["images"]:
            st = (agg[im["id"]] if agg else im)
            if split and im["split"] != split:
                continue
            if status and st["status"] != status:
                continue
            if q and q not in im["name"].lower():
                continue
            rows.append({"img_id": im["id"], "name": im["name"], "split": im["split"],
                         "box_count": len(im["boxes"]), "status": st["status"],
                         "note": st["note"], "has_changes": _img_changed(im)})
        rows.sort(key=lambda r: 0 if r["status"] in workspace_mod.TERMINAL_STATUSES else 1)
        total = len(rows)
        offset = int(request.args.get("offset", 0))
        limit = min(int(request.args.get("limit", 100)), 1000)
        return jsonify({"total": total, "images": rows[offset:offset + limit]})

    @app.get("/api/workspaces/<ws_id>/images/<img_id>")
    def get_image(ws_id, img_id):
        ws = store.get(ws_id)
        try:
            im = workspace_mod.get_image(ws, img_id)
        except KeyError:
            return api_error("E_IMG_NOT_FOUND", f"图片不存在: {img_id}", 404)
        task_id = request.args.get("task") or None
        agg = workspace_mod.aggregate_states(ws).get(img_id) or \
            {"status": im["status"], "note": im["note"]}
        if task_id:
            st = workspace_mod.get_task_state(ws, task_id, img_id)
        else:
            st = {"status": agg["status"], "note": agg["note"]}
        return jsonify({
            "img": {"id": im["id"], "name": im["name"], "split": im["split"],
                    "width": im["width"], "height": im["height"],
                    "rel_image": im["rel_image"]},
            "boxes": im["boxes"], "orig_boxes": im["orig_boxes"],
            "status": st["status"], "note": st["note"],
            "aggregate_status": agg["status"],
            "verified_box_ids": im["verified_box_ids"],
            "has_changes": _img_changed(im),
        })

    @app.get("/api/workspaces/<ws_id>/images/<img_id>/file")
    def get_image_file(ws_id, img_id):
        ws = store.get(ws_id)
        try:
            im = workspace_mod.get_image(ws, img_id)
        except KeyError:
            return api_error("E_IMG_NOT_FOUND", f"图片不存在: {img_id}", 404)
        path = os.path.join(ws["package"]["pkg_dir"], im["rel_image"])
        if not os.path.exists(path):
            return api_error("E_IMG_FILE_MISSING", f"图片文件缺失: {im['rel_image']}", 404)
        return send_file(path, conditional=True, max_age=86400)

    @app.put("/api/workspaces/<ws_id>/images/<img_id>/boxes")
    def put_boxes(ws_id, img_id):
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        try:
            res = workspace_mod.put_boxes(ws, img_id, d.get("boxes") or [],
                                          task_id=d.get("task_id"))
            store.save(ws)
            return jsonify({"boxes": res["boxes"], "has_changes": res["has_changes"],
                            "status": res["status"], "warnings": res["warnings"]})
        except KeyError:
            return api_error("E_IMG_NOT_FOUND", f"图片不存在: {img_id}", 404)
        except ValueError as e:
            return api_error("E_BOXES", str(e))

    @app.put("/api/workspaces/<ws_id>/images/<img_id>/state")
    def put_state(ws_id, img_id):
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        try:
            res = workspace_mod.put_state(ws, img_id, d.get("status", ""),
                                          note=d.get("note"),
                                          verified_box_ids=d.get("verified_box_ids"),
                                          task_id=d.get("task_id"))
            store.save(ws)
            return jsonify(res)
        except KeyError:
            return api_error("E_IMG_NOT_FOUND", f"图片不存在: {img_id}", 404)
        except ValueError as e:
            return api_error("E_STATE", str(e))

    @app.post("/api/workspaces/<ws_id>/images/<img_id>/reset")
    def reset_image(ws_id, img_id):
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        try:
            res = workspace_mod.reset_image(ws, img_id, task_id=d.get("task_id"))
            store.save(ws)
            return jsonify(res)
        except KeyError:
            return api_error("E_IMG_NOT_FOUND", f"图片不存在: {img_id}", 404)

    # ---------- CSV / 报告 / 导出 ----------
    @app.get("/api/workspaces/<ws_id>/csv")
    def get_csv(ws_id):
        ws = store.get(ws_id)
        agg = workspace_mod.aggregate_states(ws) if ws.get("task_states") is not None else None
        lines = ["文件名,split,原始框数,状态,修改说明或疑问"]
        from diffs import diff_boxes
        for im in ws["images"]:
            st = (agg[im["id"]] if agg else im)
            note = st["note"] or ""
            if st["status"] == "已修改" and not note:
                d = diff_boxes(im["orig_boxes"], im["boxes"])
                note = _note_summary(im, d)
            lines.append(",".join([im["name"], im["split"], str(im["orig_box_count"]),
                                   st["status"], note.replace(",", "，")]))
        from flask import Response
        return Response("﻿" + "\r\n".join(lines) + "\r\n", mimetype="text/csv; charset=utf-8")

    @app.get("/api/workspaces/<ws_id>/report")
    def get_report(ws_id):
        ws = store.get(ws_id)
        return jsonify(export_mod.build_report(ws))

    @app.post("/api/workspaces/<ws_id>/export")
    def do_export(ws_id):
        ws = store.get(ws_id)
        d = request.get_json(silent=True) or {}
        try:
            res = export_mod.export_workspace(ws,
                                              output_dir=d.get("output_dir") or None,
                                              overwrite=bool(d.get("overwrite", False)),
                                              strict=d.get("strict"))
            report_path = os.path.join(ws_root, ws_id, "report", "export_report.json")
            from io_utils import atomic_write_json
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            atomic_write_json(report_path, res["report"])
            return jsonify(res)
        except ValueError as e:
            return api_error("E_EXPORT", str(e))
        except FileExistsError as e:
            return api_error("E_EXPORT_EXISTS", str(e), 409)

    # ---------- 关闭 ----------
    @app.post("/api/shutdown")
    def shutdown():
        def _stop():
            time.sleep(0.3)
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()
        return jsonify({"ok": True})

    return app


def _list_drives():
    import string

    drives = []
    for letter in string.ascii_uppercase:
        p = f"{letter}:\\"
        if os.path.exists(p):
            drives.append(p)
    return drives


def _img_changed(im):
    return any(b.get("changed") or b.get("is_new") for b in im["boxes"]) or \
        len(im["boxes"]) != len(im["orig_boxes"])


def _note_summary(im, diff):
    parts = []
    if diff["counts"]["class_changed"]:
        parts.append(f"改类{diff['counts']['class_changed']}")
    if diff["counts"]["added"]:
        parts.append(f"新增{diff['counts']['added']}")
    if diff["counts"]["removed"]:
        parts.append(f"删除{diff['counts']['removed']}")
    if diff["counts"]["moved"]:
        parts.append(f"移动{diff['counts']['moved']}")
    return "/".join(parts)
