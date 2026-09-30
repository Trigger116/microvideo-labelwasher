# -*- coding: utf-8 -*-
"""核心单元测试：io_utils / packages / workspace / diffs / tasks / export。

迷你 fixture 包断言始终运行；真实包断言由环境变量驱动（未设置则跳过）：
  LABELWASH_REAL_PKG_ROOT  真实数据包所在根目录（其下含数据包子目录）
  LABELWASH_REAL_PKG_SUB   数据包子目录名
  LABELWASH_BATCH_DIR      T2 批次清单目录（可选，配合前两者使用）
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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

REAL_ROOT = os.environ.get("LABELWASH_REAL_PKG_ROOT", "")
REAL_SUB = os.environ.get("LABELWASH_REAL_PKG_SUB", "")
BATCH_DIR = os.environ.get("LABELWASH_BATCH_DIR", "")
HAS_REAL = bool(REAL_ROOT and REAL_SUB and os.path.isdir(os.path.join(REAL_ROOT, REAL_SUB)))


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
        d = tempfile.mkdtemp()
        self.assertTrue(io_utils.is_within(os.path.join(d, "a", "b", "c"), d))
        self.assertFalse(io_utils.is_within(d + "_done", d))
        with self.assertRaises(ValueError):
            io_utils.assert_paths_safe(d, os.path.join(d, "out"))

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

    def test_boundary_adjust(self):
        orig = [{"id": "b1", "class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]
        # 仅宽变化（中心固定）→ 框微调
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.2}])
        self.assertEqual(d["counts"]["boundary_adjust"], 1)
        self.assertEqual(d["counts"]["moved"], 0)
        self.assertEqual(d["counts"]["total_diff"], 1)
        # 仅高变化（中心固定）→ 框微调
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.35}])
        self.assertEqual(d["counts"]["boundary_adjust"], 1)
        self.assertEqual(d["counts"]["moved"], 0)
        # 中心也动 → moved（与框微调互斥）
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 1, "cx": 0.501, "cy": 0.5, "w": 0.3, "h": 0.2}])
        self.assertEqual(d["counts"]["boundary_adjust"], 0)
        self.assertEqual(d["counts"]["moved"], 1)
        # 改类 + 微调同框 → 两类各计 1（与历史口径一致：类别与几何独立计数）
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 2, "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.2}])
        self.assertEqual(d["counts"]["class_changed"], 1)
        self.assertEqual(d["counts"]["boundary_adjust"], 1)
        self.assertEqual(d["counts"]["total_diff"], 2)

    def test_boundary_adjust_epsilon_boundary(self):
        orig = [{"id": "b1", "class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]
        # 中心位移 ≤ eps（0.0004 < 0.0005）→ 仍算框微调
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 1, "cx": 0.5004, "cy": 0.5, "w": 0.3, "h": 0.2}],
                             move_eps=0.0005)
        self.assertEqual(d["counts"]["boundary_adjust"], 1)
        # 中心位移 > eps → moved
        d = diffs.diff_boxes(orig, [{"id": "b1", "class_id": 1, "cx": 0.5006, "cy": 0.5, "w": 0.3, "h": 0.2}],
                             move_eps=0.0005)
        self.assertEqual(d["counts"]["moved"], 1)

    def test_diff_events_order_and_ops(self):
        orig = [{"id": "b1", "class_id": 4, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2},
                {"id": "b2", "class_id": 2, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}]
        cur = [{"id": "b1", "class_id": 4, "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.2},
               {"id": "b_new_1", "class_id": 3, "cx": 0.7, "cy": 0.7, "w": 0.1, "h": 0.1}]
        ev = diffs.diff_events(orig, cur)
        # 顺序确定性：当前框序的 ~/+ 在前，orig 序的 - 在后
        self.assertEqual([e["type"] for e in ev], ["boundary_adjust", "added", "removed"])
        self.assertEqual(ev[0]["id"], "b1")
        self.assertEqual(ev[1]["class_id"], 3)
        self.assertEqual(ev[2]["class_id"], 2)
        # 改类 + 移动同框 → 两条事件
        cur2 = [{"id": "b1", "class_id": 0, "cx": 0.55, "cy": 0.5, "w": 0.2, "h": 0.2},
                {"id": "b2", "class_id": 2, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}]
        ev2 = diffs.diff_events(orig, cur2)
        self.assertEqual([e["type"] for e in ev2], ["class_changed", "moved"])
        # 无变更 → 空
        self.assertEqual(diffs.diff_events(orig, orig), [])

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


class TestTaskStates(unittest.TestCase):
    """v2 per-task 状态机：迁移 / 槽位隔离 / 广播兼容 / 聚合口径。"""

    def _mk_ws(self, tmp, batch_dir=None):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        scan = packages.scan_package(os.path.dirname(pkg), os.path.basename(pkg))
        return workspace.create_workspace(scan, tmp, batch_dir=batch_dir,
                                          output_dir=os.path.join(tmp, "out_done"))

    def _img(self, ws, name):
        return next(i for i in ws["images"] if i["name"] == name)

    def _degrade_to_v1(self, ws):
        """把新建 ws 手动退化为 v1 形态并落盘（删槽位、version=1），模拟旧工作区。"""
        ws["version"] = 1
        ws.pop("task_states", None)
        ws["task_states_migrated"] = False
        workspace.save_workspace(ws)

    def test_migration_copies_terminal_to_t1_slots(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        a2 = self._img(ws, "a2.png")
        a1["status"] = "已修改"
        a1["note"] = "旧全局备注"
        a2["status"] = "待裁决"
        self._degrade_to_v1(ws)
        self.assertTrue(tasks.ensure_task_states(ws))
        self.assertEqual(ws["version"], 2)
        self.assertTrue(ws["task_states_migrated"])
        ts = ws["task_states"]
        # a1 含 builder(0)+sedan(2) 框 → 归属 t1-9 / t1-10，终态复制到两槽位
        self.assertEqual(ts["t1-9"][a1["id"]]["status"], "已修改")
        self.assertEqual(ts["t1-9"][a1["id"]]["note"], "旧全局备注")
        self.assertEqual(ts["t1-10"][a1["id"]]["status"], "已修改")
        # a2 仅 truck(3) 框 → 无 T1 归属；T2 槽位只建空 dict（全新开始）
        self.assertEqual(ts.get("t2-auto-low", {}), {})
        # 图级字段保留（兜底值不动）
        self.assertEqual(a1["status"], "已修改")
        # 迁移前自动备份
        self.assertTrue(os.path.exists(os.path.join(tmp, ws["id"], "workspace.pre_v2.json")))

    def test_migration_idempotent(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        self._img(ws, "a1.jpg")["status"] = "已修改"
        self._degrade_to_v1(ws)
        self.assertTrue(tasks.ensure_task_states(ws))
        snap = copy.deepcopy(ws["task_states"])
        self.assertFalse(tasks.ensure_task_states(ws))
        self.assertEqual(ws["task_states"], snap)
        # load_workspace 重载也不重复迁移
        ws2 = workspace.load_workspace(tmp, ws["id"])
        self.assertEqual(ws2["task_states"], snap)

    def test_slot_isolation_and_aggregate(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        t2_id = tasks.gen_t2_tasks(ws)[0]["id"]
        workspace.put_state(ws, a1["id"], "待裁决", note="反光服存疑", task_id="t1-9")
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "待裁决")
        self.assertEqual(workspace.get_task_state(ws, "t1-10", a1["id"])["status"], "未复核")
        # t1-9 已"待裁决" → 聚合取最高优先级（其他任务未复核不影响）
        agg = workspace.aggregate_states(ws)
        self.assertEqual(agg[a1["id"]]["status"], "待裁决")
        self.assertEqual(agg[a1["id"]]["note"], "反光服存疑")
        # 全部任务未复核 → 聚合"未复核"
        workspace.reset_image(ws, a1["id"], task_id="t1-9")
        agg = workspace.aggregate_states(ws)
        self.assertEqual(agg[a1["id"]]["status"], "未复核")
        # t1-10/t2 完成、t1-9 待裁决 → 聚合仍 待裁决 优先
        workspace.put_state(ws, a1["id"], "待裁决", note="反光服存疑", task_id="t1-9")
        workspace.put_state(ws, a1["id"], "已核验无修改", task_id="t1-10")
        workspace.put_state(ws, a1["id"], "已核验无修改", task_id=t2_id)
        agg = workspace.aggregate_states(ws)
        self.assertEqual(agg[a1["id"]]["status"], "待裁决")
        self.assertEqual(agg[a1["id"]]["note"], "反光服存疑")
        # 槽位互不覆盖
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "待裁决")
        self.assertEqual(workspace.get_task_state(ws, "t1-10", a1["id"])["status"], "已核验无修改")

    def test_broadcast_without_task_id(self):
        """旧 API（无 task_id）写图级并广播到全部所属任务槽位。"""
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        t2_id = tasks.gen_t2_tasks(ws)[0]["id"]
        workspace.put_state(ws, a1["id"], "已修改", note="旧 API 兼容")
        for tid in ("t1-9", "t1-10", t2_id):
            self.assertEqual(workspace.get_task_state(ws, tid, a1["id"])["status"], "已修改")
        self.assertEqual(workspace.aggregate_states(ws)[a1["id"]]["status"], "已修改")

    def test_task_images_slot_view(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        workspace.put_state(ws, a1["id"], "已核验无修改", task_id="t1-9")
        r9 = tasks.task_images(ws, "t1-9")
        self.assertEqual(r9["total"], 1)
        self.assertEqual(r9["images"][0]["status"], "已核验无修改")
        # 同图在 t1-10 视角仍未复核
        r10 = tasks.task_images(ws, "t1-10")
        self.assertEqual(r10["total"], 1)
        self.assertEqual(r10["images"][0]["status"], "未复核")
        # 任务进度同样槽位视角
        flat = {t["id"]: t for t in tasks.all_tasks_flat(ws)}
        self.assertEqual(tasks.task_progress(ws, flat["t1-9"])["terminal_images"], 1)
        self.assertEqual(tasks.task_progress(ws, flat["t1-10"])["terminal_images"], 0)

    def test_put_boxes_writes_task_slot(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        boxes = copy.deepcopy(a1["boxes"])
        boxes[0]["class_id"] = 1
        r = workspace.put_boxes(ws, a1["id"], boxes, task_id="t1-9")
        self.assertEqual(r["status"], "已修改")
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "已修改")
        self.assertEqual(workspace.get_task_state(ws, "t1-10", a1["id"])["status"], "未复核")

    def test_reset_clears_task_slot(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        workspace.put_state(ws, a1["id"], "已修改", task_id="t1-9")
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "已修改")
        r = workspace.reset_image(ws, a1["id"], task_id="t1-9")
        self.assertEqual(r["status"], "未复核")
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "未复核")

    def test_ws_stats_aggregate(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        t2_id = tasks.gen_t2_tasks(ws)[0]["id"]
        workspace.put_state(ws, a1["id"], "已修改", task_id="t1-9")
        workspace.put_state(ws, a1["id"], "已核验无修改", task_id="t1-10")
        workspace.put_state(ws, a1["id"], "已核验无修改", task_id=t2_id)
        st = workspace.ws_stats(ws)
        self.assertEqual(st["terminal_images"], 1)  # 仅 a1 全任务完成
        self.assertEqual(st["status_counts"].get("已修改"), 1)
        self.assertEqual(st["status_counts"].get("未复核"), 3)

    def test_create_ws_imports_csv_terminal(self):
        """create_workspace 把 CSV 初始终态灌入 T1 槽位；T2 槽位空白。"""
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        csv_p = os.path.join(pkg, "复核记录.csv")
        with open(csv_p, "w", encoding="utf-8", newline="") as f:
            f.write("文件名,split,原始框数,状态,修改说明或疑问\r\n")
            f.write("a1.jpg,train,2,已核验无修改,导入备注\r\n")
            f.write("a2.png,train,1,未复核,\r\n")
            f.write("a3.jpg,train,0,未复核,\r\n")
            f.write("b1.jpg,val,1,未复核,\r\n")
        scan = packages.scan_package(os.path.dirname(pkg), os.path.basename(pkg))
        tmp = tempfile.mkdtemp()
        ws = workspace.create_workspace(scan, tmp)
        a1 = self._img(ws, "a1.jpg")
        self.assertEqual(ws["version"], 2)
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["status"], "已核验无修改")
        self.assertEqual(workspace.get_task_state(ws, "t1-9", a1["id"])["note"], "导入备注")
        self.assertEqual(workspace.get_task_state(ws, "t1-10", a1["id"])["status"], "已核验无修改")
        t2_id = tasks.gen_t2_tasks(ws)[0]["id"]
        self.assertEqual(workspace.get_task_state(ws, t2_id, a1["id"])["status"], "未复核")

    def test_export_uses_aggregate(self):
        """导出 CSV/报告用聚合口径：图级已修改但槽位未复核 → 仍算未复核。"""
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        a1 = self._img(ws, "a1.jpg")
        a3 = self._img(ws, "a3.jpg")
        t2_id = tasks.gen_t2_tasks(ws)[0]["id"]
        boxes = copy.deepcopy(a1["boxes"])
        boxes[0]["class_id"] = 1
        workspace.put_boxes(ws, a1["id"], boxes, task_id="t1-9")
        workspace.put_state(ws, a1["id"], "已修改", task_id="t1-9")
        workspace.put_state(ws, a1["id"], "已修改", task_id="t1-10")
        workspace.put_state(ws, a1["id"], "已修改", task_id=t2_id)
        boxes3 = copy.deepcopy(a3["boxes"])
        boxes3.append({"class_id": 2, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2})
        workspace.put_boxes(ws, a3["id"], boxes3)  # 无 task_id：只图级派生，槽位仍空白
        res = export_mod.export_workspace(ws, overwrite=True)
        self.assertEqual(len(res["warnings"]), 1)
        self.assertEqual(res["report"]["status_counts"].get("已修改"), 1)
        self.assertEqual(res["report"]["status_counts"].get("未复核"), 3)
        out_sub = os.path.join(ws["package"]["output_dir"], ws["package"]["subdir"])
        csv_txt = io_utils.read_text(os.path.join(out_sub, "复核记录.csv"))
        a1_line = next(l for l in csv_txt.splitlines() if l.startswith("a1.jpg"))
        a3_line = next(l for l in csv_txt.splitlines() if l.startswith("a3.jpg"))
        self.assertIn("已修改", a1_line)
        self.assertIn("未复核", a3_line)  # 槽位视角，而非图级"已修改"
        shutil.rmtree(ws["package"]["output_dir"], ignore_errors=True)


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
        # CSV 新增两列（变更摘要 + 逐框明细），前 5 列顺序不变
        csv_lines = csv_bytes.decode("utf-8-sig").replace("\r", "").split("\n")
        header = csv_lines[0].split(",")
        self.assertEqual(header[:5], ["文件名", "split", "原始框数", "状态", "修改说明或疑问"])
        self.assertEqual(header[5:], ["变更摘要", "逐框明细"])
        a1_row = next(l for l in csv_lines if l.startswith("a1.jpg,"))
        self.assertIn("改类1", a1_row)
        self.assertIn("改类:", a1_row)          # 逐框明细：类别名用 zh
        a3_row = next(l for l in csv_lines if l.startswith("a3.jpg,"))
        self.assertIn("新增1", a3_row)
        self.assertIn("新增:", a3_row)
        unchanged_row = next(l for l in csv_lines if l.startswith("a2."))
        self.assertIn("无", unchanged_row)      # 无变更图摘要="无"
        # 输入目录未被修改
        after = io_utils.dir_tree_hash(root, exclude_names=["review_fix_done"])
        self.assertEqual(before, after)
        # 审计报告
        self.assertEqual(res["report"]["counts"]["class_changed"], 1)
        self.assertEqual(res["report"]["counts"]["added"], 1)
        shutil.rmtree(out, ignore_errors=True)


class TestAsyncSave(unittest.TestCase):
    """P2 异步合并写盘：序列化瘦身 / 去抖合并 / flush / 并发 mutation / stats 缓存。"""

    def _mk_ws(self, tmp):
        d = tempfile.mkdtemp()
        pkg = make_fixture(d)
        scan = packages.scan_package(os.path.dirname(pkg), os.path.basename(pkg))
        return workspace.create_workspace(scan, tmp, output_dir=os.path.join(tmp, "out_done"))

    def test_dumps_strips_helper_keys(self):
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        workspace.get_image(ws, ws["images"][0]["id"])  # 构建 _img_index
        ws["_mut_seq"] = 7
        data = json.loads(workspace._dumps_workspace(ws).decode("utf-8"))
        self.assertNotIn("_dir", data)
        self.assertNotIn("_mut_seq", data)
        self.assertNotIn("_img_index", data)
        self.assertNotIn("images_by_id", data)
        self.assertEqual(data["version"], 2)

    def test_scheduler_debounce_merge_and_flush(self):
        import server
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        sched = server.SaveScheduler(ws, ws["_dir"], debounce=0.2)
        calls = []
        orig = workspace.atomic_write_bytes
        workspace.atomic_write_bytes = lambda path, data: calls.append(data)
        try:
            sched.schedule()
            sched.schedule()
            sched.schedule()                 # 突发 3 次 → 合并 1 次写
            time.sleep(0.05)
            self.assertEqual(len(calls), 0)  # 去抖窗口内未写
            sched.flush()
            self.assertEqual(len(calls), 1)  # flush 立即写
            sched.flush()
            self.assertEqual(len(calls), 1)  # 无未决内容 → 不重复写
            sched.schedule()
            time.sleep(0.3)                  # 超过去抖(0.2s) → 后台自动写
            self.assertEqual(len(calls), 2)
            sched.stop()
        finally:
            workspace.atomic_write_bytes = orig

    def test_concurrent_mutations(self):
        import server
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        sched = server.SaveScheduler(ws, ws["_dir"], debounce=0.05)
        ids = [im["id"] for im in ws["images"]]
        errors = []

        def worker(k):
            try:
                for _ in range(20):
                    workspace.put_state(ws, ids[k % len(ids)], "待裁决",
                                        note=f"t{k}", task_id="t1-9")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        sched.schedule()
        sched.flush()
        disk = json.load(open(os.path.join(tmp, ws["id"], "workspace.json"),
                              encoding="utf-8"))
        slot = disk["task_states"]["t1-9"]
        for img_id in ids:
            self.assertEqual(slot[img_id]["status"], "待裁决")
        sched.stop()

    def test_stats_cache_invalidation(self):
        import server
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        store = server.WsStore(tmp)
        store._cache[ws["id"]] = ws
        d1 = store.stats_and_tasks(ws)
        d2 = store.stats_and_tasks(ws)
        self.assertIs(d1["stats"], d2["stats"])   # 命中缓存
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        workspace.put_state(ws, a1["id"], "待裁决", task_id="t1-9")
        d3 = store.stats_and_tasks(ws)
        self.assertIsNot(d1["stats"], d3["stats"])  # mutation 后失效重算
        self.assertGreater(ws["_mut_seq"], 0)

    def test_schedule_flush_persists_edits(self):
        import server
        tmp = tempfile.mkdtemp()
        ws = self._mk_ws(tmp)
        store = server.WsStore(tmp)
        store._cache[ws["id"]] = ws
        a1 = next(i for i in ws["images"] if i["name"] == "a1.jpg")
        workspace.put_state(ws, a1["id"], "已修改", note="P2", task_id="t1-9")
        store.schedule(ws)
        store.flush(ws["id"])
        disk = json.load(open(os.path.join(tmp, ws["id"], "workspace.json"),
                              encoding="utf-8"))
        self.assertEqual(disk["task_states"]["t1-9"][a1["id"]]["status"], "已修改")
        store.stop_and_flush(ws["id"])


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
        # CSV 预览与导出同源（表头含新增两列）
        r = c.get(f"/api/workspaces/{ws_id}/csv")
        self.assertEqual(r.status_code, 200)
        csv_head = r.data.decode("utf-8-sig").split("\r\n")[0]
        self.assertEqual(csv_head,
                         "文件名,split,原始框数,状态,修改说明或疑问,变更摘要,逐框明细")
        # 导出
        out = os.path.join(self.pkg_root, "review_fix_done")
        r = c.post(f"/api/workspaces/{ws_id}/export",
                   json={"output_dir": out, "overwrite": True})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(os.path.exists(os.path.join(out, "review_01", "val", "labels", "b1.txt")))
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
