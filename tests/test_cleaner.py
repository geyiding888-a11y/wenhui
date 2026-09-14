"""清洗：把"人随手写的值"认出来，但认不出的一律不动。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from wenhui.excel.cleaner import is_empty, normalize_text, parse_date, parse_number

EMPTY_MARKERS = ("无", "没有", "N/A", "—", "/", "-")


def test_全角转半角并去空格():
    assert normalize_text("王　五") == "王五"
    assert normalize_text("１２３") == "123"
    assert normalize_text(" 张三 ") == "张三"


def test_数字的各种写法():
    assert parse_number("1.2万") == Decimal("12000")
    assert parse_number("3,500") == Decimal("3500")
    assert parse_number("￥1200") == Decimal("1200")
    assert parse_number("５００") == Decimal("500")
    assert parse_number(120) == Decimal("120")
    assert parse_number(12.5) == Decimal("12.5")


def test_百分比():
    assert parse_number("50%") == Decimal("0.5")


def test_解析不了就返回None而不是抛异常():
    """清洗函数在任何时候都不该把流程炸掉。"""
    for value in ("无", "约二十", "张三", "", None, True, [1, 2]):
        assert parse_number(value) is None


def test_零和False是有效数据不算空():
    """很多表里 0 是有意义的，不能当成没填。"""
    assert not is_empty(0, EMPTY_MARKERS)
    assert not is_empty(False, EMPTY_MARKERS)
    assert not is_empty(Decimal("0"), EMPTY_MARKERS)


def test_各种没填的写法都算空():
    for text in ("无", "N/A", "—", "/", "", "   ", "　"):
        assert is_empty(text, EMPTY_MARKERS)


def test_日期的各种写法():
    assert parse_date("2026/9/1") == date(2026, 9, 1)
    assert parse_date("2026-09-01") == date(2026, 9, 1)
    assert parse_date("2026年9月1日") == date(2026, 9, 1)
    assert parse_date("20260901") == date(2026, 9, 1)
    assert parse_date(date(2026, 9, 1)) == date(2026, 9, 1)


def test_Excel日期序列号():
    serial = (date(2026, 9, 1) - date(1899, 12, 30)).days
    assert parse_date(serial) == date(2026, 9, 1)


def test_不像日期的数字不能硬认成日期():
    """年份超出合理范围、月份日数不存在的，都得拒绝。"""
    for value in ("18990101", "20261301", "20260230", "999"):
        assert parse_date(value) is None
