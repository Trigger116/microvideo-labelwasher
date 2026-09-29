# microvideo-labelwasher · 图片清洗工具

面向 YOLO 检测数据集人工清洗的一站式工具：**包扫描 → 分层任务审核（框级 T1 / 图级 T2）→ 审计导出**。
输入目录全程只读，所有改动记录在工作区，审核完成后一键导出 `<输入根>_done` 完整数据包。

## 快速开始

### 方式一：打包版（无需 Python 环境）
1. 解压 `dist/microvideo-labelwasher.zip`
2. 双击 `microvideo-labelwasher.exe`（自动打开浏览器 http://127.0.0.1:PORT）
3. 首次使用：点击 📂 打开文件夹（本地目录选择器）或逐级浏览到数据包父目录 → 单击数据包（📦 标记）→ 创建审查工作区

### 方式二：源码运行
```bash
pip install flask pillow pyinstaller   # 打包时才需要 pyinstaller
python main.py                          # 或 python main.py --port 8899 --no-browser
```

## 目录约定

```
输入（只读）:
  <数据根>/
    <包名>/
      classes.txt            类名（按行，ID 从 0 起）
      类别说明.csv            可选：class_id,english_name,中文参考
      复核记录.csv            可选：文件名,split,原始框数,状态,修改说明或疑问
      train/{images,labels}/  val/{images,labels}/    YOLO 标签: class cx cy w h

工作区（工具写入）:
  workspaces/ws_YYYYmmdd_HHMMSS/workspace.json  原子写入 + 50 次/份滚动备份

输出:
  <数据根>_done/
    <包名>/                 结构与输入一致
      classes.txt / 类别说明.csv / 复核记录.csv（更新状态列）
      train|val/{images,labels}/   图片复制 + 清洗后标签（6 位小数）
```

## 审核流程

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| 包扫描 | 自动检查：缺图/缺标签、坏行、越界框、类别分布 | 无预警或已了解 |
| **T1 框级核验** | 10 个易混淆类别任务（交警/警车/危险品车/养护施工车/防撞车/客车/摩托/救护消防/施工人员/行人），判类证据准则显示在任务横幅；可疑目标用裁剪面板 **↓快速降级** | 每类框级 ✓ 全部核验 |
| **T2 全图漏标扫视** | 批次（与 plan/batches 同源，无清单时按 t2_rules 自算）：低框数批次逐图细扫查漏标，高密度批次查边缘截断与小目标 | 每批全部终态 |
| 审计导出 | 类别转换明细、增删框统计、待裁决清单；严格模式拒绝未复核残留 | 导出 `_done` 包 |

图片状态机：`未复核 → 已核验无修改 / 已修改 / 待裁决`。
框有差异时自动派生"已修改"；有差异时禁止标记"已核验无修改"；撤销至无差异且无备注自动回退"未复核"。

## 界面

- **主画布**：原图 + 全部框（类色描边/12% 填充；已修改框橙色；越界框红色虚线；类名 chip）
- **裁剪面板**：每个框 96×96 裁剪缩略图（外扩 1.8 倍），T1 任务只显示目标类；点击定位放大；✓ 框级核验标记；↓ 快速降级按钮
- **任务面板**：T1/T2 进度（框级/图级）、当前批次关键任务目标横幅
- **特殊类别高亮**：关注类（⭐）与身份类在 config.json 中配置独立颜色

## 快捷键

| 键 | 功能 |
|---|---|
| ← / → | 上一张 / 下一张（自动保存） |
| ↑ / ↓ | 上一个 / 下一个框 |
| 空格 | 浏览模式 ⇄ 标注模式 |
| Tab | 跳到下一个未核验目标框（跨图） |
| 1-9, 0 | 选中框改类（类 ID 0-9）；无选中时设为新框默认类 |
| Shift+1-9, Shift+0 | 设置新框默认类 |
| D | 删除选中框 |
| E | 改类弹窗（选中框） |
| N | 进入标注模式（画新框） |
| S | 标记"已核验无修改" |
| Ctrl+Z / Ctrl+Y | 撤销 / 重做 |
| Ctrl+S | 立即保存 |
| + / - / 0 | 放大 / 缩小 / 适配窗口 |
| F | 画布全屏 |
| G | 跳转任务内图片序号 |
| 滚轮 | 以光标为中心缩放 |
| 右键框 | 改类 / 框级核验 / 删除 / 图片待裁决 |

标注模式鼠标：拖框内=移动；8 向手柄=缩放（对角/对边固定，最小 4px）；空白拖拽=画新框；双击框=改类。

## 配置（config.json）

- `classes`：类别 `name`（英文名，界面标签展示）/`zh`（仅用于任务描述）/颜色/`identity`（身份类）/`focus`（关注类）
- `t1_tasks`：任务 `class_ids` + `banner`（任务目标）+ `criteria`（判类证据）+ `degrade`（快速降级映射）
- `t2_rules`：无批次清单时的自算规则（low_box_max / high_box_min / batch_size）
- `boundary_tolerance_px` / `move_epsilon` / `export{strict, csv_bom, decimals}` / `ui{autosave_ms, undo_limit}`

批次清单（plan/batches/*.txt，TSV：`train/images/xxx.jpg\t框数\t备注`）由 `create_workspace` 自动探测（`<输入根>/../plan/batches`），与人工任务工序清单同源。

## 测试与构建

```bash
python -m unittest tests.test_core        # 18 个单元/API 测试（真实包断言由 LABELWASH_REAL_PKG_ROOT 等环境变量驱动，未设置自动跳过）
build.bat                                  # PyInstaller onedir + zip
```

## 单实例

重复启动 exe 时自动复用已运行实例并打开浏览器；日志写 `logs/app.log`（UTF-8）；工作区在 exe 同级 `workspaces/`。
