"""校验：把汇总后的问题挑出来，精确到"哪个文件、第几行、哪一列"。

## 设计原则：宁可少报，不可错报

校验规则最容易犯的错是"太热心"——报一堆假警报，用户看两次就不看了，
真正的问题反而被淹没。所以这里的每条规则都遵循：

- **只在有对比、有依据时才报**。例：某列在别的行都填了，唯独这行空着 →
  才提示漏填。整列全空 → 说明这列本来就不填，**不报**。
- **措辞是"提醒"不是"你错了"**。程序没有资格断定用户填错了，
  它只能说"这里和其他地方不太一样，你看一眼"。
- 每条问题都带 **建议动作**，用户看完知道该干嘛。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .cleaner import parse_date, parse_number
from .field_kinds import is_group_field, is_identity_field

#: 一整列里，数字占比超过这个比例，就认为"这列本该是数字"
_NUMERIC_COLUMN_RATIO = 0.8

#: 一条记录里，非空字段少于这个比例，就提示"这行像是没填完"
_SPARSE_ROW_RATIO = 0.5


@dataclass
class Record:
    """一条合并后的记录，始终带着它的出处。

    ``values`` 是清洗后的值（用于比较、查重），``raw`` 是原始值（用于输出）。
    """

    values: dict[str, object]
    raw: dict[str, object]
    file: str
    sheet: str
    row: int                    # Excel 里的原始行号，从 1 开始

    @property
    def origin(self) -> str:
        if self.sheet:
            return f"{self.file}「{self.sheet}」第 {self.row} 行"
        return f"{self.file} 第 {self.row} 行"


@dataclass
class Issue:
    """一条问题。会被写进「问题清单」交给用户。"""

    level: str                  # "error" 必须处理 / "warning" 请确认 / "info" 仅供参考
    problem: str                # 说清楚是什么问题
    suggestion: str             # 告诉用户该干嘛
    file: str = ""
    sheet: str = ""
    row: int | None = None
    column: str = ""
    value: object = None
    field_order: int = field(default=0, repr=False)

    @property
    def level_label(self) -> str:
        return {"error": "必须处理", "warning": "请确认", "info": "仅供参考"}.get(self.level, self.level)

    @property
    def location(self) -> str:
        """人话版的位置描述。"""
        parts = []
        if self.file:
            parts.append(self.file)
        if self.sheet and self.sheet not in ("Sheet1", "Sheet"):
            parts.append(f"工作表「{self.sheet}」")
        if self.row:
            parts.append(f"第 {self.row} 行")
        if self.column:
            parts.append(f"「{self.column}」列")
        return " ".join(parts) or "（全局）"


def build_key_fields(fields: list[str]) -> list[str]:
    """挑出"组合起来能唯一标识一行"的字段，用来查重复。

    **为什么必须是组合**：单看"单位"，一个学院好几行数据，全都算重复；
    单看"姓名"，两个学院各有一个"张三"，也算重复。两个一起看才对。
    这两个误报都会让用户觉得工具坏了，比漏报严重得多。

    返回空列表表示"找不到任何能标识一行的字段"——这时调用方会退回整行比对。
    """
    group = next((f for f in fields if is_group_field(f)), "")
    # 命中"分组"的字段不再当身份用（"单位名称"是分组，不是身份）
    identity = next(
        (f for f in fields if f != group and is_identity_field(f)),
        "",
    )
    return [f for f in (group, identity) if f]


def _non_empty(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def validate(
    records: list[Record],
    fields: list[str],
    low_confidence_fields: set[str] | None = None,
) -> list[Issue]:
    """跑全部校验规则，返回问题列表。

    :param records: 已清洗的合并记录
    :param fields: 统一后的字段顺序
    :param low_confidence_fields: AI 拿不准的字段（会被提示复核）
    """
    issues: list[Issue] = []
    if not records:
        return issues

    issues.extend(_check_missing(records, fields))
    issues.extend(_check_types(records, fields))
    issues.extend(_check_duplicates(records, fields))
    issues.extend(_check_sparse_rows(records, fields))
    issues.extend(_check_low_confidence(records, fields, low_confidence_fields or set()))

    # 排序：必须是先看的排前面；同级别按文件、行号排，方便照着一张表去改
    level_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (level_rank.get(i.level, 9), i.file, i.row or 0, i.field_order))
    return issues


def _check_missing(records: list[Record], fields: list[str]) -> list[Issue]:
    """漏填检查。

    **关键分寸**：整列全空 → 这列本来就不填，不报。
    只有当"别人都填了、就这行没填"时才提示。

    还有一种情况要单独处理：**某一份表整份都缺这一列**。逐行报的话，
    一份 30 行的表就刷 30 条同样的问题，真正的问题会被淹掉。
    这种合并成一条"这份表缺了这一列"，用户一眼就知道该找谁补。
    """
    issues: list[Issue] = []
    for order, field_name in enumerate(fields):
        filled = [r for r in records if _non_empty(r.values.get(field_name))]
        if not filled:
            continue                        # 整列全空，不报
        if len(filled) == len(records):
            continue                        # 全都填了，没问题

        missing = [r for r in records if not _non_empty(r.values.get(field_name))]
        by_file: dict[str, list[Record]] = defaultdict(list)
        for record in missing:
            by_file[record.file].append(record)

        for file_name, rows in by_file.items():
            file_total = sum(1 for r in records if r.file == file_name)
            if len(rows) == file_total and file_total > 1:
                issues.append(
                    Issue(
                        level="warning",
                        problem=(
                            f"这份表里「{field_name}」一列都没填"
                            f"（共 {file_total} 行），别的表填了"
                        ),
                        suggestion="确认是漏了这一列，还是这份表本来就不需要；确实不需要可以忽略",
                        file=file_name,
                        column=field_name,
                        field_order=order,
                    )
                )
                continue

            for record in rows:
                issues.append(
                    Issue(
                        level="warning",
                        problem=f"「{field_name}」这一格是空的，但其它 {len(filled)} 行都填了",
                        suggestion="确认一下是确实没有这项，还是漏填了；确实没有就写「无」",
                        file=record.file,
                        sheet=record.sheet,
                        row=record.row,
                        column=field_name,
                        field_order=order,
                    )
                )
    return issues


def _check_types(records: list[Record], fields: list[str]) -> list[Issue]:
    """类型一致性检查：一列里绝大多数是数字，某一格却是文字 → 提示。"""
    issues: list[Issue] = []
    for order, field_name in enumerate(fields):
        values = [(r, r.values.get(field_name)) for r in records]
        non_empty = [(r, v) for r, v in values if _non_empty(v)]
        if len(non_empty) < 3:
            continue                        # 样本太少，不下结论

        numeric_hits = [(r, v) for r, v in non_empty if parse_number(v) is not None]
        if len(numeric_hits) / len(non_empty) < _NUMERIC_COLUMN_RATIO:
            continue                        # 这列本来就不是数字列

        offenders = [(r, v) for r, v in non_empty if parse_number(v) is None]
        # 日期也算"不是数字"会误报——单独排除掉
        offenders = [(r, v) for r, v in offenders if parse_date(v) is None]
        for record, value in offenders:
            issues.append(
                Issue(
                    level="warning",
                    problem=f"「{field_name}」这一列多数是数字，这一格却是「{value}」",
                    suggestion="确认一下是不是写错了；如果本来就是文字说明，忽略这条",
                    file=record.file,
                    sheet=record.sheet,
                    row=record.row,
                    column=field_name,
                    value=value,
                    field_order=order,
                )
            )
    return issues


def _normalize_for_compare(value: object) -> str:
    """比较用的归一化：去掉空格、全角、大小写差异。"""
    text = str(value).strip()
    text = re.sub(r"[\s　]+", "", text)
    return text.casefold()


def _describe_key(record: Record, key_fields: list[str]) -> str:
    """把一条记录的"身份"说成人话，用在重复提示里。"""
    if not key_fields:
        return "整行内容"
    return "、".join(f"{f}「{record.values.get(f)}」" for f in key_fields)


def _dup_key(record: Record, key_fields: list[str]) -> tuple[str, int] | None:
    """算一条记录的查重键，返回 ``(键, 实际用上了几个字段)``。

    空字段**跳过而不是放弃**：下级表里"单位"常常整列没填（写在标题里），
    要是碰到空值就整行不查重，那这张表的重复就永远抓不到了。

    但"用上了几个字段"要一起返回——用得越少，这个键越不可信：
    只靠"姓名"拼出来的键，两个学院各有一个张三就会撞上，
    所以调用方会据此把结论从"必须处理"降成"请确认"。
    """
    if key_fields:
        parts: list[str] = []
        for name in key_fields:
            value = record.values.get(name)
            if _non_empty(value):
                parts.append(_normalize_for_compare(value))
        return ("|".join(parts), len(parts)) if parts else None

    # 没有可用字段，退回"整行比对"：每一列都一样才算重复。
    # 这是最保守的做法，几乎不可能误报。
    parts = [
        _normalize_for_compare(record.values.get(name)) for name in sorted(record.values)
    ]
    if not any(parts):
        return None
    return ("|".join(parts), len(key_fields) or 2)


def _check_duplicates(records: list[Record], fields: list[str]) -> list[Issue]:
    """重复检查：同一单位下的同一个人出现两次，通常是重复填报。"""
    key_fields = build_key_fields(fields)

    seen: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        entry = _dup_key(record, key_fields)
        if entry is not None:
            seen[entry[0]].append(record)

    issues: list[Issue] = []
    order = fields.index(key_fields[0]) if key_fields else 0
    for _key, group in seen.items():
        if len(group) < 2:
            continue
        first = group[0]
        for record in group[1:]:
            same_file = record.file == first.file
            # 同一份表里出现两次 → 基本可以断定是重复填报。
            # 跨文件重名则要看情况：得"单位+姓名"两个字段都填全了才敢断言，
            # 只靠"姓名"一个字的话，两个学院各有一个张三也很正常。
            used = _dup_key(record, key_fields)
            confident = same_file or (len(key_fields) >= 2 and used is not None and used[1] >= 2)
            issues.append(
                Issue(
                    level="error" if confident else "warning",
                    problem=(
                        f"{_describe_key(record, key_fields)}和 {first.origin} 重复了"
                    ),
                    suggestion=(
                        "如果是重复填报，删掉其中一份；"
                        "如果确实有两行，请在表里加一列区分（如序号、班级）"
                        if confident
                        else "确认一下是不是同一个人；重名的话忽略这条"
                    ),
                    file=record.file,
                    sheet=record.sheet,
                    row=record.row,
                    column=key_fields[0] if key_fields else "",
                    value=_describe_key(record, key_fields),
                    field_order=order,
                )
            )
    return issues


def _check_sparse_rows(records: list[Record], fields: list[str]) -> list[Issue]:
    """整行大面积空的，多半是填了一半就交了。"""
    if len(fields) < 3:
        return []
    issues: list[Issue] = []
    for record in records:
        filled = sum(1 for f in fields if _non_empty(record.values.get(f)))
        if filled == 0:
            continue                        # 全空行在读取阶段已经丢掉了
        if filled / len(fields) < _SPARSE_ROW_RATIO:
            issues.append(
                Issue(
                    level="warning",
                    problem=f"这一行只填了 {filled}/{len(fields)} 项，看起来没填完",
                    suggestion="确认这份表是不是交早了、或者只填了一部分",
                    file=record.file,
                    sheet=record.sheet,
                    row=record.row,
                    field_order=len(fields),
                )
            )
    return issues


def _check_low_confidence(
    records: list[Record], fields: list[str], low_confidence: set[str]
) -> list[Issue]:
    """AI 拿不准的列，单独提醒用户去复核。"""
    if not low_confidence:
        return []
    issues: list[Issue] = []
    for order, field_name in enumerate(fields):
        if field_name not in low_confidence:
            continue
        issues.append(
            Issue(
                level="info",
                problem=f"「{field_name}」这一列，程序不太确定自己对得对不对",
                suggestion="在界面上的「待确认」区域看一眼，点一下确认或改正即可",
                column=field_name,
                field_order=order,
            )
        )
    return issues
