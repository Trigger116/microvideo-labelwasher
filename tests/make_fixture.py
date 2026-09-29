# -*- coding: utf-8 -*-
"""生成迷你测试包：覆盖 BOM CSV / CRLF classes / 空标签图 / 多扩展名 等陷阱。"""
import os

from PIL import Image


def make_fixture(root):
    """在 root 下生成 review_fix/review_01 测试包，返回包目录。"""
    pkg = os.path.join(root, "review_fix", "review_01")
    for split in ("train", "val"):
        os.makedirs(os.path.join(pkg, split, "images"), exist_ok=True)
        os.makedirs(os.path.join(pkg, split, "labels"), exist_ok=True)

    classes = ["builder", "person", "sedan", "truck"]
    with open(os.path.join(pkg, "classes.txt"), "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(classes) + "\r\n")

    with open(os.path.join(pkg, "类别说明.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("class_id,english_name,中文参考\r\n")
        for i, n in enumerate(classes):
            f.write(f"{i},{n},类别{i}\r\n")

    with open(os.path.join(pkg, "复核记录.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("文件名,split,原始框数,状态,修改说明或疑问\r\n")
        f.write("a1.jpg,train,2,未复核,\r\n")
        f.write("a2.png,train,1,未复核,\r\n")
        f.write("a3.jpg,train,0,未复核,\r\n")
        f.write("b1.jpg,val,1,未复核,\r\n")

    imgs = {
        "a1.jpg": ((160, 90), ["0 0.25 0.5 0.2 0.3", "2 0.6 0.5 0.3 0.4"]),
        "a2.png": ((100, 80), ["3 0.5 0.5 0.4 0.5"]),
        "a3.jpg": ((120, 60), []),  # 空标签图
        "b1.jpg": ((90, 70), ["1 0.5 0.4 0.3 0.3"]),
    }
    for fn, (size, labels) in imgs.items():
        split = "val" if fn.startswith("b") else "train"
        img = Image.new("RGB", size, (120, 140, 160))
        img.save(os.path.join(pkg, split, "images", fn))
        stem = os.path.splitext(fn)[0]
        with open(os.path.join(pkg, split, "labels", stem + ".txt"), "w", encoding="utf-8") as f:
            if labels:
                f.write("\n".join(labels) + "\n")
    return pkg


if __name__ == "__main__":
    import tempfile

    d = tempfile.mkdtemp(prefix="fixture_")
    p = make_fixture(d)
    print(p)
