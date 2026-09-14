"""问数助手的设置：读得出来、改得动、改错了不炸。

`config/settings.toml` 是**给用户改的**文件。用户改它的时候不会跑测试，
所以这里要盯的是"改完之后程序还能不能起来"：

- 段落整个删掉 → 用默认值，不能报错
- 某一项写错类型（比如把数字写成文字）→ 不能静默当成 0 用下去
- 段落存在但没有某一项 → 用默认值

**为什么"删掉整段"也要测**：用户想关掉这个功能时，最自然的做法是
把 `[agent]` 整段删掉，而不是逐行改。那时候程序必须照常启动。
"""

from __future__ import annotations

import textwrap

import pytest

from wenhui.config import AgentSettings, Settings, load_settings


def _write(tmp_path, body: str):
    p = tmp_path / "settings.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return load_settings(p)


# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------

def test_什么都不配也用默认值(tmp_path):
    settings = _write(tmp_path, "")
    assert settings.agent.enabled is True
    assert settings.agent.max_turns == AgentSettings.max_turns


def test_整段删掉不报错(tmp_path):
    """用户想关功能时最自然的做法是删掉整段。程序必须照常起来。"""
    settings = _write(tmp_path, "[llm]\nmodel = 'x'\n")
    assert settings.agent.enabled is True


def test_关掉功能读得出来(tmp_path):
    settings = _write(tmp_path, "[agent]\nenabled = false\n")
    assert settings.agent.enabled is False


def test_真实配置文件能读出来():
    """盯的是仓库里那份 `config/settings.toml`——它写错了，
    用户双击启动就直接炸，而测试全绿。"""
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert isinstance(settings.agent, AgentSettings)
    assert settings.agent.max_turns >= 1


def test_真实配置文件里姓名默认不打码():
    """这一项默认值反了的话，用户问"张三有多少学生"会得到一堆"张*"，
    但他根本没改过配置，只会觉得程序坏了。"""
    assert load_settings().agent.mask_name_in_answer is False


# --------------------------------------------------------------------------
# 各项单独生效
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("enabled", "false", False),
        ("max_turns", "12", 12),
        ("result_rows_for_ai", "50", 50),
        ("samples_per_column", "0", 0),
        ("mask_name_in_answer", "true", True),
    ],
)
def test_每一项都真的读进去了(tmp_path, key, value, expected):
    """**防止"配置项写了但没接线"。**

    `AgentSettings` 加了字段、`settings.toml` 加了说明、`load_settings`
    忘了加一行 —— 结果是用户在文件里改了、程序完全不理。
    界面上看起来一切正常，最难受的就是这种。
    """
    settings = _write(tmp_path, f"[agent]\n{key} = {value}\n")
    assert getattr(settings.agent, key) is expected


def test_写错类型会当场报错_不静默吞掉(tmp_path):
    """"三"这种填法应当直接炸，而不是被当成 0 用下去。

    这里图的是：启动就报错，用户马上知道是自己刚改的那行有问题；
    静默变成 0 的话，程序能跑，但行为诡异，谁也不知道问题在哪。
    """
    with pytest.raises(ValueError):
        _write(tmp_path, "[agent]\nmax_turns = '三'\n")


def test_段落写成了别的形状也不炸(tmp_path):
    """`agent = "开"` 这种写法。不该把整份配置都带崩。"""
    settings = _write(tmp_path, 'agent = "开"\n[llm]\nmodel = "x"\n')
    assert settings.agent.enabled is True
