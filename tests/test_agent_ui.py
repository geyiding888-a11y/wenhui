"""对话界面：让 Streamlit **自己跑一遍**，而不是靠眼睛看代码。

## 这个文件是怎么来的

界面上线第一次就炸了：``st.chat_message(..., avatar="◈")`` 抛
``Failed to load the provided avatar value as an image``。

原因是那个 ``avatar`` 参数**只认一个 emoji 或者一张图片**，
而 ``◈`` 是界面上用的装饰符号，既不是 emoji 也不是图片。

**真正该反省的不是写错了参数，是自检方式。** 当时的自检是在
"还没问过话"的状态下跑的——``_render_history`` 里画助手气泡那一行
压根没被执行到，所以跑出一片绿，却漏掉了唯一会炸的地方。

所以这里的核心是 ``test_有对话记录时整个界面能画出来``：
它**先往 session_state 里塞几条聊天记录**，再让整个应用跑一遍。
上面那个 bug 放到今天跑，会被这一条当场抓住。

## 为什么用 AppTest 而不是直接调函数

``render_ask_panel`` 依赖 Streamlit 的运行时（session_state、上下文）。
直接调它只会得到"missing ScriptRunContext"，验不到任何东西。
``AppTest`` 是真的把 app.py 当应用跑起来，所以它能验到
"参数对不对""这块会不会抛异常"这类只有真跑才知道的事。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import streamlit as st

from streamlit.testing.v1 import AppTest

from wenhui.agent.ui import _AVATAR, _KEY_HISTORY
from wenhui.excel.validator import Record
from wenhui.pipeline import PipelineResult
from wenhui.store import Store

#: 应用入口。用绝对路径，免得依赖 pytest 从哪个目录启动
APP = Path(__file__).resolve().parents[1] / "src" / "wenhui" / "app.py"


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """把每个测试关进一个自己的临时世界里。

    **做两件事，两件都是必须的。**

    **一、清 Streamlit 的缓存。** ``@st.cache_resource`` 是**进程级**的，
    跨测试、跨会话都在。原有的 ``test_app.py`` 靠 patch
    ``wenhui.store.Store`` 把数据库指到临时目录，而它拿到的 Store
    是从缓存里取的——本文件先跑，缓存里就留下一个**指向真实
    ``data/wenhui.db`` 的 Store**，于是那条测试的断言就挂了。
    这个坑特别阴：**单独跑 test_app.py 是绿的，一起跑才红**。

    **二、数据库和输出目录都指到临时目录。** 不这么干的话，跑一次
    测试就碰一下用户真实的 ``data/wenhui.db``，还在「输出」里留个文件。
    测试不该在用户的数据上留下任何痕迹。

    临时目录用 pytest 的 ``tmp_path``、而不是自己 ``TemporaryDirectory``
    再在 ``_run`` 里退出：**补丁必须活满整个测试**。有的测试会在
    ``_run`` 之后又点一次按钮（比如点例子按钮），那次重画同样要
    ``get_store()``——而缓存里那个 Store 还指着已经删掉的目录，
    会报 ``unable to open database file``。

    **三、给一个假密钥。** 这些测试验的是"界面画得出来吗""卡片上写了什么"，
    跟用户本机有没有配密钥毫无关系。不铺这一层的话，它们会**偷偷依赖
    跑测试的这台机器设了 ``LLM_API_KEY``**——在你机器上是绿的，
    换台机器（或者哪天你把密钥清了）就莫名其妙红一片，
    而报错信息完全指不到真正的原因。
    """
    st.cache_resource.clear()
    st.cache_data.clear()
    with patch(
        "wenhui.store.Store", return_value=Store(tmp_path / "t.db")
    ), patch("wenhui.config.OUTPUT_DIR", tmp_path), patch(
        "wenhui.agent.ui.get_api_key", return_value="sk-测试用的假密钥"
    ):
        yield
    st.cache_resource.clear()
    st.cache_data.clear()


def _result():
    return PipelineResult(
        fields=["单位", "姓名", "学生数"],
        records=[
            Record(
                values={"单位": "计算机学院", "姓名": "张三", "学生数": 120},
                raw={}, file="甲学院.xlsx", sheet="", row=2,
            )
        ],
        run_id=1,
    )


def _pending_entry(reason="用户明确要求重新汇总"):
    """一条"助手停下来等点头"的对话记录，形状和 ``_entry_of`` 造出来的完全一致。"""
    return {
        "role": "assistant", "text": "", "error": "", "uncounted": False,
        "runs": [], "cost": 0.001,
        "pending": [{
            "name": "rerun_merge",
            "args": {"reason": reason},
            "description": "助手想重新汇总一遍收件箱里的表。",
        }],
    }


def _captions(at) -> list[str]:
    """把界面上**所有**说明文字收集起来——气泡里的和气泡外的都要。

    两种都存在，而 ``at.caption``（顶层那个）**只抓得到气泡外面的**：
    卡片上的报价、"它给的理由"都在 ``st.chat_message`` 里面，它一个都看不见。
    反过来，面板级的那句"上面那件事还没答复"又在气泡外面。

    只取一边的话，测试会写成"什么都没找到"——看着像功能坏了，
    其实是找错了地方。所以两边都收。
    """
    return [c.value for c in at.caption] + [
        c.value for msg in at.chat_message for c in msg.caption
    ]


def _run(**session):
    """把应用跑一遍（临时世界由 :func:`_isolate` 备好）。"""
    at = AppTest.from_file(str(APP), default_timeout=300)
    for key, value in session.items():
        at.session_state[key] = value
    at.run()
    return at


# --------------------------------------------------------------------------
# 头像 —— 那次线上事故的正面
# --------------------------------------------------------------------------

def test_头像必须是单个emoji():
    """**这条的价值不在它本身，在于它旁边那句注释。**

    ``st.chat_message`` 的 avatar 只认"一个 emoji"或"一张图片"。
    界面上那些装饰符号（◈ ▤ ⌘ ◎ ▱）一个都不合格——
    传进去不会降级、不会忽略，而是**当场抛异常，整页变红**。
    """
    assert len(_AVATAR) == 1
    assert ord(_AVATAR) > 127          # 不是 ASCII 字母
    assert _AVATAR not in "◈▤⌘◎▱"      # 别再把装饰符号塞进来


# --------------------------------------------------------------------------
# 空状态
# --------------------------------------------------------------------------

def test_没有数据时不画输入框():
    """一个能打字、但永远回答"没数据"的框，只会让用户以为程序坏了。"""
    at = _run()
    assert not at.exception
    assert any("还没有可查的数据" in i.value for i in at.info)
    assert "想问什么？" not in [t.label for t in at.text_input]


def test_有数据时输入框出现():
    at = _run(result=_result())
    assert not at.exception
    assert "想问什么？" in [t.label for t in at.text_input]
    assert "提问" in [b.label for b in at.button]


def test_对话框排在任务记录前面():
    """**顺序是有讲究的**，不是随便摆的。

    ``render_workspace`` 在收件箱为空时会 ``st.stop()``，把它后面
    所有的渲染都掐掉。对话框要是排在它后面，用户把收件箱一清空，
    对话框就会"莫名其妙消失"——不报错、不留痕迹。
    """
    at = _run(result=_result())
    labels = [s.value for s in at.subheader]
    assert labels.index("问一句，直接得到答案") < labels.index("任务执行记录")


# --------------------------------------------------------------------------
# 有对话记录 —— 上次漏掉的那一半
# --------------------------------------------------------------------------

def test_有对话记录时整个界面能画出来():
    """**这个文件里最值钱的一条。**（事故复盘见文件开头）

    塞进去四种助手消息，把 ``_render_history`` 的每条分支都走一遍：
    正常回答、带查询结果的、报错的、没记上账的。
    """
    runs = [{
        "sql": 'SELECT "姓名" FROM "汇总数据"',
        "columns": ["姓名", "学生数"],
        "rows": [["张三", 120], ["李四", 30]],
        "n_rows": 2,
        "error": "",
    }]
    at = _run(result=_result(), **{_KEY_HISTORY: [
        {"role": "user", "text": "指导学生最多的是谁？"},
        {"role": "assistant", "text": "最多的是张三。", "error": "",
         "uncounted": False, "runs": runs, "cost": 0.001, "pending": []},
        {"role": "assistant", "text": "", "error": "AI 现在连不上。",
         "uncounted": False, "runs": [], "cost": 0.0, "pending": []},
        {"role": "assistant", "text": "一共 900 条。", "error": "",
         "uncounted": True, "runs": [], "cost": 0.002, "pending": []},
    ]})

    assert not at.exception
    assert len(at.chat_message) == 4
    # 出错的那些不能画成普通回答
    assert any("连不上" in e.value for e in at.error)
    # "AI 查了什么"要能展开，而且表头得是列名、不是 0/1/2
    assert any("AI 查了什么" in e.label for e in at.expander)
    assert list(at.dataframe[0].value.columns) == ["姓名", "学生数"]
    # 没记上账要提醒。注意两点：
    #   ① 它在气泡**里面**，顶层的 at.caption 抓不到，得进气泡找
    #   ② 原文是"没**能**记上账"——照着界面文案抄，别凭印象写
    assert any(
        "没能记上账" in c.value for msg in at.chat_message for c in msg.caption
    )


def test_查询失败的那一次不会把界面搞崩():
    """一条烂查询在界面上应该显示成一句"没跑通"，而不是一片红。"""
    at = _run(result=_result(), **{_KEY_HISTORY: [
        {"role": "assistant", "text": "", "error": "", "uncounted": False,
         "runs": [{"sql": "SELECT 不存在的列", "columns": [], "rows": [],
                   "n_rows": 0, "error": "no such column"}],
         "cost": 0.0},
    ]})
    assert not at.exception
    assert any("没跑通" in c.value for msg in at.chat_message for c in msg.caption)


# --------------------------------------------------------------------------
# "等你点头"那张卡片
# --------------------------------------------------------------------------

def test_等你点头的卡片能画出来():
    """**"动手先问"在界面上的那一半。**

    塞一条"助手停下来等答复"的记录进去，看整个应用能不能画出来、
    两个按钮在不在、它给的理由有没有原话显示。
    """
    at = _run(result=_result(), **{_KEY_HISTORY: [
        {"role": "user", "text": "帮我重新汇总一下"},
        _pending_entry(),
    ]})

    assert not at.exception
    labels = [b.label for b in at.button]
    assert "好，照做" in labels
    assert "先别动" in labels
    # 它给的理由必须**原话**显示——用户是靠这句话决定点不点的。
    # 换成一句模板话（"助手想执行一个操作"），那个"照做"按钮就变成了摆设。
    assert any("用户明确要求重新汇总" in c for c in _captions(at))


def test_等你点头时不给再问新问题():
    """还有没答复的动作时，把输入框收起来。

    记忆里还存着上一次没走完的中断，这时候再问一句，新问题会跟它搅在一起——
    最后那个回答到底在回答哪一句，谁都说不清。还不如把话说白。
    """
    at = _run(result=_result(), **{_KEY_HISTORY: [_pending_entry()]})
    assert not at.exception
    assert "想问什么？" not in [t.label for t in at.text_input]
    assert any("还没答复" in c for c in _captions(at))


def test_答复完卡片就没了输入框回来():
    """反过来的那一半：卡片摘掉之后，得能接着问话。

    少了这一条，界面可能"卡在等答复"上再也出不来——而用户什么都做不了。
    """
    at = _run(result=_result(), **{_KEY_HISTORY: [
        {"role": "user", "text": "帮我重新汇总一下"},
        # 已经答复过的那条：pending 是空的
        {"role": "assistant", "text": "好的，那就不动了。", "error": "",
         "uncounted": False, "runs": [], "cost": 0.001, "pending": []},
    ]})
    assert not at.exception
    assert "想问什么？" in [t.label for t in at.text_input]
    assert "先别动" not in [b.label for b in at.button]


def test_不花钱试跑时卡片上说清楚不要钱():
    """用户上次勾了"不花钱试跑"，卡片就得说"这次不花钱"。

    **这一条和下面那条是一对，缺一不可。** 只测"要花钱"的话，
    代码可能变成"永远都警告要花钱"，用户被吓多了就再也不点那个按钮了。
    """
    at = _run(result=_result(), last_dry_run=True,
              **{_KEY_HISTORY: [_pending_entry()]})
    assert not at.exception
    assert any("不花钱试跑" in c for c in _captions(at))
    assert not any("会产生费用" in c for c in _captions(at))


def test_要花钱时卡片上会报价(tmp_path):
    """**这一条是"闭着眼睛签字"的正解。**

    用户看到的只有两个按钮。卡片上不报价的话，那个「好，照做」
    就是在让他为一个不知道多少钱的动作签字。
    """
    store = Store(tmp_path / "t.db")     # 和 _isolate 里是同一个库文件
    run_id = store.start_run(3)
    store.finish_run(run_id, 900, 2, 0.0050)

    at = _run(result=_result(), last_dry_run=False,
              **{_KEY_HISTORY: [_pending_entry()]})

    assert not at.exception
    captions = _captions(at)
    assert any("会产生费用" in c for c in captions)
    assert any("0.0050" in c for c in captions), "报了价但没说是多少钱"
    assert any("3 个文件" in c for c in captions), "没说这个价是按几个文件估的"


def test_点了按钮却没走成时不会把用户卡死(tmp_path):
    """**"出不来"那个死角。**

    助手停下来等人点头的时候，"它还停在那儿"这件事记在**记忆**里。
    要是那件事最后没走成（没密钥、超预算、网断了），卡片被摘掉了、
    记忆里却还卡着那个中断——接下来每一句提问都会被"上一件事还没答复"
    挡回去，而屏幕上**已经没有按钮可点了**。用户就此卡死，只能重启程序。

    这里用"超预算"造出这个局面（它不联网，结果是确定的）。
    """
    store = Store(tmp_path / "t.db")
    store.record_query("之前问过的", 9999.0, n_turns=1)   # 先把额度花穿

    at = _run(
        result=_result(),
        last_dry_run=True,
        **{
            "ask_memory": "假装这里有一份记忆",
            _KEY_HISTORY: [_pending_entry()],
        },
    )
    at.button(key="ask-stop-0").click().run()

    assert not at.exception
    # 为什么没走成，得说清楚——不能只是"按钮点了没反应"
    assert any("上限" in e.value for e in at.error)
    # ★ 记忆必须被丢掉，否则用户永远卡在"先答复上面那件事"上
    assert "ask_memory" not in at.session_state, "中断还卡在记忆里，用户出不来了"
    # 卡片摘掉了，输入框回来
    assert "先别动" not in [b.label for b in at.button]


def test_没跑过花钱的汇总时不编一个价格(tmp_path):
    """"不花钱试跑"也会在库里留一行，但花费是 0。

    不过滤的话，卡片上会写"上次花了 ¥0.0000"——**报了一个假数**，
    比不报还糟：用户会以为重跑是免费的。
    """
    store = Store(tmp_path / "t.db")
    run_id = store.start_run(3)
    store.finish_run(run_id, 900, 2, 0.0)      # 试跑，花费 0

    at = _run(result=_result(), last_dry_run=False,
              **{_KEY_HISTORY: [_pending_entry()]})

    assert not at.exception
    captions = _captions(at)
    assert any("会产生费用" in c for c in captions)
    assert any("说不准" in c for c in captions), "没历史就该直说说不准"
    assert not any("0.0000" in c for c in captions), "把试跑那次的 0 元当成了报价"


# --------------------------------------------------------------------------
# 例子按钮
# --------------------------------------------------------------------------

def test_点例子按钮会把问题填进输入框():
    """新手最大的问题是"不知道该问什么"，点一下就替他填好。"""
    at = _run(result=_result())
    examples = [b for b in at.button if b.label.endswith("？")]
    assert examples, "一个例子按钮都没有"
    at.button(key=examples[0].key).click().run()
    assert not at.exception
    assert at.text_input(key="ask_draft").value == examples[0].label
