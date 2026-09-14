"""记账：AI 花的每一分钱都必须进账。

## 这个文件盯的是一个真出过的 bug

问数助手跑了四轮对话，右下角"本次花费"仍然显示 **¥0.00000**。

原因：整个程序里只有一个地方会记账——``llm.py`` 的 ``structured()``。
汇总流水线走那条路，所以账记得好好的。但问数助手走的是
``client.model``（LangChain 的 agent 直接调模型），**绕过了 structured**，
于是它花的钱一分都没被记下来。

后果不是"数字难看"，是**用户会以为这功能不要钱**，然后放心地一直问。
``settings.toml`` 里那个 ``cost_limit_cny``（花费上限）对它也完全不起作用。

所以这里钉三件事：
1. ``record_usage`` 真能把 token 用量记进去
2. 记不上时要能**被发现**（返回 False），不能静默当成"没花钱"
3. 问答的花费和汇总的花费**分开算**，不混成一个说不清的数字
"""

from __future__ import annotations

import pytest

from wenhui.config import LLMSettings
from wenhui.llm import BudgetExceeded, LLMClient, Usage
from wenhui.store import Store

#: 一份典型的用量回执，字段名和 LangChain 的 ``usage_metadata`` 一致
FAKE_USAGE = {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}


class _FakeAIMessage:
    """假装是模型回的一条消息。只带 usage_metadata 这一个属性——
    记账只需要这个，多给就掩盖了"别的字段被误用"的问题。"""

    def __init__(self, usage):
        self.usage_metadata = usage


@pytest.fixture()
def client():
    # 密钥是假的，这个文件里一次网络都不会发
    return LLMClient(LLMSettings(), "sk-not-a-real-key", cost_limit_cny=5.0)


# --------------------------------------------------------------------------
# Usage 本身
# --------------------------------------------------------------------------

def test_能直接从字典记用量():
    """问数助手那边拿到的常常是一份字典，不是一个带属性的对象。"""
    u = Usage()
    u.add_metadata(FAKE_USAGE)
    assert u.calls == 1
    assert u.input_tokens == 1200
    assert u.output_tokens == 300


def test_取不到用量也要把次数记上():
    """**"调了一次但没拿到用量"和"没调过"是两件事。**

    前者意味着费用被低估了，要能被看见；后者不用管。
    两个都记成 0 的话，界面上看不出任何区别。
    """
    u = Usage()
    u.add_metadata(None)
    u.add_metadata({})
    assert u.calls == 2
    assert u.input_tokens == 0


def test_缺字段不会崩():
    """有的服务端只回 total_tokens，不细分输入输出。"""
    u = Usage()
    u.add_metadata({"total_tokens": 999})
    assert u.calls == 1
    assert u.input_tokens == 0 and u.output_tokens == 0


def test_老的add_call仍然好使():
    """现有流水线走的是这条路，不能改坏。"""
    u = Usage()
    u.add_call(_FakeAIMessage(FAKE_USAGE))
    assert u.calls == 1 and u.input_tokens == 1200


def test_add_call遇到没有用量的消息也只记次数():
    u = Usage()
    u.add_call(object())
    assert u.calls == 1 and u.input_tokens == 0


# --------------------------------------------------------------------------
# LLMClient.record_usage
# --------------------------------------------------------------------------

def test_记上了返回True(client):
    assert client.record_usage(_FakeAIMessage(FAKE_USAGE)) is True


def test_字典也能记(client):
    assert client.record_usage(FAKE_USAGE) is True


def test_没用量就返回False_而不是假装记上了(client):
    """**这条是防止那个 bug 换个形式回来。**

    调用方要能区分"记上了一笔"和"这条消息里没有用量"。
    如果两种情况都返回 True，调用方就没法发现"费用被低估了"，
    界面上又会安静地显示一个偏小的数字。
    """
    assert client.record_usage(object()) is False
    assert client.record_usage(None) is False
    assert client.record_usage({}) is False


def test_记完之后花费不再是0(client):
    """**这就是那个 bug 的原始现场。**

    修之前：跑了四轮对话，``cost_cny()`` 返回 0.00000。
    修之后：同样的用量，花费要是个正数。
    """
    assert client.cost_so_far() == 0.0
    for _ in range(4):
        client.record_usage(FAKE_USAGE)
    assert client.cost_so_far() > 0
    assert client.usage.calls == 4


def test_记一次失败不涨花费但能被看见(client):
    client.record_failure()
    assert client.usage.failures == 1
    assert client.cost_so_far() == 0.0


# --------------------------------------------------------------------------
# 花费上限对问数助手也要管用
# --------------------------------------------------------------------------

def test_超上限会抛出来(client):
    """``settings.toml`` 里那个 5 块钱上限，对问答也必须生效。

    问数助手绕过了 ``structured()``，不自己调一次 ``check_budget()`` 的话，
    那个上限对它形同虚设——用户可以一直问下去，问到超出几十块都没人拦。
    """
    client.cost_limit_cny = 0.000001  # 小到一记就超
    client.record_usage(FAKE_USAGE)
    with pytest.raises(BudgetExceeded) as exc:
        client.check_budget()
    assert "上限" in str(exc.value)


def test_没超上限就不打扰(client):
    client.record_usage(FAKE_USAGE)
    client.check_budget()  # 不该抛


def test_上限设为0表示不限制():
    c = LLMClient(LLMSettings(), "sk-x", cost_limit_cny=0.0)
    for _ in range(100):
        c.record_usage(FAKE_USAGE)
    c.check_budget()  # 不该抛


def test_超上限的提示是给人看的(client):
    """报错信息要能让不懂技术的人知道下一步干嘛。"""
    client.cost_limit_cny = 0.000001
    client.record_usage(FAKE_USAGE)
    with pytest.raises(BudgetExceeded) as exc:
        client.check_budget()
    assert "settings.toml" in str(exc.value)


# --------------------------------------------------------------------------
# 问答花费单独存
# --------------------------------------------------------------------------

def test_问答花费能存能取(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.query_cost() == 0.0
    store.record_query("张三有多少学生", 0.00027, n_turns=3)
    assert store.query_cost() == pytest.approx(0.00027)


def test_问答花费和汇总花费分开算(tmp_path):
    """**这条是"为什么要另开一张表"的理由。**

    混在一起的话，用户在侧边栏看到"累计花费 ¥3.2"，
    根本没法判断这是汇总花的还是聊天花的——也就没法判断
    "这个聊天功能贵不贵、要不要少用"。
    """
    store = Store(tmp_path / "t.db")
    run_id = store.start_run(3)
    store.finish_run(run_id, n_records=900, n_issues=2, cost_cny=0.5)

    store.record_query("问一句", 0.01)
    store.record_query("再问一句", 0.02)

    assert store.total_cost() == pytest.approx(0.5)     # 只算汇总
    assert store.query_cost() == pytest.approx(0.03)    # 只算问答
    assert store.n_queries() == 2


def test_问过几次取得回来(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.n_queries() == 0
    store.record_query("a", 0.01)
    assert store.n_queries() == 1


def test_超长的问题会被截断(tmp_path):
    """用户可能往框里粘一大段文字。整段存进库没有意义，
    还把一个本地数据库撑大。"""
    store = Store(tmp_path / "t.db")
    store.record_query("问" * 5000, 0.01)
    assert store.n_queries() == 1


def test_老库自动补上问答花费表(tmp_path):
    """**用户手上已经有一个 data/wenhui.db 了。**

    升级之后不能让他去删文件重来——`_init` 里用的是
    ``CREATE TABLE IF NOT EXISTS``，老库打开时自动补上这张新表。
    """
    import sqlite3

    path = tmp_path / "old.db"
    # 造一个"老版本"的库：只有 runs 和 mapping_cache
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE mapping_cache (signature TEXT PRIMARY KEY, mapping TEXT NOT NULL,"
        " source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " hit_count INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,"
        " finished_at TEXT, n_files INTEGER NOT NULL DEFAULT 0,"
        " n_records INTEGER NOT NULL DEFAULT 0, n_issues INTEGER NOT NULL DEFAULT 0,"
        " cost_cny REAL NOT NULL DEFAULT 0);"
    )
    conn.commit()
    conn.close()

    store = Store(path)                 # 打开老库
    store.record_query("升级后问的第一句", 0.01)
    assert store.n_queries() == 1
