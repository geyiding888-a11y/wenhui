"""列语义对齐：判断"下级表的这一列，到底是标准表里的哪一列"。

这是整个项目里**唯一真正需要 AI** 的地方，也是 LangChain 的用武之地。

## 为什么规则搞不定

"单位 / 部门 / 所在学院 / 二级单位 / 填报单位 / 院系"——这六个写法在六份表里
指的是同一件事。用规则也能写死一个同义词表，但你永远写不全：

- 明年新来个单位，写的是"教学单位"
- 有人写"归口部门"
- 有人中英文混着写"Dept."

规则表会越来越长，还是漏。而模型天生就懂这些词是一个意思。

## 为什么模型只看列名，不碰数据

模型看的是**列名 + 几个打了码的样本值**，判断这一列的**含义**。
真正的搬运、计算、合并全部由普通 Python 代码做。
这样分工：结果可复现、可测试、出错能定位——AI 只负责它不可替代的那部分。

## 拿不准怎么办

模型必须从给定的选项里挑，**没有"自己编一个字段名"的选项**（见
``build_mapping_schema``）。而且它要如实报告把握程度，
把握低于阈值的一律标成"待确认"交给用户。用户改过一次之后，
改正会写进缓存，下次同样表头直接按你教的对齐。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, create_model

from ..config import AggregateSettings
from ..llm import LLMClient, LLMError
from ..privacy import build_masked_samples
from .field_kinds import is_group_field
from .reader import ColumnInfo, SheetData

#: 规则兜底时，列名相似度到这个程度就认为是一列
_RULE_SIMILARITY = 0.6

# --------------------------------------------------------------------------
# 两个哨兵值
#
# 为什么需要它们：如果只用 None 表示"没对应上"，那么"序号列（本来就不用管）"
# 和"这一列我拿不准"会被混为一谈——结果就是每个序号、备注列都被标成"待确认"，
# 满屏都是黄的，真正的问题反而看不见了。所以必须分开：
# --------------------------------------------------------------------------

#: 这一列与标准表无关（序号、备注、填表说明），**放心忽略**
IGNORE_FIELD = "__IGNORE__"

#: 拿不准，**请人工确认**
UNKNOWN_FIELD = "__UNKNOWN__"

#: 序号/备注这类列，基本可以确定不需要进总表
_IGNORABLE_RE = re.compile(r"^(序号|编号|no\.?|备注|说明|填表说明|注)$", re.IGNORECASE)


class _OneMapping(BaseModel):
    """模型对**一列**的判断。"""

    source_column: str = Field(description="下级表里的原始列名，必须原样抄写，一个字都不能改")
    target_field: str = Field(description="对应标准表的哪一列")
    confidence: float = Field(ge=0.0, le=1.0, description="把握程度 0~1")
    reason: str = Field(default="", description="一句话说明判断依据")


def build_mapping_schema(target_fields: list[str]) -> type[BaseModel]:
    """给这一份表**动态生成**一个 Pydantic 模型，把可选字段编译成枚举。

    这是防"模型编造字段名"最有效的一招。不这样做的话，模型可能返回
    "学生数量"，而标准表里只有"学生人数"——你就得写代码去猜它想说什么，
    或者更糟：静默产生一列没人认识的数据。

    用 ``Literal`` 约束之后，**模型在物理上只能从给定选项里挑**，
    返回值必然是标准字段之一、或者是两个哨兵值之一。
    """
    choices = tuple(target_fields) + (IGNORE_FIELD, UNKNOWN_FIELD)

    one = create_model(
        "_OneMappingForThisSheet",
        __base__=_OneMapping,
        target_field=(
            Literal[choices],  # type: ignore[valid-type]
            Field(
                description=(
                    "对应标准表的哪一列。"
                    f"与标准表无关的列（序号、备注、填表说明）填 {IGNORE_FIELD}；"
                    f"拿不准的填 {UNKNOWN_FIELD}。**绝对不要猜**。"
                )
            ),
        ),
    )
    return create_model(
        "ColumnMappingList",
        mappings=(list[one], Field(description="每一列的映射结果，一列都不能漏")),
    )


@dataclass
class MappingResult:
    """一份表的对齐结果。"""

    mapping: dict[str, str | None] = field(default_factory=dict)   # 原列名 -> 标准字段
    confidence: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    source: str = "llm"          # 'cache' | 'llm' | 'rule' | 'user'
    error: str = ""

    @property
    def ignored(self) -> set[str]:
        """确定不需要进总表的列（不打扰用户）。"""
        return {c for c, v in self.mapping.items() if v is None and self.confidence.get(c, 0) >= 0.7}

    @property
    def uncertain(self) -> set[str]:
        """需要用户确认的列。"""
        return {
            c
            for c, v in self.mapping.items()
            if v is not None and v != IGNORE_FIELD and v == UNKNOWN_FIELD
        } | {
            c
            for c, v in self.mapping.items()
            if v is not None and v != IGNORE_FIELD and v != UNKNOWN_FIELD and self.confidence.get(c, 0) < 0.7
        }


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""你是一位高校数据汇总助手。你的唯一任务是：判断下级单位交上来的表格里，\
每一列对应到学校标准表的哪一列。

工作要求：

1. **按含义对应，不按字面**。"单位"、"所在学院"、"二级单位"、"院系"、"部门"、"填报单位"\
指的是同一件事，都该对到标准表的同一列。
2. **无关的列填 {IGNORE_FIELD}**。序号、备注、填表说明、空白列这类，\
本来就不该进总表——**放心填 {IGNORE_FIELD}，不要犹豫**。
3. **拿不准就填 {UNKNOWN_FIELD}，绝对不要猜**。错配会静默产生错误数据，\
比留空有害得多——留空会被人看到并处理，错配不会。
4. **confidence 要如实反映把握**：
   - 0.9 以上：列名和样本值都非常明确
   - 0.6~0.9：含义基本清楚，但表述不标准
   - 0.6 以下：只能靠猜
   如果你发现自己在给每一列都打 0.95，说明你没有认真评估。
5. **样本值已脱敏**（身份证、手机号打了码）。你只需要看它们的**形态**\
判断列的含义——"18 位数字"是身份证号，"2026-09-01"是日期。\
你不需要也无法知道真实内容。
6. source_column 必须**原样抄写**输入里给出的列名，一个字符都不能改，\
否则对应关系会错位。

（回答格式：程序会指定一个 JSON 结构让你填。你只管按它的要求填内容，
不用自己组织输出格式。）"""


def _format_fields(target_fields: list[str], field_samples: dict[str, list[str]]) -> str:
    lines = []
    for name in target_fields:
        samples = field_samples.get(name) or []
        sample_text = "、".join(samples) if samples else "（暂无样本）"
        lines.append(f"- {name}    样本：{sample_text}")
    return "\n".join(lines)


def _format_columns(columns: list[ColumnInfo], masked_samples: dict[str, list[str]]) -> str:
    lines = []
    for col in columns:
        samples = masked_samples.get(col.name) or []
        sample_text = "、".join(samples) if samples else "（整列都是空的）"
        lines.append(f"- {col.name}    样本：{sample_text}")
    return "\n".join(lines)


def build_prompt(
    columns: list[ColumnInfo],
    target_fields: list[str],
    field_samples: dict[str, list[str]],
    masked_samples: dict[str, list[str]],
) -> str:
    return f"""## 标准表的列（要往这些列上对应）

{_format_fields(target_fields, field_samples)}

## 下级表的列（要判断这些列）

{_format_columns(columns, masked_samples)}

## 请回答

对上面「下级表的列」里的**每一列**都给出结论，一列都不能漏：
能对上就填标准表的列名；无关的填 {IGNORE_FIELD}；拿不准的填 {UNKNOWN_FIELD}。"""


# --------------------------------------------------------------------------
# 规则兜底
# --------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """列名归一化，用于规则匹配。"""
    text = re.sub(r"[\s　]+", "", str(name))
    text = re.sub(r"[（(].*?[)）]", "", text)      # 去掉"（万元）"这类备注
    text = re.sub(r"[·\-_/、,，.。:：]", "", text)
    return text.casefold()


def _similarity(left: str, right: str) -> float:
    """两个列名的相似度：按**字符集合**算重合比例（Jaccard）。

    中文列名按字符集合比按顺序比更靠谱：
    ``"学生人数"`` 和 ``"学生数"`` 重合 3 个字、并集 4 个 → 0.75，认得出是一回事；
    而按顺序比对只有 0.57，会被判成不相干。

    单字列名不参与——"数"和"学"这种，怎么算都容易撞上不相干的字段。
    """
    if len(left) < 2 or len(right) < 2:
        return 0.0
    a, b = set(left), set(right)
    return len(a & b) / len(a | b)


def rule_based_map(
    source_columns: list[str], target_fields: list[str]
) -> dict[str, str | None]:
    """不调 AI 的兜底匹配：完全相同 → 互相包含 → 同类概念 → 字符重合。

    只在模型调用失败、或者用户主动选"不花钱模式"时用。
    结果会标成"待确认"，因为规则匹配的可靠性明显不如模型。
    """
    normalized_targets = {_normalize(t): t for t in target_fields}
    result: dict[str, str | None] = {}
    taken: set[str] = set()

    for column in source_columns:
        key = _normalize(column)

        # 序号、备注这类，规则就能确定不用管
        if _IGNORABLE_RE.match(key):
            result[column] = IGNORE_FIELD
            continue

        # 1) 完全相同
        if key in normalized_targets and normalized_targets[key] not in taken:
            match = normalized_targets[key]
            result[column] = match
            taken.add(match)
            continue

        # 2) 互相包含（"单位" ↔ "填报单位"）
        contained = [
            (norm, original)
            for norm, original in normalized_targets.items()
            if original not in taken and key and (key in norm or norm in key)
        ]
        if len(contained) == 1:
            result[column] = contained[0][1]
            taken.add(contained[0][1])
            continue

        # 3) 同一类概念（"所在学院" ↔ "单位"）
        #    只在标准表里这一类字段**唯一**时才认，否则（比如同时有"单位"
        #    和"部门"）根本分不清该给谁，宁可交给人确认。
        if is_group_field(key):
            same_kind = [
                original
                for norm, original in normalized_targets.items()
                if original not in taken and is_group_field(norm)
            ]
            if len(same_kind) == 1:
                result[column] = same_kind[0]
                taken.add(same_kind[0])
                continue

        # 4) 字符重合度
        best, best_ratio = None, 0.0
        for norm, original in normalized_targets.items():
            if original in taken:
                continue
            ratio = _similarity(key, norm)
            if ratio > best_ratio:
                best, best_ratio = original, ratio
        result[column] = best if best_ratio >= _RULE_SIMILARITY else UNKNOWN_FIELD

    return result


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _resolve_conflicts(
    mapping: dict[str, str | None],
    confidence: dict[str, float],
    reasons: dict[str, str],
) -> None:
    """一列标准字段只能由一列源列填。冲突时就地解决。

    两列都声称自己是"填报单位"时，保留把握大的那个；
    另一个降级为"待确认"，并写明原因——让用户来定夺，而不是默默选一个。
    """
    sentinels = {IGNORE_FIELD, UNKNOWN_FIELD}
    best: dict[str, tuple[str, float]] = {}
    for column, target in mapping.items():
        if not target or target in sentinels:
            continue
        current = best.get(target)
        if current is None or confidence.get(column, 0.0) > current[1]:
            best[target] = (column, confidence.get(column, 0.0))

    for column, target in list(mapping.items()):
        if not target or target in sentinels:
            continue
        winner, _ = best[target]
        if winner != column:
            mapping[column] = UNKNOWN_FIELD
            confidence[column] = min(confidence.get(column, 0.5), 0.4)
            reasons[column] = (
                f"和「{winner}」都想对到「{target}」，程序先把这一列留空，请你确认"
            )


def _cache_applies(cached: dict[str, str | None], target_fields: list[str]) -> bool:
    """这份缓存还适用吗？

    缓存的键是**表头指纹**（只看列名），但只要用户换了模板，
    标准字段就变了，旧映射会指向一个**已经不存在的字段**——
    那一列的数据会被静默丢掉，而且没有任何提示。

    所以只要发现缓存里有一个目标字段不在当前标准表里，就整份作废，
    重新走一遍对齐流程。宁可多花一次 AI 的钱，也不能悄悄丢数据。
    """
    allowed = set(target_fields) | {IGNORE_FIELD, UNKNOWN_FIELD}
    return all(target in allowed for target in cached.values() if target)


def map_sheet(
    sheet: SheetData,
    target_fields: list[str],
    client: LLMClient | None,
    settings: AggregateSettings,
    field_samples: dict[str, list[str]] | None = None,
    cached: dict[str, str | None] | None = None,
    cache_source: str = "cache",
    cached_confidence: dict[str, float] | None = None,
) -> MappingResult:
    """给一份表的列做对齐。

    :param client: 模型客户端；传 ``None`` 表示强制走规则兜底（不花钱的模式）
    :param cached: 已经命中的缓存映射，传了就不再调模型
    :param cached_confidence: 缓存里那一列当初的把握程度。**不能省**——
        忘了它，"上次没把握"的列就会被当成"确定无疑"，再也不会提醒用户。
    """
    source_columns = [c.name for c in sheet.columns]
    result = MappingResult(source=cache_source)

    if cached is not None and _cache_applies(cached, target_fields):
        saved = cached_confidence or {}
        result.mapping = {c: cached.get(c, UNKNOWN_FIELD) for c in source_columns}
        # 缓存里没记把握程度的（老库、或用户手工确认的），按"确定"算：
        # 用户自己定过的当然不用再问。
        result.confidence = {c: float(saved.get(c, 1.0)) for c in source_columns}
        result.reasons = {
            c: ("用你上次确认的结果" if c in saved else "用上次的结果")
            for c in source_columns
        }
        return result
    # 缓存不适用（多半是换了模板）→ 当作没缓存，往下重新对齐

    if client is None:
        result.source = "rule"
        result.mapping = rule_based_map(source_columns, target_fields)
        result.confidence = {
            c: (0.9 if v == IGNORE_FIELD else 0.5) for c, v in result.mapping.items()
        }
        result.reasons = {c: "未连接 AI，用名称相似度匹配" for c in source_columns}
        return result

    # 只把"打了码的几个样本"发出去——真实数据不出门
    masked = {
        col.name: build_masked_samples(col.samples, settings.samples_per_column)
        for col in sheet.columns
    }
    prompt = build_prompt(sheet.columns, target_fields, field_samples or {}, masked)
    schema = build_mapping_schema(target_fields)

    try:
        answer = client.structured(schema, _SYSTEM_PROMPT, prompt)
    except LLMError as exc:
        # 调用失败不能让整个流程崩掉——降级到规则，并如实告诉用户
        result.source = "rule"
        result.mapping = rule_based_map(source_columns, target_fields)
        result.confidence = {
            c: (0.9 if v == IGNORE_FIELD else 0.4) for c, v in result.mapping.items()
        }
        result.reasons = {c: "AI 没调通，用名称相似度匹配" for c in source_columns}
        result.error = str(exc)
        _resolve_conflicts(result.mapping, result.confidence, result.reasons)
        return result

    result.source = "llm"
    allowed = set(target_fields)
    by_column = {m.source_column: m for m in answer.mappings if m.source_column}

    for column in source_columns:
        item = by_column.get(column)
        if item is None:
            # 模型漏答了这一列：宁可交给人，也不猜
            result.mapping[column] = UNKNOWN_FIELD
            result.confidence[column] = 0.0
            result.reasons[column] = "AI 没有回答这一列，请人工确认"
            continue

        target = str(item.target_field).strip()
        if target in (IGNORE_FIELD, UNKNOWN_FIELD):
            result.mapping[column] = target
            result.confidence[column] = float(item.confidence)
            result.reasons[column] = item.reason or ""
        elif target in allowed:
            result.mapping[column] = target
            result.confidence[column] = float(item.confidence)
            result.reasons[column] = item.reason or ""
        else:
            # 兜底：理论上 Literal 已经挡住了，但万一模型走了别的通道返回野字段
            result.mapping[column] = UNKNOWN_FIELD
            result.confidence[column] = 0.2
            result.reasons[column] = (
                f"AI 想对到「{target}」，但标准表里没有这一列，请人工确认"
            )

    _resolve_conflicts(result.mapping, result.confidence, result.reasons)
    return result


def pick_target_fields(
    sheets: list[SheetData], template_columns: list[str] | None = None
) -> list[str]:
    """确定"标准表"有哪些列。

    优先用**用户放进「模板」文件夹的那份表**——这是唯一真正可靠的办法，
    read.md 里要提醒用户放一份。

    没放模板时只能猜：拿列数最多的那份当基准（列最全的通常就是照着
    官方模板做的）。列数并列时，选**列名平均最短**的那份——
    下级单位交表时爱加前缀（"所在学院""二级单位""填报日期"），
    而规范模板的列名最简洁（"单位""姓名""日期"）。这只是并列时的取舍，
    不影响列数不同的情况。
    """
    if template_columns:
        seen: list[str] = []
        for column in template_columns:
            text = str(column).strip()
            if text and text not in seen:
                seen.append(text)
        if seen:
            return seen

    if not sheets:
        return []

    def rank(sheet: SheetData) -> tuple[int, float]:
        names = [c.name.strip() for c in sheet.columns if c.name.strip()]
        if not names:
            return (0, 0.0)
        return (len(names), -sum(len(n) for n in names) / len(names))

    richest = max(sheets, key=rank)
    return [c.name for c in richest.columns if c.name.strip()]


def collect_field_samples(
    sheets: list[SheetData],
    target_fields: list[str],
    limit: int = 3,
) -> dict[str, list[str]]:
    """给标准表的每一列也准备几个样本，帮模型理解这一列该装什么。

    取的是**列名与标准字段相同**的源列的样本。取不到就留空，模型照样能靠列名判断。
    """
    samples: dict[str, list[str]] = {}
    for field_name in target_fields:
        for sheet in sheets:
            for column in sheet.columns:
                if column.name == field_name and column.samples:
                    samples[field_name] = column.samples[:limit]
                    break
            if samples.get(field_name):
                break
    return samples


def read_template_columns(path: Path, settings) -> list[str]:
    """读模板文件，取出它的列名。模板的第一张工作表就是标准表。"""
    from .reader import read_workbook

    sheets = read_workbook(path, settings)
    if not sheets:
        return []
    return [c.name for c in sheets[0].columns if c.name.strip()]
