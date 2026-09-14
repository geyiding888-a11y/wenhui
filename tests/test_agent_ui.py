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
    """
    st.cache_resource.clear()
    st.cache_data.clear()
    with patch(
        "wenhui.store.Store", return_value=Store(tmp_path / "t.db")
    ), patch("wenhui.config.OUTPUT_DIR", tmp_path):
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
         "uncounted": False, "runs": runs, "cost": 0.001},
        {"role": "assistant", "text": "", "error": "AI 现在连不上。",
         "uncounted": False, "runs": [], "cost": 0.0},
        {"role": "assistant", "text": "一共 900 条。", "error": "",
         "uncounted": True, "runs": [], "cost": 0.002},
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
