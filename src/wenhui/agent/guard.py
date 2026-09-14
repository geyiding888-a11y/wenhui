"""安全闸：让 AI 写出来的查询**只能看，不能改**。

## 为什么要有这个文件

问数助手的工作方式是"你说人话 → AI 写一条查询 → 程序执行"。
AI 写出来的东西**不是你能预先检查的**——它可能写错，也可能被一段
精心构造的问题带偏。所以不能靠"相信它不会乱写"，得靠一道它绕不过去的闸。

## 闸门分四层，各管各的

| 层 | 挡什么 | 靠什么 |
|---|---|---|
| 只读授权器 | 删表、改数、插数、建表、PRAGMA、ATTACH | ``set_authorizer`` 按**动作**拒绝 |
| 单语句 | 一次塞好几条语句（``SELECT ...; DROP ...``） | sqlite3 自己就不允许 |
| 超时 | 无限递归之类的查询把界面焊死 | ``set_progress_handler`` |
| 包一层 LIMIT | 一次把 900 行全拖回来 | 外面套一层 |

**为什么授权器是主力，而不是"检查 SQL 文本里有没有 DELETE"**：
文本检查是可以绕的——加个注释、换个大小写、用 ``/* */`` 断开关键字，
都能骗过去。而授权器是数据库在**真要执行那个动作的那一刻**问一句
"这个动作允许吗"，它看到的是动作本身，不是文本。绕不过去。

所以本模块里的文本检查（``looks_like_select``）**只是给模型的一句友善提示**，
让它自己知道写错了、赶紧改。**它不是安全边界**，别把它当回事。

## 还有一个"不报错但算错"的坑，单独治

SQLite 里单引号和双引号**不是一个意思**：

- ``"单位"`` —— 双引号，指的是**列**
- ``'单位'`` —— 单引号，指的是**一段文字**

AI 写查询时特别爱用单引号。写成 ``SELECT '单位' FROM 汇总数据``，
SQLite **一声不吭**，返回一列全是"单位"两个字的假数据。
**不报错，但答案是错的**——这种错最伤信任，因为你根本看不出来。

``fix_identifier_quotes`` 就是治这个的：把**和已知列名一模一样**的单引号
换成双引号。只换一模一样的，所以 ``WHERE "姓名"='张三'`` 里的 ``'张三'``
不会被误伤。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable

# --------------------------------------------------------------------------
# 第一层：只读授权器
# --------------------------------------------------------------------------

#: 允许的动作白名单。**白名单，不是黑名单**——
#: 黑名单的思路是"把危险的挑出来禁掉"，可数据库的动作有几十种，
#: 漏一个就是一个洞。白名单反过来：没写进来的，一律禁止。
_ALLOWED_ACTIONS = frozenset(
    {
        # 执行 SELECT
        sqlite3.SQLITE_SELECT,
        # 读某一列
        sqlite3.SQLITE_READ,
        # SUM / COUNT / AVG 这类内置函数会走这里
        sqlite3.SQLITE_FUNCTION,
        # 开启/结束事务。只读查询也会用到，不放行的话正常查询都跑不了
        sqlite3.SQLITE_TRANSACTION,
        # WITH RECURSIVE 递归查询。放行它是因为超时闸已经能兜住
        # "递归把自己转死"的情况，而禁掉它会让正常的递归查询也用不了
        sqlite3.SQLITE_RECURSIVE,
    }
)


def install_readonly_guard(conn: sqlite3.Connection) -> None:
    """给连接装一道只读闸。装完之后，这个连接**再也写不了任何东西**。

    实测拦得住：``DROP TABLE`` / ``UPDATE`` / ``INSERT`` / ``CREATE TABLE`` /
    ``PRAGMA`` / ``ATTACH``，全部返回 ``not authorized``。
    """
    # 注意：SQLITE_DENY 会让语句直接失败并抛异常，
    # 而不是"忽略这个动作继续跑"——这正是我们要的。
    # 用 SQLITE_IGNORE 的话会静默跳过，反而可能出现半执行的状态。
    conn.set_authorizer(
        lambda action, _a1, _a2, _db, _trigger: (
            sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY
        )
    )


# --------------------------------------------------------------------------
# 第二层：超时（防"一条烂查询把界面焊死"）
# --------------------------------------------------------------------------

#: 默认给多少秒。900 行的表，正常查询是毫秒级的，
#: 给 5 秒已经很宽裕——真跑到 5 秒，那基本就是写坏了。
DEFAULT_TIMEOUT_SECONDS = 5.0

#: 每执行多少条虚拟机指令回调一次。数值小 = 反应快但开销大，
#: 1000 是"反应够快、开销可忽略"的经验值。
_PROGRESS_INTERVAL = 1000


def arm_timeout(conn: sqlite3.Connection, seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """给**下一次**查询装上超时闸。

    **每次执行查询前都要重新调一次**：截止时间是现算的，
    装一次只能用一次。忘了重装的话，第二次查询就没有超时保护了。

    超时后 sqlite3 抛 ``OperationalError: interrupted``，
    而且**连接仍然可用**（实测过）——不会把整个连接搞坏。
    """
    deadline = time.monotonic() + seconds

    def _handler() -> int:
        # 返回非 0 = "别跑了"，sqlite3 会中断当前语句
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_handler, _PROGRESS_INTERVAL)


def clear_timeout(conn: sqlite3.Connection) -> None:
    """撤掉超时闸。跑完查询后调，免得它在别的地方乱中断。"""
    conn.set_progress_handler(None, 0)


# --------------------------------------------------------------------------
# 第三层半：关掉 SQLite 的"双引号当字符串"怪癖
# --------------------------------------------------------------------------

#: `setconfig` 是 Python 3.12 才有的。本项目要求 `>=3.12,<3.13`，
#: 所以正常情况下一定拿得到；写成常量是为了万一在更老的解释器上跑时，
#: 能降级成"不做这件事"而不是直接崩掉。
_HAS_SETCONFIG = hasattr(sqlite3.Connection, "setconfig")


def disable_dqs(conn: sqlite3.Connection) -> bool:
    """关掉 SQLite 那个"双引号找不到列就当成字符串"的历史怪癖。

    **治的是单引号那个坑的镜像版，而且更隐蔽。**

    SQLite 有个老习惯（叫 DQS，double-quoted string）：双引号里写的名字
    要是找不到对应的列，它**不报错**，而是把那段文字当成一个字符串常量。

    ```
    SELECT "不存在的列" FROM 汇总数据
    →  不存在的列
       不存在的列
       不存在的列      ← 一列假数据，看着完全正常
    ```

    这比"单引号写成列名"那个坑**更危险**，因为我们的提示词里明确
    要求"列名用双引号"——模型照着做，一旦记错一个列名，
    得到的不是报错，而是一列看起来很像回事的假数据，它还会拿这个去答你。

    关掉之后同一条查询会变成一条真正的报错，而且 SQLite 自己的提示
    还挺到位：``no such column: "不存在的列" - should this be a string
    literal in single-quotes?``

    关掉是安全的：正常写法完全不受影响——
    真列名用双引号（``"姓名"``）照常，字符串值用单引号（``'张三'``）照常。

    :returns: 关上了返回 ``True``；解释器太老、没这个开关时返回 ``False``。
    """
    if not _HAS_SETCONFIG:
        return False
    # DML 管 SELECT/INSERT/UPDATE/DELETE 里的双引号；
    # DDL 管 CREATE/ALTER 里的。我们只跑 SELECT，但两个都关掉更省心——
    # 万一哪天有人拿这个连接做别的事，行为是一致的。
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
    return True


# --------------------------------------------------------------------------
# 第三层：单引号修正（治"不报错但算错"）
# --------------------------------------------------------------------------

#: 匹配一段单引号字符串。``''`` 是 SQL 里"字符串内部的单引号"的转义写法，
#: 所以要允许它出现在中间，否则 ``'it''s'`` 会被截成两段。
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")


def fix_identifier_quotes(sql: str, identifiers: Iterable[str]) -> str:
    """把**和已知名字一模一样**的单引号，换成双引号。

    治的是这个坑：``SELECT '单位' FROM 汇总数据`` 不报错，
    但返回的是一列"单位"两个字，**不是那一列的真实数据**。

    :param identifiers: 已知的表名和列名。只有**一字不差**的才会被换，
        所以 ``WHERE "姓名"='张三'`` 里的 ``'张三'`` 不会被误伤
        （"张三"是数据，不是列名）。

    .. note::
       这是**保守**的文本替换，不是万能的。AI 要是把列名写成别的样子
       （比如 ``' 单位 '`` 带空格），这里认不出来。
       真正的兜底是提示词里反复叮嘱"列名用双引号"，外加结果看起来
       不对劲时你自己能发现。
    """
    known = {str(name) for name in identifiers if str(name).strip()}
    if not known:
        return sql

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        if inner in known:
            # 双引号内部的双引号要写两遍转义
            return '"' + inner.replace('"', '""') + '"'
        return match.group(0)

    return _SINGLE_QUOTED.sub(_replace, sql)


# --------------------------------------------------------------------------
# 第四层：包一层 LIMIT（防"一次拖回 900 行"）
# --------------------------------------------------------------------------


#: 认出"已经写了 LIMIT"的粗略判断。**认错了也不要紧**：
#: 漏认（明明有 LIMIT 又包了一层）只是多套一层壳，结果一样；
#: 多认（明明没有却以为有）才会拖回全表，所以这里宁可宽松。
_HAS_LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)


def wrap_limit(sql: str, limit: int) -> str:
    """给没写 LIMIT 的查询外面套一层，限制最多返回多少行。

    ``SELECT * FROM (原查询) LIMIT 200`` —— 实测可行。
    已经写了 LIMIT 的原样返回（它自己心里有数，别去改它的语义）。

    :param limit: 最多几行。``<= 0`` 表示不限制。
    """
    if limit <= 0:
        return sql
    # 结尾的分号要先剥掉，否则套进括号里会变成语法错误
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return sql
    if _HAS_LIMIT.search(stripped):
        return stripped
    return f"SELECT * FROM ({stripped}) LIMIT {int(limit)}"


# --------------------------------------------------------------------------
# 友善提示（不是安全边界）
# --------------------------------------------------------------------------

_LEADING_COMMENTS = re.compile(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)*", re.DOTALL)


def looks_like_select(sql: str) -> bool:
    """粗判这是不是一条查询语句。

    .. warning::
       **这不是安全边界，只是给模型的一句友善提示。**
       目的是让它写错时能收到一句人话（"只能用 SELECT 查数据"），
       自己改过来。真正的边界是 :func:`install_readonly_guard`——
       就算这里被骗过去了，授权器那一关照样拦得住。
    """
    body = _LEADING_COMMENTS.sub("", sql).lstrip()
    # WITH ... SELECT 也是只读查询，允许
    return body[:6].lower() == "select" or body[:4].lower() == "with"
