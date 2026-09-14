"""清洗：把"人随手写的值"变成"机器能比较的值"。

下级交上来的表里，同一个事实有十几种写法：

- 空格：全角空格（U+3000）、不间断空格、首尾空格、数字中间的空格
- 日期：``2026/9/1``、``2026-09-01``、``2026年9月1日``、``20260901``、
  还有 Excel 存成数字序列的 ``46266``
- 数字：``1.2万``、``3,500``、``５００``（全角）、``￥1200``、``12%``
- 空值：``无``、``没有``、``—``、``/``、``N/A``、``NULL``

不清洗的话，"1.2万"和"12000"会被当成两个不同的值，重复行检测、合计核对全会失灵。

**一条重要分寸**：清洗只针对**程序要比较的字段**，并且**永远保留原始值**。
拿不准的一律不动——把正常数据改乱，比脏数据更糟。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------
# 基础文本处理
# --------------------------------------------------------------------------

#: 需要清掉的空格类字符：普通空格、全角空格、不间断空格、零宽字符、制表符
_SPACE_CHARS = " 　 ​‌‍\t\r\n"
_SPACE_TABLE = {ord(ch): None for ch in _SPACE_CHARS}

#: 全角转半角（数字、字母、常见符号）
_FULLWIDTH_TABLE = {
    **{0xFF01 + i: 0x21 + i for i in range(94)},   # ！ ～ ～
    ord("　"): 0x20,                            # 全角空格
}

_NUMERIC_CLEAN_RE = re.compile(r"[,\s，、]")
_CURRENCY_CHARS = "￥$¥€£"
_UNIT_MULTIPLIERS: dict[str, Decimal] = {
    "万": Decimal(10000),
    "萬": Decimal(10000),
    "亿": Decimal(100_000_000),
    "億": Decimal(100_000_000),
    "千": Decimal(1000),
    "k": Decimal(1000),
    "K": Decimal(1000),
    "w": Decimal(10000),
    "W": Decimal(10000),
}
_NUMBER_WITH_UNIT_RE = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*([万亿萬億千kKwW])?元?$")

_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$"),   # 缺年份
)

#: Excel 把 1900-01-01 记作 1，但历史上误把 1900 当闰年，所以有个 60 的偏移。
#: 用 1899-12-30 作基准可以自动把这个错误抵消掉。
_EXCEL_EPOCH = datetime(1899, 12, 30)

#: 合理的日期区间——超出这个范围的"数字日期"多半不是日期（比如学号 20260001）
_MIN_YEAR, _MAX_YEAR = 1970, 2100


@dataclass
class CleanResult:
    """清洗一个值的结果。

    :param value: 清洗后的值（可能数字、日期、文本，或 ``None``）
    :param changed: 是否真的改动了——用于给用户报告"我动了哪些格子"
    """

    value: object
    changed: bool


def normalize_text(text: str) -> str:
    """全角转半角 + 去掉各种奇怪空格。"""
    text = text.translate(_FULLWIDTH_TABLE)
    text = text.translate(_SPACE_TABLE)
    # NFKC 会把 ① → 1、Ⅻ → XII 之类的兼容字符统一掉，方便比较
    return unicodedata.normalize("NFKC", text).strip()


def is_empty(value: object, empty_markers: tuple[str, ...]) -> bool:
    """判断是不是"等同于没填"。

    ``0`` 和 ``False`` **不算空**——它们是有效数据，很多表里 0 是有意义的。
    """
    if value is None:
        return True
    if isinstance(value, str):
        text = normalize_text(value)
        if not text:
            return True
        return text in {normalize_text(m) for m in empty_markers}
    return False


def parse_number(value: object) -> Decimal | None:
    """尝试把值解析成数字。解析不了返回 ``None``（**不抛异常**）。

    能处理：``12000``、``"12,000"``、``"1.2万"``、``"￥1200"``、``"50%"``、``"５００"``
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        # 浮点转 Decimal 要先转字符串，否则 0.1 会变成 0.1000000000000000055…
        return Decimal(str(value))
    if not isinstance(value, str):
        return None

    text = normalize_text(value)
    if not text:
        return None

    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()

    text = text.lstrip(_CURRENCY_CHARS).strip()
    text = _NUMERIC_CLEAN_RE.sub("", text)
    if not text:
        return None

    match = _NUMBER_WITH_UNIT_RE.match(text)
    if not match:
        return None

    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None

    unit = match.group(2)
    if unit:
        number *= _UNIT_MULTIPLIERS[unit]
    if percent:
        number = number / Decimal(100)
    return number


def parse_date(value: object) -> date | None:
    """尝试把值解析成日期。解析不了返回 ``None``。

    会**保守**判断：像 ``20260001`` 这种既可能是日期也可能是编号的，
    只有在能构成合理日期（年份 1970~2100、月份 1~12、日 1~31）时才认。
    """
    if value is None or isinstance(value, bool):
        return None

    # Excel 里的日期常常是"距 1900 年的天数"
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_excel_serial(float(value))

    if not isinstance(value, str):
        return None

    text = normalize_text(value)
    if not text:
        return None

    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groups()
        try:
            if len(groups) == 3:
                year, month, day = (int(g) for g in groups)
            else:
                year = datetime.now().year
                month, day = (int(g) for g in groups)
        except (TypeError, ValueError):
            continue
        if not (_MIN_YEAR <= year <= _MAX_YEAR):
            continue
        try:
            return date(year, month, day)
        except ValueError:
            continue

    # 纯数字也可能是 Excel 序列号（如 46266 → 2026-09-01）
    if text.isdigit() and len(text) <= 6:
        serial = _from_excel_serial(float(text))
        if serial is not None:
            return serial
    return None


def _from_excel_serial(serial: float) -> date | None:
    """把 Excel 的日期序列号转成日期。范围不合理就返回 ``None``。"""
    if serial <= 0:
        return None
    try:
        result = (_EXCEL_EPOCH + timedelta(days=serial)).date()
    except (OverflowError, ValueError):
        return None
    return result if _MIN_YEAR <= result.year <= _MAX_YEAR else None


def clean_text(value: object, empty_markers: tuple[str, ...] = ()) -> CleanResult:
    """默认清洗：去掉奇怪空格、全角转半角；"无/—/N/A"这类统一成空。"""
    if value is None:
        return CleanResult(None, False)
    if not isinstance(value, str):
        return CleanResult(value, False)

    original = value
    text = normalize_text(value)
    if not text or (empty_markers and text in {normalize_text(m) for m in empty_markers}):
        return CleanResult(None, original != "")
    return CleanResult(text, text != original)
