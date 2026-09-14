"""生成"故意做乱"的样例表格，用来验收。

为什么要故意做乱：真实下级单位交上来的表就是这个样子——
表头行不在第一行、有合并单元格、列名各不相同、全角空格、
"1.2万"和"12000"混着写、最后有一行合计、还有人漏填。

**如果验收数据是干净的，那验收本身就没有意义。**

运行：``uv run python tests/make_samples.py``
生成到：``tests/samples/``
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side

SAMPLES_DIR = Path(__file__).parent / "samples"

_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _style_header(ws, row: int, n_cols: int) -> None:
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _BORDER


def make_file1(directory: Path = SAMPLES_DIR) -> Path:
    """计算机学院：标题 + 元信息行 + **两行表头带合并单元格** + 合计行。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "师资统计"

    # 第 1 行：大标题（跨列合并）——程序不能把它当表头
    ws["A1"] = "计算机学院2026年师资情况统计表"
    ws.merge_cells("A1:F1")
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    # 第 2 行：元信息——单位写在这里，表体里没有独立的单位列时要用它
    ws["A2"] = "填报单位：计算机学院    填报人：李老师    电话：0571-8888xxxx"

    # 第 3 行：多级表头的上层（合并单元格！）
    ws["A3"] = "基本信息"
    ws.merge_cells("A3:B3")          # 「基本信息」横跨 A、B 两列
    ws["C3"] = "教学情况"
    ws.merge_cells("C3:F3")          # 「教学情况」横跨 C~F 四列
    _style_header(ws, 3, 6)

    # 第 4 行：多级表头的下层
    for col, name in enumerate(["单位", "姓名", "职称", "课程数", "学生数", "填报日期"], start=1):
        ws.cell(row=4, column=col, value=name)
    _style_header(ws, 4, 6)

    # 数据行
    rows = [
        ["计算机学院", "张三", "教授", 3, 120, "2026-09-01"],
        ["计算机学院", "李四", "副教授", 2, 85, "2026-09-01"],
    ]
    for offset, row in enumerate(rows, start=5):
        for col, value in enumerate(row, start=1):
            ws.cell(row=offset, column=col, value=value)

    # 合计行——必须被剔除，否则总表里会多出一条假数据
    ws.cell(row=7, column=1, value="合计")
    ws.cell(row=7, column=4, value=5)
    ws.cell(row=7, column=5, value=205)

    # 尾部留一堆空行（下级表常见）
    for _ in range(3):
        ws.append([])

    path = directory / "计算机学院-师资统计表.xlsx"
    wb.save(path)
    return path


def make_file2(directory: Path = SAMPLES_DIR) -> Path:
    """外国语学院：单行表头，**列名完全不同**，值脏（1.2万 / 2026/9/1 / 全角空格）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    headers = ["所在学院", "教师姓名", "职称", "课时数", "学生人数", "日期"]
    ws.append(headers)
    _style_header(ws, 1, len(headers))

    ws.append(["外国语学院", "王　五", "讲师", "1.2万", "3,500", "2026/9/1"])   # 全角空格 + 万字 + 千分位
    ws.append(["外国语学院", "赵六", "教授", "9800", "２１００", "2026年9月2日"])  # 全角数字 + 中文日期

    path = directory / "外国语学院上报数据.xlsx"
    wb.save(path)
    return path


def make_file3(directory: Path = SAMPLES_DIR) -> Path:
    """二级单位：表头带全角空格，**有一行漏填姓名**，还有一条重复记录。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "上报表"

    ws.append(["二级单位", "姓名", "职称", "课程数", "学生数", "填报日期"])
    _style_header(ws, 1, 6)

    ws.append(["马克思主义学院", "孙七", "讲师", 4, 300, "2026-09-03"])
    ws.append(["马克思主义学院", None, "副教授", 3, 260, "2026-09-03"])       # ← 漏填姓名
    ws.append(["马克思主义学院", "孙七", "讲师", 4, 300, "2026-09-03"])       # ← 重复
    ws.append(["马克思主义学院", "周八", "无", "N/A", "—", "2026/9/4"])       # ← 各种"空"写法

    path = directory / "马克思主义学院-上报表.xlsx"
    wb.save(path)
    return path


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    paths = [make_file1(), make_file2(), make_file3()]
    print("已生成样例文件：")
    for path in paths:
        print(f"  · {path.relative_to(SAMPLES_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
