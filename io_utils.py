# -*- coding: utf-8 -*-
"""编码/换行/原子写/路径守卫等基础工具。

Windows 下数据集文件混杂编码（classes.txt CRLF、CSV 为 UTF-8 BOM + CRLF），
所有读写必须显式指定，避免 GBK 控制台与文件编码陷阱。
"""
import os
import json
import shutil
import tempfile

ENCODING = "utf-8"


def read_text(path, encoding="utf-8-sig"):
    """按 utf-8-sig 读取文本（兼容 BOM/无 BOM），统一返回无 BOM 的 str。"""
    with open(path, "r", encoding=encoding, newline="") as f:
        return f.read()


def read_lines(path):
    """读取文本行，去掉 CRLF/LF 行尾与行尾空白。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [line.rstrip("\r\n") for line in f]


def atomic_write_bytes(path, data: bytes):
    """原子写二进制：先写临时文件再 os.replace，写入中断不损坏原文件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text: str, encoding="utf-8", newline="\n"):
    """原子写文本，显式控制编码与换行。"""
    data = text.replace("\r\n", "\n").replace("\n", newline).encode(encoding)
    atomic_write_bytes(path, data)


def atomic_write_json(path, obj, indent=None):
    """原子写 JSON（ensure_ascii=False，中文可读）。"""
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text + "\n")


def fmt_float(v, decimals=6):
    """坐标格式化：固定小数位（去掉无意义的尾随零，但保留 6 位内精度）。"""
    return ("{:." + str(decimals) + "f}").format(float(v))


def is_within(child_path, parent_path):
    """判断 child 是否在 parent 目录内（含自身），用于路径守卫。"""
    child = os.path.abspath(child_path)
    parent = os.path.abspath(parent_path)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def assert_paths_safe(input_dir, output_dir):
    """守卫：输出目录不得等于输入目录，也不得位于输入目录之内（防止覆盖清洗）。"""
    if is_within(output_dir, input_dir):
        raise ValueError(
            "输出目录不得位于输入目录之内（会破坏只读的原始数据）："
            f"output={output_dir} input={input_dir}"
        )


def copy_file_bytes(src, dst):
    """字节级复制（保留 BOM/CRLF 等原始字节）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(src, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def file_hash(path, algo="md5", chunk=1 << 20):
    """文件哈希（用于导出前后输入完整性校验）。"""
    import hashlib

    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dir_tree_hash(root, exclude_names=None):
    """整目录内容哈希（相对路径+文件 md5），用于断言输入目录未被修改。"""
    exclude = set(exclude_names or [])
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for fn in sorted(filenames):
            if fn in exclude:
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            items.append(f"{rel}:{file_hash(p)}")
    import hashlib

    return hashlib.md5("\n".join(items).encode("utf-8")).hexdigest()
