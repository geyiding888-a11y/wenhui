"""列语义对齐：不连 AI 的那部分（规则兜底 + 防编造字段）。

连 AI 的部分没法在单元测试里跑（要花钱、结果还不稳定），
所以这里只测"AI 调不通时兜底靠不靠谱"——而这条路径恰恰最需要测，
因为它在线上是没人看着的。
"""

from __future__ import annotations

import json
from pathlib import Path

from wenhui.config import AggregateSettings
from wenhui.excel.mapper import (
    IGNORE_FIELD,
    UNKNOWN_FIELD,
    _cache_applies,
    build_mapping_schema,
    map_sheet,
    rule_based_map,
)
from wenhui.excel.reader import ColumnInfo, SheetData


def test_完全一样的列名直接对上():
    assert rule_based_map(["单位", "姓名"], ["单位", "姓名"]) == {
        "单位": "单位",
        "姓名": "姓名",
    }


def test_序号备注说明直接忽略不打扰用户():
    """这类列要是被标成"待确认"，满屏都是黄的，真问题就看不见了。"""
    mapping = rule_based_map(["序号", "备注", "姓名"], ["姓名"])
    assert mapping["序号"] == IGNORE_FIELD
    assert mapping["备注"] == IGNORE_FIELD
    assert mapping["姓名"] == "姓名"


def test_包含关系的列名能对上():
    assert rule_based_map(["填报单位"], ["单位"])["填报单位"] == "单位"
    assert rule_based_map(["教师姓名"], ["姓名"])["教师姓名"] == "姓名"


def test_同一类概念能对上():
    """"所在学院"和"单位"字面上毫无重合，但显然是一回事。"""
    assert rule_based_map(["所在学院"], ["单位"])["所在学院"] == "单位"
    assert rule_based_map(["二级单位"], ["单位"])["二级单位"] == "单位"


def test_同类字段不唯一时不许乱配():
    """标准表里同时有"单位"和"部门"，就分不清该给谁——交给人确认。"""
    mapping = rule_based_map(["所在学院"], ["单位", "部门", "人数"])
    assert mapping["所在学院"] == UNKNOWN_FIELD


def test_拿不准的就交给人工绝不硬猜():
    """错配会静默产生错误数据，比留空有害得多。"""
    assert rule_based_map(["课时数"], ["课程数"])["课时数"] == UNKNOWN_FIELD


def test_一个标准字段不会被两列同时占用():
    mapping = rule_based_map(["姓名", "教师姓名"], ["姓名"])
    assert list(mapping.values()).count("姓名") == 1


def test_可选字段被编译成枚举():
    """这是防"模型编造字段名"的关键：它只能从给定选项里挑。"""
    schema = build_mapping_schema(["姓名", "人数"])
    text = json.dumps(schema.model_json_schema(), ensure_ascii=False)

    assert "姓名" in text and "人数" in text
    assert IGNORE_FIELD in text and UNKNOWN_FIELD in text


# --------------------------------------------------------------------------
# 缓存的有效性
# --------------------------------------------------------------------------


def test_缓存里的字段还在就继续用():
    assert _cache_applies({"单位": "单位", "备注": IGNORE_FIELD}, ["单位", "姓名"])


def test_换了模板之后旧缓存必须作废():
    """否则旧映射指向一个不存在的字段，那一列的数据会被**静默丢掉**。"""
    stale = {"所在学院": "所在学院", "教师姓名": "教师姓名"}
    assert not _cache_applies(stale, ["单位", "姓名"])


def test_只含哨兵值的缓存永远有效():
    assert _cache_applies({"序号": IGNORE_FIELD, "备注": None}, ["单位"])


# --------------------------------------------------------------------------
# 缓存命中时，"把握程度"必须一起带回来
#
# 这里防的是一个很隐蔽、后果又很严重的缺陷：缓存命中时无条件把每一列都
# 当成"确定无疑"，于是**上次没把握、需要用户确认的列，这次不再提醒了**。
# 用户看到的是"全部对上了"，而错配就那么留在总表里。
# --------------------------------------------------------------------------


def _sheet(*column_names: str) -> SheetData:
    return SheetData(
        path=Path("某学院.xlsx"),
        sheet_name="Sheet1",
        header_rows=[1],
        columns=[ColumnInfo(index=i, name=n) for i, n in enumerate(column_names)],
        rows=[],
        row_numbers=[],
    )


def test_缓存命中时不把没把握的列说成确定无疑():
    cached = {"所在学院": "单位", "课时数": "课程数"}
    result = map_sheet(
        _sheet("所在学院", "课时数"),
        ["单位", "课程数"],
        client=None,
        settings=AggregateSettings(),
        cached=cached,
        cached_confidence={"所在学院": 0.95, "课时数": 0.3},
    )

    assert result.mapping == cached
    assert result.confidence["所在学院"] == 0.95
    assert result.confidence["课时数"] == 0.3
    # 关键：没把握的那一列，这次仍然要请用户确认
    assert result.uncertain == {"课时数"}


def test_用户手工确认过的缓存不再打扰用户():
    """老库、以及用户自己确认过的映射没有记把握程度，按"确定"算。"""
    result = map_sheet(
        _sheet("所在学院"),
        ["单位"],
        client=None,
        settings=AggregateSettings(),
        cached={"所在学院": "单位"},
        cache_source="user",
        cached_confidence={},
    )
    assert result.source == "user"
    assert not result.uncertain
