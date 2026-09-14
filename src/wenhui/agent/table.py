"""把汇总结果变成一张**内存里**的表，让 AI 写查询去查它。

## 这个文件解决什么

汇总跑完，数据在内存里是 ``list[Record]``——一堆 Python 对象。
AI 不会查 Python 对象，它只会写 SQL。所以中间要有个东西把
"Python 对象的一堆行"变成"一张能写 SQL 查的表"。这就是本模块。

## 三条硬规矩

**一、绝不落盘。** 用 ``sqlite3.connect(":memory:")``——纯内存数据库，
进程一退数据就没了，硬盘上不留任何痕迹。这不是随手选的：
``store.py`` 立过一条不变量"库里只存列名和映射关系，**不存任何表格数据**"。
写个临时文件到磁盘，就等于把学生信息又多抄了一份到硬盘上，
那份抄件没人管、没人删、也不会跟着汇总结果一起过期。**不能这么干。**

**二、必须 ``check_same_thread=False``。** 这不是"多此一举的兼容开关"，
是**不写就必然崩**：LangGraph 在单独的工作线程里跑工具，而 sqlite3 默认
禁止连接跨线程使用，会抛 ``SQLite objects created in a thread can only be
used in that thread``。

这个坑特别坑人，因为它**伪装成"模型不行"**：模型写的 SQL 完全正确，
但每个工具调用都报错，于是模型以为是自己写错了，换个写法再试，
再报错，再试……最后放弃说"我查不出来"。你会以为是 AI 笨，
实际上是连接建错了地方。

**三、必须推断列类型。** 如果一律按文本存，``学生数 > 100`` 会变成
**按文字比大小**——文字比较是逐字符比的，``"9"`` 比 ``"100"`` 大
（因为 ``'9' > '1'``）。**不报错，但结果全错。**

## 用哪一份值

用 ``record.values``（**清洗后**的），不用 ``record.raw``（原始值）。
因为清洗后的值才是"同一个东西的规范写法"：``1.2万`` 已经是 ``12000``，
``2026/9/1`` 已经是 ``2026-09-01``。用原始值去查，``> 10000`` 就查不到
那个写成 ``1.2万`` 的格子——而它在总表里明明显示 12000。
你看到的和它查到的不一致，是最容易吵起来的那种 bug。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import PrivacySettings
from ..excel.validator import Record
from ..excel.writer import AUX_HEADERS
from ..privacy import enabled_masks, mask_text
from .guard import disable_dqs, install_readonly_guard

#: 内存表的表名。给模型看的提示词里会写死这个名字，改的话两边一起改。
TABLE_NAME = "汇总数据"

#: 哪些列名算"姓名列"——只有 :attr:`AgentSettings.mask_name_in_answer`
#: 打开时才用得上。**故意收得很窄**：只认名字里带"姓名"两个字的。
#: 收宽了（比如把"教师"也算上）容易误伤——有的表里"教师"那列填的是
#: "在职/外聘"这种状态，打码就把有用的信息打没了。
_NAME_HINTS = ("姓名", "名字")

#: 两个溯源列在建表时的类型。
#:
#: **"来源行"必须是数字**——写成 TEXT 的话行号会被存成字符串，
#: ``来源行 > 100`` 就变成了按文字比大小（``"9"`` 比 ``"100"`` 大）。
#: 跟主字段那边"数字按文字存"是同一个坑，而且这列**每一行都有值**，
#: 踩中的概率比别的列高得多。
_AUX_SQL_TYPES = {"来源文件": "TEXT", "来源行": "INTEGER"}


@dataclass
class QueryTable:
    """一张建好的内存查询表。同一个汇总结果问 10 个问题，共用这一个。"""

    conn: sqlite3.Connection
    #: 真实字段名（不含"来源文件"/"来源行"），顺序和汇总表一致
    fields: list[str]
    #: 每个字段推断出来的类型："INTEGER" / "REAL" / "TEXT"
    types: dict[str, str]
    #: 表里一共多少行
    n_rows: int
    #: 这份数据属于哪一次汇总。用来判断"要不要重建表"
    run_id: int | None = None
    #: 建表时用的脱敏设置。给模型看的样本要按它打码
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    #: 要不要连姓名一起打码。来自 ``settings.agent.mask_name_in_answer``，
    #: **默认关**——姓名不打码，否则"张三有多少学生"这种问题根本没法答。
    mask_name: bool = False

    @property
    def columns(self) -> list[str]:
        """全部列名，含末尾两个溯源列。"""
        return list(self.fields) + list(AUX_HEADERS)

    def close(self) -> None:
        """关掉连接。内存库一关，数据就没了——这正是我们要的。"""
        try:
            self.conn.close()
        except sqlite3.Error:
            # 已经关了 / 连接已失效。关闭失败不影响任何事，不用吵用户
            pass

    # ------------------------------------------------------------------
    # 给模型看的表结构说明
    # ------------------------------------------------------------------

    def schema_text(self, samples_per_column: int = 3) -> str:
        """生成一段"这表长什么样"的说明，塞进提示词给模型看。

        包含：表名、每列的名字和类型、每列两三个**脱敏后**的样本、总行数。

        **样本必须脱敏**——这一步没有任何例外。模型只需要看出
        "这列是数字还是文字、大概长什么样"，不需要知道张三的身份证号。
        表结构说明是**每次提问都会发出去**的东西，比查询结果发得还频繁，
        这里漏一个号码，泄露就是持续性的。

        :param samples_per_column: 每列给几个样本。给 0 就不给样本
            （只看列名它也能写查询），给两三个能帮它分清列的性质。
        """
        lines = [
            f"表名：{TABLE_NAME}",
            f"总行数：{self.n_rows}",
            "",
            "列（写查询时列名要用双引号包起来）：",
        ]
        for name in self.columns:
            kind = self._display_kind(name)
            note = "（溯源用）" if name in AUX_HEADERS else ""
            samples = self._samples_for(name, samples_per_column)
            if samples:
                shown = "、".join(samples)
                lines.append(f'  "{name}"  {kind}{note}  例：{shown}')
            else:
                lines.append(f'  "{name}"  {kind}{note}')
        return "\n".join(lines)

    def _display_kind(self, name: str) -> str:
        """给模型看的类型名。说人话，不说 SQLite 的内部叫法。"""
        if name in AUX_HEADERS:
            return "文字" if name == "来源文件" else "数字"
        return {"INTEGER": "数字", "REAL": "数字"}.get(self.types.get(name, "TEXT"), "文字")

    def _samples_for(self, name: str, limit: int) -> list[str]:
        """取几个样本值，**脱敏后**返回。

        打码在这里做，而且是**唯一的入口**——样本值只从这里出去。
        别的地方想拿样本，都得走这个方法，否则早晚漏一个没打码的。
        """
        if limit <= 0:
            return []
        # 样本直接从表里捞，捞的是已经写进表的规范化值，
        # 和模型查询时看到的值一致
        try:
            rows = self.conn.execute(
                f"SELECT {_quote(name)} FROM {_quote(TABLE_NAME)} "
                f"WHERE {_quote(name)} IS NOT NULL LIMIT 40"
            ).fetchall()
        except sqlite3.Error:
            # 取不到样本不算致命——模型只看列名也能写查询，
            # 不该为这个让整个提问失败
            return []
        return mask_samples(
            [row[0] for row in rows],
            limit=limit,
            privacy=self.privacy,
            is_name_column=self.mask_name and is_name_column(name),
        )


def mask_samples(
    values: list[object],
    limit: int,
    privacy: PrivacySettings,
    is_name_column: bool = False,
) -> list[str]:
    """挑几个代表性样本，**打码后**返回。跳过空值，去重。

    **先打码再去重**——不然同一串号码因为打码位置不同会被当成两个样本，
    白占位置。

    :param is_name_column: 这列是不是姓名列、且用户开了姓名打码。
        是的话额外按"留姓"的规则再打一次。
    """
    if limit <= 0:
        return []
    enabled = enabled_masks(privacy)
    seen: list[str] = []
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        masked = mask_text(text, enabled)
        if is_name_column:
            masked = mask_name(masked)
        if masked not in seen:
            seen.append(masked)
        if len(seen) >= limit:
            break
    return seen


def is_name_column(name: str) -> bool:
    """这个列名像不像"姓名"列。"""
    return any(hint in name for hint in _NAME_HINTS)


def mask_value_for_ai(
    value: object,
    privacy: PrivacySettings,
    is_name_column: bool = False,
) -> str:
    """把一个单元格的值处理成**能发给 AI 的样子**。

    两件事：按 ``[privacy]`` 里的开关打码，姓名列还要额外按"留姓"处理。
    时间/日期这类非文字值转成文字——发给模型的就是一段文本。

    **所有要发出去的值都必须从这里过。** 直接 ``str(value)`` 发出去，
    等于绕开了整套脱敏。
    """
    if value is None:
        return ""
    text = str(value)
    masked = mask_text(text, enabled_masks(privacy))
    return mask_name(masked) if is_name_column else masked


def mask_rows_for_ai(
    columns: list[str],
    rows: list[tuple],
    privacy: PrivacySettings,
    mask_name_in_answer: bool = False,
) -> list[list[str]]:
    """把整块查询结果处理成能发给 AI 的样子。

    :param mask_name_in_answer: 要不要连姓名一起打码。
        **默认不打**——打码了"张三有多少学生"这类问题就永远答不出来。
    """
    name_flags = [
        mask_name_in_answer and is_name_column(col) for col in columns
    ]
    return [
        [
            mask_value_for_ai(cell, privacy, name_flags[i])
            for i, cell in enumerate(row)
        ]
        for row in rows
    ]


def mask_name(text: str) -> str:
    """姓名打码：留姓，其余换成星号。``张三`` → ``张*``，``欧阳修`` → ``欧**``

    **只留一个字**。留两个字的话（``张三`` → ``张*`` 已经是全留了），
    三个字的"欧阳修"要是留成"欧阳*"，配合"哪个学院的"照样能定位到人。
    """
    text = text.strip()
    if len(text) <= 1:
        return text
    return text[0] + "*" * (len(text) - 1)


# --------------------------------------------------------------------------
# 建表
# --------------------------------------------------------------------------


def _infer_types(records: list[Record], fields: list[str]) -> dict[str, str]:
    """看整列的值，判断这列该按数字存还是按文字存。

    **必须看整列，不能看单个格子。** 跟 ``pipeline._classify_columns``
    是同一个道理：单个格子的类型说明不了这一列是什么。

    规则从紧：只要有**一个**非空值不是数字，整列就按文字存。
    宁可把数字当文字存（大不了排序不灵），也不能把文字当数字存
    （``学号`` 存成数字会丢掉开头的 0，那是**改坏了数据**）。
    """
    types: dict[str, str] = {}
    for name in fields:
        values = [r.values.get(name) for r in records]
        filled = [v for v in values if v is not None and str(v).strip() != ""]
        if filled and all(isinstance(v, int) and not isinstance(v, bool) for v in filled):
            types[name] = "INTEGER"
        elif filled and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in filled
        ):
            types[name] = "REAL"
        else:
            types[name] = "TEXT"
    return types


def _to_cell(value: object) -> object:
    """把一个 Python 值转成能塞进 SQLite 的东西。

    两种要转：

    - **日期**：Python 3.12 起，sqlite3 自带的日期适配器已经废弃
      （会报 DeprecationWarning）。显式转成 ISO 字符串，
      好处是顺带能正确比大小——ISO 格式的日期，按文字比就是按时间比。
    - **其它奇怪类型**：转成文字。宁可存成文字，也不能让它抛异常
      把整个提问搞崩。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # 带时分秒的，用 isoformat 保留下来；不带就是纯日期
        return value.isoformat(sep=" ") if (value.hour or value.minute or value.second) else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        # bool 是 int 的子类，不拦的话 True 会变成 1，
        # 查询结果里就会冒出个莫名其妙的 1
        return str(value)
    if isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


def _sql_type(kind: str) -> str:
    """把推断出来的类型翻译成建表用的 SQL 类型。"""
    return {"INTEGER": "INTEGER", "REAL": "REAL"}.get(kind, "TEXT")


def _quote(name: str) -> str:
    """把列名安全地包进双引号里。

    列名里要是本来就有双引号（"姓名"这列叫 ``我的"备注"``），
    按 SQL 规矩要写两遍。不处理的话建表直接语法错误。
    """
    return '"' + str(name).replace('"', '""') + '"'


def build_query_table(
    result: object,
    privacy: PrivacySettings | None = None,
    mask_name: bool = False,
) -> QueryTable:
    """把一次汇总的结果建成一张内存查询表。

    :param result: ``PipelineResult``（``pipeline.py:78``）。
        这里用 ``object`` 而不是写上具体类型，是为了**不 import pipeline**——
        pipeline 会连带 import 一大堆 Excel 相关的东西，
        而这个模块在测试里要能被单独跑起来。
    :param privacy: 脱敏设置。只影响**发给模型的样本**，
        表里的值始终是完整的（界面要展示完整的给你自己看）。
    :param mask_name: 要不要连姓名一起打码（``settings.agent.mask_name_in_answer``）。
    """
    privacy = privacy or PrivacySettings()
    fields = list(getattr(result, "fields", []) or [])
    records = list(getattr(result, "records", []) or [])
    run_id = getattr(result, "run_id", None)

    # check_same_thread=False 是**必须的**，理由见模块开头。别删。
    conn = sqlite3.connect(":memory:", check_same_thread=False)

    if not fields:
        # 一条数据都没有。建一张没有数据列的空表，
        # 让"还没有数据"这件事在查询层面也是一致的（查了返回 0 行），
        # 而不是让上层到处判断"表存不存在"
        install_readonly_guard(conn)
        disable_dqs(conn)
        return QueryTable(
            conn=conn, fields=[], types={}, n_rows=0, run_id=run_id,
            privacy=privacy, mask_name=mask_name,
        )

    types = _infer_types(records, fields)
    all_columns = fields + list(AUX_HEADERS)

    column_sql = ", ".join(f"{_quote(c)} {_sql_type(types.get(c, 'TEXT'))}" for c in fields)
    # 辅助列的类型是**写死**的，不走推断：它们的含义程序自己最清楚。
    # 查不到类型会直接 KeyError —— 这是故意的：哪天 AUX_HEADERS 里加了
    # 第三列而忘了在这里定类型，宁可当场炸掉，也不要写出一张
    # 列数对不上、插数据时才报错的表。
    column_sql += ", " + ", ".join(f"{_quote(c)} {_AUX_SQL_TYPES[c]}" for c in AUX_HEADERS)

    # 这时候还没装只读闸——建表本来就是写操作。
    # 闸是给"AI 写的查询"装的，装在建好表之后。
    conn.execute(f"CREATE TABLE {_quote(TABLE_NAME)} ({column_sql})")

    placeholders = ", ".join("?" for _ in all_columns)
    rows = [
        tuple([_to_cell(r.values.get(name)) for name in fields] + [r.file, r.row])
        for r in records
    ]
    conn.executemany(f"INSERT INTO {_quote(TABLE_NAME)} VALUES ({placeholders})", rows)
    conn.commit()

    # 建完了，**立刻上锁**。
    #
    # 放在这里而不是让调用方自己装：忘了装的表现是"一切正常，
    # 只是模型哪天写出一条 UPDATE 就真改了数据"，而这种遗忘不会有人发现。
    # 装在这一步，就没有"忘"这个可能——表一旦交给别人用，它已经是只读的。
    #
    # 必须在 commit **之后**装：建表和插数据本身就是写操作，
    # 先装闸的话这两步当场就会被自己拦下来。
    install_readonly_guard(conn)

    # 关掉"双引号找不到列就当字符串"的怪癖。
    # 不关的话，模型把列名写错一个字的后果不是报错，而是**一列看起来
    # 很正常的假数据**——它会拿这个去回答你。详见 guard.disable_dqs。
    disable_dqs(conn)

    return QueryTable(
        conn=conn,
        fields=fields,
        types=types,
        n_rows=len(records),
        run_id=run_id,
        privacy=privacy,
        mask_name=mask_name,
    )


__all__ = [
    "TABLE_NAME",
    "QueryTable",
    "build_query_table",
    "is_name_column",
    "mask_name",
    "mask_rows_for_ai",
    "mask_samples",
    "mask_value_for_ai",
]
