"""脱敏：把敏感信息打码之后再发给大模型。

## 为什么这样设计

模型在这一套流程里**只需要判断"这一列是什么意思"**。它看的是列名，加上几个
样本值的**形状**——"这是 18 位数字，像身份证"、"这是日期"。它不需要知道
张三的身份证号到底是多少。

所以：打码后模型的判断力几乎不受影响，而真实的个人信息**从未离开这台电脑**。

## 一条必须记住的边界

脱敏**只作用于"发给模型的那几个样本值"**。
你自己拿到的汇总表、问题清单里，仍然是**完整的原文**。

## 覆盖范围

只掩盖**完整的**、能确认形态的号码。像"共 320 人"里的 320 不会被碰，
"2026 年"也不会。宁可漏一点，也不能把正常数据改乱——改乱了模型反而判断错。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 只在做类型检查时导入 —— 运行时不导入，避免底层模块反向依赖上层
    from .config import PrivacySettings

# 18 位身份证：6 位地址 + 8 位生日 + 3 位顺序 + 1 位校验（可为 X）
# 15 位是旧版，仍有人用
_ID_CARD_18 = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")
_ID_CARD_15 = re.compile(r"(?<!\d)(\d{6})(\d{6})(\d{3})(?!\d)")

# 手机号：1 开头，第二位 3-9。前面不能是数字（避免从长串里截出来）
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")

# 银行卡：16~19 位，允许中间有空格或短横
_BANK_CARD = re.compile(r"(?<!\d)(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})[\s-]?(\d{4,7})(?!\d)")

# 邮箱
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_id_card(text: str) -> str:
    """身份证：保留前 4 位和后 4 位。``330102199001011234`` → ``3301**********1234``"""

    def repl_18(m: re.Match[str]) -> str:
        return f"{m.group(1)[:4]}{'*' * 10}{m.group(3)}"

    def repl_15(m: re.Match[str]) -> str:
        return f"{m.group(1)[:4]}{'*' * 8}{m.group(3)}"

    text = _ID_CARD_18.sub(repl_18, text)
    return _ID_CARD_15.sub(repl_15, text)


def mask_phone(text: str) -> str:
    """手机号：保留前 3 位和后 4 位。``13812345678`` → ``138****5678``"""
    return _PHONE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", text)


def mask_bank_card(text: str) -> str:
    """银行卡：只保留后 4 位，长度不变。``6222021234567890`` → ``************7890``"""
    return _BANK_CARD.sub(
        lambda m: "*" * (len(m.group(1)) + len(m.group(2)) + len(m.group(3))) + m.group(4),
        text,
    )


def mask_email(text: str) -> str:
    """邮箱：首字母 + 域名。``zhangsan@example.com`` → ``z***@example.com``"""
    return _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)


#: 执行顺序有意义：银行卡放在身份证后面，
#: 否则 18 位身份证会被银行卡规则先吃掉，掩码形态就不好认了。
_MASKERS = (
    ("mask_id_card", mask_id_card),
    ("mask_phone", mask_phone),
    ("mask_bank_card", mask_bank_card),
    ("mask_email", mask_email),
)


def enabled_masks(privacy: "PrivacySettings") -> set[str]:
    """把 ``settings.toml`` 里那四个开关，翻译成"要启用哪几条规则"的名字集合。

    **这个函数存在的唯一理由，是别再让第四个地方手抄一遍这四个开关。**

    以前 ``pipeline.preview_masked`` 手抄了一份，``excel/mapper.py`` 干脆
    忘了传——后果是 settings.toml 里那四个开关**实际上是失效的**：
    界面上显示"已打码"，实际按全开处理。这种"开关点了没用"的不一致
    最难查，因为两边看起来都对。

    谁要脱敏，都从这里拿集合。

    :param privacy: ``config.PrivacySettings``。用字符串标注类型而不是直接
        写类名，是为了让这个模块**不依赖 config**——它在最底层，
        不该反过来知道上层的东西。
    """
    return {
        name
        for name, on in (
            ("mask_id_card", privacy.mask_id_card),
            ("mask_phone", privacy.mask_phone),
            ("mask_bank_card", privacy.mask_bank_card),
            ("mask_email", privacy.mask_email),
        )
        if on
    }


def mask_text(text: str, enabled: Iterable[str] | None = None) -> str:
    """对一段文本做全部脱敏。

    :param enabled: 要启用的规则名集合；``None`` 表示全部启用。
        **新代码请传 ``enabled_masks(settings.privacy)``**，
        别传 ``None``——传 ``None`` 等于无视用户关掉的开关。
    """
    if not text:
        return text
    active = {name for name, _ in _MASKERS} if enabled is None else set(enabled)
    for name, fn in _MASKERS:
        if name in active:
            text = fn(text)
    return text


def mask_value(value: object, enabled: Iterable[str] | None = None) -> object:
    """对单个单元格值脱敏。非字符串原样返回。"""
    if isinstance(value, str):
        return mask_text(value, enabled)
    return value


def build_masked_samples(
    values: Iterable[object],
    limit: int = 5,
    enabled: Iterable[str] | None = None,
) -> list[str]:
    """从一列里挑几个代表性样本，脱敏后给模型看。

    会跳过空值，并**去重前先脱敏**——避免同一串号码因为打码位置不同而重复出现。

    :param limit: 最多给几个样本。
    """
    if limit <= 0:
        return []
    seen: list[str] = []
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        masked = mask_text(text, enabled)
        if masked not in seen:
            seen.append(masked)
        if len(seen) >= limit:
            break
    return seen
