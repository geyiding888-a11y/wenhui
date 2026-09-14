"""读表：这个项目最容易翻车的一步。

真实的验收标准很朴素——**程序看到的东西，和人眼睛看到的一样**。
"""

from __future__ import annotations

from wenhui.config import load_settings
from wenhui.excel.reader import detect_header_block, flatten_headers, read_workbook

from make_samples import make_file1, make_file2, make_file3


def _read(path):
    return read_workbook(path, load_settings().excel)


# --------------------------------------------------------------------------
# 用故意做乱的样例表验收
# --------------------------------------------------------------------------


def test_多级表头与合并单元格(tmp_path):
    """大标题和"填报单位：…"那两行不能被算进表头。"""
    sheet = _read(make_file1(tmp_path))[0]

    assert sheet.header_rows == [3, 4]
    assert [c.name for c in sheet.columns] == [
        "单位", "姓名", "职称", "课程数", "学生数", "填报日期",
    ]
    assert sheet.merged_count == 3


def test_单位能从表头上方的文字里认出来(tmp_path):
    """下级表常把单位写在"填报单位：XX"里，表体里根本没有这一列。

    认不出来的话，总表的"单位"列会整列空着——整个汇总就废了。
    """
    assert _read(make_file1(tmp_path))[0].title_hint == "计算机学院"


def test_合计行必须剔掉(tmp_path):
    """不剔的话，总表里会多出一条"张三=合计"的假数据。"""
    sheet = _read(make_file1(tmp_path))[0]

    assert sheet.dropped_total_rows == 1
    assert all("合计" not in str(row[0]) for row in sheet.rows)


def test_单行表头(tmp_path):
    sheet = _read(make_file2(tmp_path))[0]
    assert sheet.header_rows == [1]
    assert "所在学院" in [c.name for c in sheet.columns]


def test_漏填的格子读成None而不是丢掉整行(tmp_path):
    """漏填要被如实读出来，交给校验环节去提示——读表阶段不许自作主张。"""
    sheet = _read(make_file3(tmp_path))[0]

    assert sheet.n_rows == 4
    assert None in [row[1] for row in sheet.rows]


# --------------------------------------------------------------------------
# 纯函数：不依赖文件，跑得快
# --------------------------------------------------------------------------


def test_跨整行的大标题不会被当成表头():
    grid = [
        ["某学院2026年统计表"] * 4,          # 合并展开后：一整行重复的同一句话
        ["单位", "姓名", "人数", "日期"],
        ["A学院", "张三", 1, "2026-09-01"],
    ]
    assert detect_header_block(grid, 10) == (1, 1)


def test_两行表头都要收进来():
    grid = [
        ["", "", ""],
        ["基本信息", "基本信息", "教学情况"],
        ["单位", "姓名", "课程数"],
        ["A学院", "张三", 3],
    ]
    assert detect_header_block(grid, 10) == (1, 2)


def test_通用分组名不污染列名():
    """"基本信息·姓名"其实就该叫"姓名"，加前缀只会让人认不出。"""
    block = [["基本信息", "基本信息", "教学情况"], ["姓名", "学号", "课程数"]]
    assert flatten_headers(block) == ["姓名", "学号", "课程数"]


def test_有信息量的分组名必须保留():
    """丢掉"招生"的话，"人数"和"毕业人数"就分不清了。"""
    block = [["招生", "招生"], ["人数", "比例"]]
    assert flatten_headers(block) == ["招生·人数", "招生·比例"]


def test_上层为空时用上层兜底():
    block = [["单位", "备注"], ["", ""]]
    assert flatten_headers(block) == ["单位", "备注"]
