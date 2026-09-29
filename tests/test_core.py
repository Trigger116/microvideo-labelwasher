# -*- coding: utf-8 -*-
"""核心单元测试：io_utils / packages / workspace / diffs / tasks / export。

真实包断言（若存在）；迷你 fixture 包断言（始终运行）。
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests/ 目录（make_fixture）

import diffs
import export as export_mod
import io_utils
import packages
import tasks
import workspace
from make_fixture import make_fixture

REAL_ROOT = r"D:\research\project\0928\review_0tya"
REAL_SUB = "review_04"
BATCH_DIR = r"D:\research\project\0928\plan\batches"
HAS_REAL = os.path.isdir(os.path.join(REAL_ROOT, REAL_SUB))


class TestIoUtils(unittest.TestCase):
    def test_atomic_write_and_read(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.txt")
        io_utils.atomic_write_text(p, "line1\nline2", newline="\r\n")
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"line1\r\nline2")
        io_utils.atomic_write_bytes(p, "中文".encode("utf-8"))
        self.assertEqual(io_utils.read_text(p), "中文")

    def test_path_guard(self):
        self.assertTrue(io_utils.is_within(r"D:\a\b\c", r"D:\a"))
        self.assertFalse(io_utils.is_within(r"D:\a_done", r"D:\a"))
        with self.assertRaises(ValueError):
            io_utils.assert_paths_safe(r"D:\a", r"D:\a\out")

    def test_fmt_float(self):
        self.assertEqual(io_utils.fmt_float(0.4574219999), "0.457422")


class TestPackages(unittest.TestCase):
    def test_fixture_scan(self):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        root = os.path.dirname(pkg)
        sub = os.path.basename(pkg)
        detail = packages.scan_package(root, sub)
        self.assertEqual(len(detail["classes"]), 4)
        self.assertEqual(detail["classes"][0]["name"], "builder")
        self.assertEqual(detail["box_total"], 4)
        imgs = {i["name"]: i for i in detail["images"]}
        self.assertEqual(len(imgs), 4)
        self.assertEqual(imgs["a3.jpg"]["box_count"], 0)  # 空标签
        self.assertEqual(imgs["a2.png"]["width"], 100)  # png 扩展名匹配
        self.assertEqual(len(detail["csv_rows"]), 4)
        self.assertEqual(detail["csv_keyed"]["a1.jpg"][0]["status"], "未复核")

    @unittest.skipUnless(HAS_REAL, "真实包不存在")
    def test_real_scan(self):
        detail = packages.scan_package(REAL_ROOT, REAL_SUB)
        self.assertEqual(len(detail["images"]), 677)
        self.assertEqual(detail["box_total"], 9844)
        self.assertEqual(len(detail["classes"]), 23)
        self.assertEqual(detail["class_box_counts"][4], 108)  # 交警
        self.assertEqual(detail["class_box_counts"].get(19, 0), 0)  # 医护
        self.assertFalse(detail["warnings"])  # 摸底结论：无坏行/无缺失


class TestWorkspace(unittest.TestCase):
    def _mk_ws(self, tmp):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        scan = packages.scan_package(os.path.dirname(pkg), os.path.basename(pkg))
        return workspace.create_workspace(scan, tmp, output_dir=os.path.join(tmp, "out_done"))

    def test_create_and_roundtrip(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        self.assertEqual(len(ws["images"]), 4)
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        self.assertEqual(len(a1["orig_boxes"]), 2)
        self.assertEqual(a1["orig_box_count"], 2)  # 取 CSV 值
        ws2 = workspace.load_workspace(tmp, ws["id"])
        self.assertEqual(len(ws2["images"]), 4)

    def test_state_derivation(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        boxes = copy.deepcopy(a1["boxes"])
        boxes[0]["class_id"] = 1  # builder→person
        r = workspace.put_boxes(ws, a1["id"], boxes)
        self.assertEqual(r["status"], "已修改")
        self.assertTrue(r["has_changes"])
        with self.assertRaises(ValueError):  # 有 diff 拒绝"已核验无修改"
            workspace.put_state(ws, a1["id"], "已核验无修改")
        workspace.put_state(ws, a1["id"], "待裁决", note="仅黄反光服")
        self.assertEqual(ws["images"][0]["status"] if False else
                         next(i for i in ws["images"] if i["id"] == a1["id"])["status"], "待裁决")

    def test_reset_and_stats(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        boxes = copy.deepcopy(a1["boxes"])
        boxes.append({"class_id": 3, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1})  # 新增
        workspace.put_boxes(ws, a1["id"], boxes)
        workspace.reset_image(ws, a1["id"])
        st = workspace.ws_stats(ws)
        self.assertEqual(st["diff_images"], 0)
        self.assertEqual(st["box_total_current"], st["box_total_original"])

    def test_atomic_save_and_backup(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        workspace.save_workspace(ws)
        p = os.path.join(tmp, ws["id"], "workspace.json")
        self.assertTrue(os.path.exists(p))


class TestDiffs(unittest.TestCase):
    def test_diff_types(self):
        orig = [{"id": "b1", "class_id": 4, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2},
                {"id": "b2", "class_id": 2, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}]
        cur = [{"id": "b1", "class_id": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2},
               {"id": "b_new_1", "class_id": 3, "cx": 0.7, "cy": 0.7, "w": 0.1, "h": 0.1}]
        d = diffs.diff_boxes(orig, cur)
        self.assertTrue(d["has_changes"])
        self.assertEqual(d["counts"]["class_changed"], 1)  # 交警→builder
        self.assertEqual(d["counts"]["added"], 1)
        self.assertEqual(d["counts"]["removed"], 1)  # b2 消失
        self.assertEqual(d["conversions"], {(4, 0): 1})

    def test_move_epsilon(self):
        orig = [{"id": "b1", "class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]
        cur_small = [{"id": "b1", "class_id": 1, "cx": 0.5001, "cy": 0.5, "w": 0.2, "h": 0.2}]
        cur_big = [{"id": "b1", "class_id": 1, "cx": 0.501, "cy": 0.5, "w": 0.2, "h": 0.2}]
        self.assertFalse(diffs.diff_boxes(orig, cur_small, move_eps=0.0005)["has_changes"])
        self.assertTrue(diffs.diff_boxes(orig, cur_big, move_eps=0.0005)["has_changes"])

    def test_oob_tolerance(self):
        b = {"cx": 0.952, "cy": 0.5, "w": 0.1, "h": 0.1}  # x2=1.002
        self.assertTrue(diffs.boxes_out_of_bounds([b], 1000, 1000, tol_px=0))
        self.assertFalse(diffs.boxes_out_of_bounds([b], 1000, 1000, tol_px=2))

    def test_clamp(self):
        r = diffs.clamp_box({"cx": 0.4, "cy": 0.5, "w": 0.9, "h": 0.2})
        self.assertAlmostEqual(r["cx"], 0.425, places=9)
        self.assertAlmostEqual(r["cy"], 0.5, places=9)
        self.assertAlmostEqual(r["w"], 0.85, places=9)  # 左边界裁掉 0.05
        self.assertAlmostEqual(r["h"], 0.2, places=9)


class TestTasks(unittest.TestCase):
    def _mk_ws(self, tmp, batch_dir=None):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        scan = packages.scan_package(os.path.dirname(pkg), os.path.basename(pkg))
        return workspace.create_workspace(scan, tmp, batch_dir=batch_dir,
                                          output_dir=os.path.join(tmp, "out_done"))

    def test_t1_gen(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        t = tasks.all_tasks(ws)
        self.assertEqual(len(t["t1"]), 10)
        t1_9 = next(x for x in t["t1"] if x["class_ids"] == [0])
        self.assertEqual(t1_9["progress"]["total_boxes"], 1)  # fixture 里 1 个 builder

    def test_t2_auto(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)  # 无 batch_dir → 自算
        t = tasks.all_tasks(ws)
        names = [x["name"] for x in t["t2"]]
        self.assertTrue(any("低框数" in n for n in names))
        self.assertEqual(sum(x["progress"]["total_images"] for x in t["t2"]), 4)

    @unittest.skipUnless(HAS_REAL and os.path.isdir(BATCH_DIR), "批次清单不存在")
    def test_t2_from_batch_files(self):
        tmp = tempfile.mkdtemp()
        scan = packages.scan_package(REAL_ROOT, REAL_SUB)
        ws = workspace.create_workspace(scan, tmp, batch_dir=BATCH_DIR)
        t = tasks.all_tasks(ws)
        self.assertEqual(len(t["t2"]), 13)
        total = sum(x["progress"]["total_images"] for x in t["t2"])
        self.assertEqual(total, 677)  # 无缺漏无重复
        self.assertEqual(t["t1"][0]["progress"]["total_boxes"], 108)  # 交警


class TestExport(unittest.TestCase):
    def test_fixture_export(self):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        root = os.path.dirname(pkg)
        scan = packages.scan_package(root, os.path.basename(pkg))
        ws_root = tempfile.mkdtemp()
        out = os.path.join(root, "review_fix_done")
        ws = workspace.create_workspace(scan, ws_root, output_dir=out)
        before = io_utils.dir_tree_hash(root, exclude_names=["review_fix_done"])
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        boxes = copy.deepcopy(a1["boxes"])
        boxes[0]["class_id"] = 1
        workspace.put_boxes(ws, a1["id"], boxes)
        workspace.put_state(ws, a1["id"], "已修改", note="builder→person 测试")
        a3 = next(i for i in ws["images"] if i["name"] == "a3.jpg")
        boxes3 = copy.deepcopy(a3["boxes"])
        boxes3.append({"class_id": 2, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2})
        workspace.put_boxes(ws, a3["id"], boxes3)

        res = export_mod.export_workspace(ws, overwrite=True)
        self.assertEqual(len(res["warnings"]), 1)  # 2 张未复核 → 警告但继续（非 strict）
        # 目录结构
        out_sub = os.path.join(out, "review_01")
        self.assertTrue(os.path.exists(os.path.join(out_sub, "classes.txt")))
        self.assertTrue(os.path.exists(os.path.join(out_sub, "train", "images", "a1.jpg")))
        self.assertTrue(os.path.exists(os.path.join(out_sub, "复核记录.csv")))
        # labels 内容（6 位小数 + 空 txt 保留）
        lab = io_utils.read_text(os.path.join(out_sub, "train", "labels", "a1.txt"))
        self.assertIn("1 0.250000 0.500000 0.200000 0.300000", lab)
        self.assertEqual(io_utils.read_text(os.path.join(out_sub, "train", "labels", "a3.txt")),
                         "2 0.500000 0.500000 0.200000 0.200000\n")
        # CSV BOM + CRLF + 状态
        with open(os.path.join(out_sub, "复核记录.csv"), "rb") as f:
            csv_bytes = f.read()
        self.assertTrue(csv_bytes.startswith("﻿".encode("utf-8")))
        self.assertIn("已修改".encode("utf-8"), csv_bytes)
        self.assertIn("builder".encode("utf-8"), csv_bytes)
        # 输入目录未被修改
        after = io_utils.dir_tree_hash(root, exclude_names=["review_fix_done"])
        self.assertEqual(before, after)
        # 审计报告
        self.assertEqual(res["report"]["counts"]["class_changed"], 1)
        self.assertEqual(res["report"]["counts"]["added"], 1)
        shutil.rmtree(out, ignore_errors=True)


class TestApi(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        make_fixture(d)
        self.pkg_root = os.path.join(d, "review_fix")  # 含 review_01 子包
        import server

        self.ws_root = tempfile.mkdtemp()
        self.app = server.create_app(ws_root=self.ws_root)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_full_flow(self):
        c = self.client
        r = c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        r = c.post("/api/packages/scan", json={"root": os.path.dirname(self.pkg_root)})
        self.assertEqual(r.json["mode"], "candidates")
        r = c.post("/api/packages/scan", json={"root": self.pkg_root, "subdir": "review_01"})
        self.assertEqual(r.json["mode"], "detail")
        self.assertEqual(r.json["detail"]["box_total"], 4)
        r = c.post("/api/workspaces", json={"root": self.pkg_root, "subdir": "review_01"})
        self.assertEqual(r.status_code, 200)
        ws_id = r.json["id"]
        r = c.get(f"/api/workspaces/{ws_id}")
        self.assertEqual(r.json["stats"]["total_images"], 4)
        self.assertEqual(len(r.json["tasks"]["t1"]), 10)
        # 图片列表与详情
        r = c.get(f"/api/workspaces/{ws_id}/images")
        self.assertEqual(r.json["total"], 4)
        img_id = r.json["images"][0]["img_id"]
        r = c.get(f"/api/workspaces/{ws_id}/images/{img_id}")
        self.assertEqual(r.json["status"], "未复核")
        # 改框 → 状态自动已修改
        boxes = r.json["boxes"]
        boxes[0]["class_id"] = 2
        r = c.put(f"/api/workspaces/{ws_id}/images/{img_id}/boxes", json={"boxes": boxes})
        self.assertEqual(r.json["status"], "已修改")
        # 状态规则：拒绝
        r = c.put(f"/api/workspaces/{ws_id}/images/{img_id}/state", json={"status": "已核验无修改"})
        self.assertEqual(r.status_code, 400)
        # 图片文件
        r = c.get(f"/api/workspaces/{ws_id}/images/{img_id}/file")
        self.assertEqual(r.status_code, 200)
        # 导出
        out = os.path.join(self.pkg_root, "review_fix_done")
        r = c.post(f"/api/workspaces/{ws_id}/export",
                   json={"output_dir": out, "overwrite": True})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(os.path.exists(os.path.join(out, "review_01", "val", "labels", "b1.txt")))
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
