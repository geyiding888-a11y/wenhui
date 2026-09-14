"""给问数助手的工具：AI 能"做"的事，全在这个文件里登记。

## 一个查询有**两个**去向，这是本文件最要紧的设计

同一条查询跑出来的结果，要同时服务两个目标，而这两个目标互相冲突：

```
        查询结果（完整：330102199001011234）
                 │
      ┌──────────┴──────────┐
      ↓                     ↓
  给 AI 看的那份         给界面看的那份
  必须打码               必须是完整原文
  3301**********1234     330102199001011234
  （它只需要知道           （你自己看自己的数据，
   "这列是身份证"）          没有理由打码）
```

工具函数**只能返回一样东西**，所以：返回给模型的那份打码，完整的那份
放进 :class:`QueryLedger`（账本），界面从账本里取。

**这两份绝对不能搞反。** 搞反了有两种后果，都很严重：
- 该打码的发给了模型 → 个人信息外流，而且每次提问都流一次
- 该完整的显示成打码 → 用户看不到自己的真实数据，会觉得程序坏了

## 为什么用"闭包"造工具，而不是模块级的全局工具

工具需要知道"查哪张表"和"账本放哪"。用模块级全局变量的话，
两个浏览器标签页同时用就会串数据——A 的查询结果出现在 B 的界面上。

改成 :func:`build_query_tool` 每次现造一个，工具通过闭包**捕获**自己那份
表和账本，天然隔离。造工具本身不联网、不花钱，随便造。

## 工具里一个 ``st.`` 都不许出现

工具跑在 LangGraph 的**工作线程**里，不是 Streamlit 的主线程。
在那里调 ``st.*`` 会抛 ``missing ScriptRunContext``，而且是那种
"有时候好使有时候不好使"的随机崩溃。工具只碰纯对象，界面的事交给 ui.py。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import AgentSettings
from .guard import (
    arm_timeout,
    clear_timeout,
    fix_identifier_quotes,
    looks_like_select,
    wrap_limit,
)
from .table import TABLE_NAME, QueryTable, mask_rows_for_ai

#: 一次查询最多允许返回多少行。
#:
#: 这个数**不是**给模型看的行数（那是 ``agent.result_rows_for_ai``，默认 20），
#: 而是"往内存里捞多少行"的硬上限。900 行的表随便查都够，
#: 给到 5000 是为了兜住一种极端情况：模型写了个自己连自己的查询，
#: 900 行的表会变成 81 万行，内存当场就爆了。
MAX_ROWS = 5000

#: 查询超时。900 行的表，正常查询是毫秒级，5 秒已经很宽裕。
QUERY_TIMEOUT_SECONDS = 5.0


@dataclass
class QueryRun:
    """跑过的一次查询。界面拿它来展示"AI 到底查了什么、查到了什么"。"""

    sql: str
    #: 列名
    columns: list[str] = field(default_factory=list)
    #: **完整**结果（没打码）。界面用它展示给你自己看。
    rows: list[tuple] = field(default_factory=list)
    #: 因为 :data:`MAX_ROWS` 被截断了吗
    truncated: bool = False
    #: 出错信息。空字符串表示这次跑成功了
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def n_rows(self) -> int:
        return len(self.rows)


@dataclass
class QueryLedger:
    """一次提问期间跑过的所有查询。

    存在的唯一理由是**给出完整结果留一个不经过模型的出口**——
    见模块开头那张图。界面从 :attr:`runs` 里取最后一条成功的查询来展示。

    **不落盘、不外发。** 它只活在这一次提问期间，提问结束就被丢掉。
    """

    runs: list[QueryRun] = field(default_factory=list)

    def add(self, run: QueryRun) -> QueryRun:
        self.runs.append(run)
        return run

    def clear(self) -> None:
        self.runs.clear()

    @property
    def last_ok(self) -> QueryRun | None:
        """最后一条成功的查询。界面优先展示它——失败的查询没什么好看的。"""
        for run in reversed(self.runs):
            if run.ok:
                return run
        return None

    @property
    def n_queries(self) -> int:
        return len(self.runs)


def _format_table(columns: list[str], rows: list[list[str]]) -> str:
    """把结果排成一个对齐的文本表格，给模型看。

    用 ``|`` 分隔而不是 Markdown 表格：模型两种都读得懂，
    但 ``|`` 这种更省 token，而且列多的时候不会被表格语法搞乱。
    """
    if not columns:
        return "（没有列）"
    lines = [" | ".join(columns)]
    lines.extend(" | ".join(row) for row in rows)
    return "\n".join(lines)


def run_query(
    table: QueryTable,
    ledger: QueryLedger,
    sql: str,
    settings: AgentSettings,
) -> str:
    """执行一条查询，返回**给模型看的那段文字**。

    这是工具的核心，单独拆出来是为了能**脱离 LangChain 直接测**——
    工具的外壳（``@tool`` 装饰器、参数校验）是框架的事，
    而"查询怎么跑、结果怎么看"是我们的事，后者才是会出错的地方。

    四道闸在这里依次过一遍，顺序不能反：

    1. :func:`~wenhui.agent.guard.looks_like_select` —— 只给模型一句提示，
       **不是安全边界**
    2. :func:`~wenhui.agent.guard.fix_identifier_quotes` —— 修单引号假结果
    3. :func:`~wenhui.agent.guard.wrap_limit` —— 防止一次捞回太多行
    4. 授权器（建表时就装好了）+ :func:`~wenhui.agent.guard.arm_timeout`

    出错时**返回文字而不是抛异常**：模型看到"你写的 SQL 第 3 列不存在"
    会改一版再来；抛异常的话整个对话就断在那儿了。
    """
    # ---- 1. 粗判是不是查询语句（友善提示，不是安全边界）----
    if not looks_like_select(sql):
        ledger.add(QueryRun(sql=sql, error="不是查询语句"))
        return (
            "这个工具只能查数据，不能改数据。"
            f"请写一条 SELECT 语句来查「{TABLE_NAME}」。"
        )

    # ---- 2. 单引号当列名 → 换成双引号 ----
    # 不做这一步的话，`SELECT '单位' FROM ...` 会**不报错但返回一列假的
    # "单位"两个字**，模型会当成真实数据报给你，谁也看不出来。
    sql = fix_identifier_quotes(sql, table.columns)

    # ---- 3. 套一层行数上限 ----
    sql = wrap_limit(sql, MAX_ROWS)

    # ---- 4. 真正执行 ----
    # 超时闸**每次执行前都要重新装**：截止时间是现算的，装一次只能用一次。
    arm_timeout(table.conn, QUERY_TIMEOUT_SECONDS)
    try:
        cursor = table.conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        raw_rows = cursor.fetchall()
    except sqlite3.Error as exc:
        ledger.add(QueryRun(sql=sql, error=str(exc)))
        return (
            f"这条查询没跑通：{exc}\n"
            "请检查列名有没有写错（列名要用双引号包起来），然后重写一条。"
        )
    finally:
        # 一定要撤掉：留着的话，下一次查询可能刚开跑就被一个
        # 早就过期的截止时间掐断，表现是"时好时坏"
        clear_timeout(table.conn)

    # ---- 5. 完整结果进账本（给界面），打码结果给模型 ----
    truncated = len(raw_rows) >= MAX_ROWS
    ledger.add(
        QueryRun(sql=sql, columns=columns, rows=raw_rows, truncated=truncated)
    )

    masked = mask_rows_for_ai(
        columns, raw_rows, table.privacy, settings.mask_name_in_answer
    )
    total = len(masked)
    shown = masked[: settings.result_rows_for_ai]

    header = f"共 {total} 行"
    if total > len(shown):
        # 超出的部分**只报总数**，不把内容发出去——既省 token，
        # 也少往外发数据。同时明确告诉它该怎么办，否则它会直接把
        # "前 20 行"当成全部，答出"一共 20 人"这种错话。
        header += (
            f"，只给你看前 {len(shown)} 行。"
            "要精确数字（总人数、合计、平均）请改用 COUNT / SUM / AVG 自己算，"
            "不要拿这 20 行去推断总数。"
        )
    if truncated:
        header += f"（结果超过 {MAX_ROWS} 行，已截断）"
    if not columns:
        return f"{header}\n（这条查询没有返回任何列）"
    if not shown:
        return f"{header}\n（一行都没有）"
    return f"{header}\n\n{_format_table(columns, shown)}"


def build_query_tool(
    table: QueryTable,
    ledger: QueryLedger,
    settings: AgentSettings,
) -> Any:
    """造一个"查数据"工具，绑在指定的表和账本上。

    :returns: 一个 LangChain 工具对象，可以直接放进 ``create_agent`` 的工具列表。
    """
    from langchain_core.tools import tool

    # 提示词里写死了表名和"列名要加双引号"。这两句**都不是装饰**：
    # - 不说表名，模型会猜一个（比如 `SELECT * FROM data`），然后报错
    # - 不说双引号，模型很爱用单引号，那会返回假数据且不报错
    doc = f"""查询汇总表里的数据。**任何关于数字、人数、课时、职称的问题，都必须先用这个工具查，绝对不许凭印象猜。**

数据库里只有一张表，表名是 {TABLE_NAME}。列名请用**双引号**包起来，例如：
    SELECT "姓名", "学生数" FROM "{TABLE_NAME}" WHERE "职称" = '教授'

注意：
- 列名用双引号（"姓名"），文字值用单引号（'教授'）。写反了不会报错，但结果是错的。
- 不要用 SELECT * 把整张表捞出来。要总数就写 COUNT(*)，要合计就写 SUM("列名")，
  要分组就写 GROUP BY。这样更准，也更快。
- 一次查不出答案没关系，可以分几步查（先看看有哪些单位，再按单位统计）。
- 查不到就如实说查不到，不要编一个数字出来。
- **排序取"前几名"的时候，一定要小心并列。** 这是最容易答错、而且用户
  完全看不出来的一种错：比如问"学生最多的前 3 位是谁"，如果第 3 名和第 4 名
  数值一样，`LIMIT 3` 会**随便截掉**并列的人，你说出的"前 3 位"就成了
  从 5 个并列的人里随便挑的 3 个，而用户会以为只有这 3 个人。
  正确做法：取前几名之前先查清楚**边界上并列的有几个**
  （例如再查一次 `SELECT COUNT(*) ... WHERE "学生数" >= 第N名的值`），
  然后在回答里如实说："最多的都是 N 人，共有 X 位，其中列出了……"。
  宁可多说一句话，也不能漏掉并列的人。"""

    @tool("query_records", description=doc)
    def query_records(sql: str) -> str:
        return run_query(table, ledger, sql, settings)

    return query_records


__all__ = [
    "MAX_ROWS",
    "QUERY_TIMEOUT_SECONDS",
    "QueryLedger",
    "QueryRun",
    "build_query_tool",
    "run_query",
]
