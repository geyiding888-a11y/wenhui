"""双击启动文件（.bat / .command）的"不许退化"测试。

## 为什么值得单独写一个测试文件

这几个文件里藏过一个很难查的坑：`.bat` 里写中文，cmd.exe 读的时候
按**字符数**而不是字节数移动读取位置，于是中文一多就串行——某一行整个
消失，下一行的尾巴被当成命令执行，屏幕上冒出一句
`'?─────┘' is not recognized as an internal or external command`。

这个 bug 的可怕之处在于它**时有时无**：直接双击（输出到屏幕）时不明显，
一旦输出被重定向就必现。靠人眼验收很容易漏过去，所以让测试盯着：

- `.bat` 必须是**纯 ASCII**（一个中文都不许有）
- `.bat` 必须是 **CRLF** 换行（LF 在某些 Windows 上会让整行读不到）
- `.bat` 里 `type` 的那几个说明文件必须真的存在

改坏了这里会立刻红，而不是等哪个老师双击之后看见满屏英文报错。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BAT_FILES = sorted(PROJECT_ROOT.glob("*.bat"))
COMMAND_FILES = sorted(PROJECT_ROOT.glob("*.command"))


def test_至少找到了三个双击文件() -> None:
    """防止 glob 写错、路径变了，导致下面几条测试空跑一场全绿。"""
    assert len(BAT_FILES) == 3, [p.name for p in BAT_FILES]
    assert len(COMMAND_FILES) == 3, [p.name for p in COMMAND_FILES]


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_bat里一个非ASCII字符都不能有(path: Path) -> None:
    """这是那个字节错位 bug 的根因，必须守住。

    **别想着"就加一个中文注释没事"**——错位是按行累积的，
    这一行加了中文，受影响的是**它后面那一行**，现场看起来毫无关联。
    """
    text = path.read_text(encoding="utf-8")
    offenders = [
        (lineno, ch)
        for lineno, line in enumerate(text.splitlines(), 1)
        for ch in line
        if ord(ch) > 127
    ]
    assert not offenders, (
        f"{path.name} 里出现了非 ASCII 字符，会让 cmd.exe 读串行：\n"
        + "\n".join(f"  第 {n} 行: {ch!r} (U+{ord(ch):04X})" for n, ch in offenders[:10])
        + "\n中文请写进 src/wenhui/launcher.py 或 config/*.txt。"
    )


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_bat用CRLF换行(path: Path) -> None:
    """LF 换行的 .bat 在部分 Windows 上会把两行读成一行，同样会坏。"""
    data = path.read_bytes()
    lone_lf = data.count(b"\n") - data.count(b"\r\n")
    assert lone_lf == 0, f"{path.name} 里有 {lone_lf} 个 LF 换行，应该是 CRLF"


def test_bat引用的说明文件真的存在() -> None:
    """`.bat` 靠 `type` 把中文说明打到屏幕上（这样中文就不用进 .bat）。

    文件被改名或删掉的话，用户双击后只会看到一句
    "系统找不到指定的文件"——比原来的 bug 还难懂。
    """
    referenced: set[str] = set()
    for bat in BAT_FILES + COMMAND_FILES:
        text = bat.read_text(encoding="utf-8")
        referenced.update(re.findall(r"config[\\/][A-Za-z0-9._-]+\.txt", text))

    assert referenced, "没有任何 .bat/.command 引用 config 下的说明文件，是不是改法变了？"
    for rel in sorted(referenced):
        path = PROJECT_ROOT / rel.replace("\\", "/")
        assert path.exists(), f"{rel} 被引用了但不存在"


@pytest.mark.parametrize("name", ["no-uv.txt", "sync-failed.txt"])
def test_说明文件是UTF8且内容非空(name: str) -> None:
    """必须是 UTF-8：`type` 是把字节原样倒到屏幕上，配着 chcp 65001 才不乱码。

    带 BOM 的话，BOM 那三个字节也会被当成内容打出来（开头多个怪字符）。
    """
    path = PROJECT_ROOT / "config" / name
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{name} 带了 BOM，屏幕上会多出怪字符"
    text = data.decode("utf-8")  # 解不出来就说明不是 UTF-8
    assert "uv" in text.lower() or "网络" in text, f"{name} 看起来是空的"


def test_launcher三个命令都在() -> None:
    """`.bat` 里写的 `-m wenhui.launcher <名字>` 必须真的有对应的函数。"""
    from wenhui import launcher

    assert set(launcher._COMMANDS) == {"start", "setkey", "selfcheck"}

    for bat in BAT_FILES:
        text = bat.read_text(encoding="utf-8")
        match = re.search(r"wenhui\.launcher\s+(\w+)", text)
        assert match, f"{bat.name} 里没找到 wenhui.launcher 的调用"
        assert match.group(1) in launcher._COMMANDS, (
            f"{bat.name} 调用的是不存在的命令 {match.group(1)!r}"
        )


def test_项目根目录算对了() -> None:
    """launcher 靠上溯三层定位项目根；挪动文件位置而没改这里就会找错。"""
    from wenhui import launcher

    assert launcher.PROJECT_ROOT == PROJECT_ROOT
    assert (launcher.PROJECT_ROOT / "pyproject.toml").exists()


# --------------------------------------------------------------------------
# 密钥处理
# --------------------------------------------------------------------------
#
# 这一组测试的由来：开发过程中我用"假密钥"测过一次设置流程，结果**真的把
# 用户环境变量里的真密钥覆盖掉了**——因为当时"直接回车"就等于"确认用剪贴板
# 里那把"，而剪贴板里正是那把假密钥。事后才发现。
#
# 所以现在有了"先问服务端认不认，认了才存"。下面这几条就是钉住这个行为的：
# 密钥不对时**绝不能写进环境变量**。

_FAKE_KEY = "sk-" + "a" * 32


def test_密钥形状认得出真的也拦得住假的() -> None:
    from wenhui import launcher

    assert launcher._KEY_SHAPE.match(_FAKE_KEY)
    # ⚠️ 下面用的是**编造**的密钥，长度和真实的一样（35 位）。
    # 这里**绝不能填真实密钥**：测试文件会进 git 历史，
    # 写进去就等于永久留在仓库里，删不干净。
    assert launcher._KEY_SHAPE.match("sk-" + "0123456789abcdef" * 2)

    for bad in ["", "hello", "sk-短", "8281fb3a", "sk-" + "a" * 10, "sk-" + "a" * 300]:
        assert not launcher._KEY_SHAPE.match(bad), f"{bad!r} 不该被当成密钥"


def test_打码之后看不到密钥的后半段() -> None:
    """界面上要能让用户确认"是不是这把"，但**不能把密钥打到屏幕上**。"""
    from wenhui import launcher

    masked = launcher._mask(_FAKE_KEY)
    assert masked.startswith("sk-")
    assert _FAKE_KEY[7:] not in masked  # 后半段一个字符都不许露
    assert masked.count("*") >= 8


def _patch_urlopen(monkeypatch, side_effect):
    from wenhui import launcher

    def fake_urlopen(*_args, **_kwargs):
        raise side_effect

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)


def test_服务端说不认时判为bad(monkeypatch) -> None:
    """401 要判成 bad —— 也就是"不许存"，这是那次事故的护栏。"""
    import urllib.error

    from wenhui import launcher

    _patch_urlopen(
        monkeypatch,
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, None),
    )
    assert launcher._check_key(_FAKE_KEY) == "bad"


def test_服务端返回403也算bad(monkeypatch) -> None:
    import urllib.error

    from wenhui import launcher

    _patch_urlopen(
        monkeypatch,
        urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
    )
    assert launcher._check_key(_FAKE_KEY) == "bad"


def test_连不上只能判为unknown而不是bad(monkeypatch) -> None:
    """网络不通 ≠ 密钥不对。

    判成 bad 的话，学校网络一挡，老师就永远设不进密钥；
    判成 unknown 才会"照样存下来，只提醒一句"。
    """
    import urllib.error

    from wenhui import launcher

    _patch_urlopen(monkeypatch, urllib.error.URLError("网络不通"))
    assert launcher._check_key(_FAKE_KEY) == "unknown"


def test_服务端返回500也只能判为unknown(monkeypatch) -> None:
    """服务端自己抽风，不能赖用户的密钥。"""
    import urllib.error

    from wenhui import launcher

    _patch_urlopen(
        monkeypatch,
        urllib.error.HTTPError("u", 500, "Server Error", {}, None),
    )
    assert launcher._check_key(_FAKE_KEY) == "unknown"


def test_检查密钥时不打印密钥原文() -> None:
    """`_check_key` 出错时不能把服务端返回的原文打出来——

    有些服务的报错信息里会把请求头里的密钥片段回显出来，
    那样等于把密钥写进了控制台和日志。
    """
    import inspect

    from wenhui import launcher

    source = inspect.getsource(launcher._check_key)
    for leak in ["print(", "exc.read()", "exc.reason"]:
        assert leak not in source, f"_check_key 里不该出现 {leak!r}"
