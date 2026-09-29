# -*- coding: utf-8 -*-
"""默认配置加载/合并/校验。"""
import json
import os
import sys

from io_utils import read_text

_DEFAULT_CONFIG = None


def resource_path(rel):
    """源码运行与 PyInstaller 冻结两种形态下解析资源路径。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def load_default_config():
    """加载 tools/microvideo-labelwasher/config.json（含内置类别配色与任务规则）。"""
    global _DEFAULT_CONFIG
    if _DEFAULT_CONFIG is None:
        _DEFAULT_CONFIG = json.loads(read_text(resource_path("config.json")))
    return _DEFAULT_CONFIG


def merge_config(base, override):
    """浅合并两层 dict（第二层为工作区级覆盖）。"""
    if not override:
        return base
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = dict(merged[k])
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def validate_config(cfg):
    """校验配置关键约束，返回错误消息列表（空=通过）。"""
    errs = []
    classes = cfg.get("classes") or []
    ids = [c.get("id") for c in classes]
    if len(ids) != len(set(ids)):
        errs.append("classes 存在重复 id")
    for c in classes:
        for k in ("id", "name", "zh", "color"):
            if k not in c:
                errs.append(f"classes 项缺少字段 {k}: {c}")
                break
        if not c.get("color", "").startswith("#"):
            errs.append(f"类别 {c.get('name')} 颜色不是 #RRGGBB: {c.get('color')}")
    for t in cfg.get("t1_tasks") or []:
        if not t.get("class_ids"):
            errs.append(f"T1 任务 {t.get('id')} 缺少 class_ids")
    return errs


def classes_as_map(cfg):
    return {c["id"]: c for c in cfg.get("classes") or []}
