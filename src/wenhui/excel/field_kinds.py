"""字段名的"概念类别"——判断一个字段名大概是什么性质。

这套词有**两个地方**要用：

- ``validator`` 挑查重键："单位"是分组字段（一个学院好几行，不能单独用来查重），
  "姓名"才可能是身份字段。
- ``mapper`` 的规则兜底做同义匹配："所在学院"和"单位"都是分组字段，可以互相对上。

**必须共用同一份**。两边各写一份的话，早晚会改了一边忘了另一边，
然后出现"规则能匹配上、查重却不认"这种互相矛盾的行为。
"""

from __future__ import annotations

import re

#: "分组"字段：它把记录分成一堆一堆的，**单独用它查重必然误报**。
GROUP_HINTS = ("单位", "部门", "学院", "院系", "科室", "学校", "班级", "年级")

#: "身份"字段：它才可能唯一标识一行。
IDENTITY_HINTS = ("姓名", "学号", "工号", "编号", "序号", "代码", "证件", "身份证")

#: 英文列名里的 id（单独成词才算，"width"、"valid" 不算）
_ID_WORD_RE = re.compile(r"(?<![a-z])id(?![a-z])")


def is_group_field(name: str) -> bool:
    """这个字段名是不是"分组"性质的（单位、学院、班级…）。"""
    return any(hint in name for hint in GROUP_HINTS)


def is_identity_field(name: str) -> bool:
    """这个字段名是不是"身份"性质的（姓名、学号、工号…）。"""
    if any(hint in name for hint in IDENTITY_HINTS):
        return True
    return bool(_ID_WORD_RE.search(name.casefold()))


def same_kind(left: str, right: str) -> bool:
    """两个字段名是不是同一类概念（都像分组，或都像身份）。"""
    if is_group_field(left) and is_group_field(right):
        return True
    # 身份类不做这种"同类即相同"的判断："学号"和"姓名"都是身份，
    # 但它们是两回事，混了会串数据。
    return False
