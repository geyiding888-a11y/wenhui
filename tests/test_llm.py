"""模型层的测试。

目前主要盯一件事：**别让电脑上的代理把请求掐了**。

这是真机上踩到的坑，值得写清楚免得以后被"优化"掉：用户的电脑装了
Clash，`NO_PROXY` 里没有排除 API 域名，于是自检的请求被送进代理、
再被掐断（WinError 10054）。实测**不做处理时 6 次里坏 3 次**——
刚好一半，所以现象是"有时能用有时不能用"，特别容易被当成网络抖动。
补上之后 6 次全通。

而且这个坑在开发机上看不见（开发用的 shell 里恰好有那个排除项），
所以必须靠测试钉住，不能靠"我这儿是好的"。
"""

from __future__ import annotations

import os

import pytest

from wenhui.config import LLMSettings
from wenhui.llm import LLMClient, _ensure_direct_connection

#: 用户机器上的真实设置：代理开着，"不走代理"的名单里只有本机
USER_NO_PROXY = "localhost,127.0.0.1,::1"
BASE_URL = "https://api.deepseek.com/v1"
HOST = "api.deepseek.com"


@pytest.fixture
def 用户环境(monkeypatch):
    """把环境变量摆成用户机器上的样子。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", USER_NO_PROXY)
    monkeypatch.setenv("no_proxy", USER_NO_PROXY)
    return monkeypatch


def test_把域名加进不走代理名单(用户环境) -> None:
    added = _ensure_direct_connection(BASE_URL)

    assert added == HOST
    # 大小写两份都要改：有的库只读大写，有的只读小写
    for name in ("NO_PROXY", "no_proxy"):
        entries = os.environ[name].split(",")
        assert HOST in entries, f"{name} 里没加上 {HOST}"
        # 原有的条目一条都不能丢，否则会把本机请求也捅到代理上去
        for keep in ("localhost", "127.0.0.1", "::1"):
            assert keep in entries


def test_重复调用不会把域名写两遍(用户环境) -> None:
    _ensure_direct_connection(BASE_URL)
    _ensure_direct_connection(BASE_URL)
    _ensure_direct_connection(BASE_URL)

    assert os.environ["NO_PROXY"].split(",").count(HOST) == 1


def test_已经写过了就报告没改动(用户环境) -> None:
    """返回值是"这次有没有动过环境变量"，第二次该说没动。"""
    assert _ensure_direct_connection(BASE_URL) == HOST
    assert _ensure_direct_connection(BASE_URL) == ""


def test_名单里写了星号就一个字都不许改(用户环境) -> None:
    """``NO_PROXY=*`` 意思是"全部不走代理"，加域名纯属多余，还可能弄坏它。"""
    用户环境.setenv("NO_PROXY", "*")
    用户环境.setenv("no_proxy", "*")

    assert _ensure_direct_connection(BASE_URL) == ""
    assert os.environ["NO_PROXY"] == "*"
    assert os.environ["no_proxy"] == "*"


def test_地址里带端口也认得出域名(用户环境) -> None:
    assert _ensure_direct_connection("https://api.deepseek.com:8443/v1") == HOST
    assert HOST in os.environ["NO_PROXY"]


def test_地址不合法时不炸也不乱写(用户环境) -> None:
    """配置写错了不能把程序搞崩——这时候更该让它跑下去、由自检去报错。"""
    before = os.environ["NO_PROXY"]
    for bad in ["", "不是网址", "/v1", "https:///v1"]:
        assert _ensure_direct_connection(bad) == ""
    assert os.environ["NO_PROXY"] == before


def test_只动不走代理名单不碰代理本身(用户环境) -> None:
    """**不能把用户的代理设置改了**——那是他自己的软件在用。"""
    _ensure_direct_connection(BASE_URL)

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_客户端建好时就已经绕开代理(用户环境) -> None:
    """时机很重要：必须在**建连接之前**改，建完再改就来不及了。"""
    LLMClient(LLMSettings(), "sk-" + "0" * 32)

    assert HOST in os.environ["NO_PROXY"]


def test_配置里关掉就不动环境变量(用户环境) -> None:
    """有人所在网络必须走代理，那时得能关掉这个行为。"""
    LLMClient(LLMSettings(bypass_proxy=False), "sk-" + "0" * 32)

    assert os.environ["NO_PROXY"] == USER_NO_PROXY
    assert os.environ["no_proxy"] == USER_NO_PROXY


def test_默认就是开着绕代理的() -> None:
    """**默认值必须是 True**。这台机器上的默认设置踩过坑，
    改成 False 会让用户又回到"有一半概率用不了"的状态。"""
    assert LLMSettings().bypass_proxy is True


def test_配置文件里那一项读得进来() -> None:
    """settings.toml 里写了 bypass_proxy，得真的被读进去。"""
    from wenhui.config import load_settings

    assert isinstance(load_settings().llm.bypass_proxy, bool)
