"""双击启动时，给用户看的那些中文。

## 为什么中文写在这里，而不是写在 .bat 里

这是个踩过坑才定下来的设计，**别往回改**。

原来的做法是：`启动.bat` 里直接 `echo` 中文和方框字符。结果是
cmd.exe 的批处理读取器按**字符数**（而不是字节数）移动文件位置，
UTF-8 的中文一个字符占 3 个字节，读着读着就错位——实测表现为
中间某行整个消失、下一行的尾巴被当成命令去执行：

```
  ┌──────────────────────────────────────────────┐
  '?──────────────┘' is not recognized as an internal or external command
```

试过三种写法都不行：UTF-8 无 BOM（错位）、UTF-8 带 BOM（第一行就废）、
GBK（批处理本身没事了，但 Python 输出的中文又变成乱码——批处理要 GBK、
Python 要 UTF-8，两个要求打架）。

所以最终方案：**`.bat` 里一个非 ASCII 字符都不留**，纯 ASCII 的批处理
在任何代码页下都不会错位；中文统一由这个模块打印（Python 处理 UTF-8 可靠）。
顺带的好处：Windows 和 Mac 的中文不再各写一份，改一处两边都变。

## 每次跑完给用户的交代

用户是技术小白。所以每个命令跑完都要说清楚：**刚才做了什么、
结果说明什么、下一步点哪里**。只丢结果和报错，等于没给。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: 项目根目录（src/wenhui/launcher.py -> 上溯三层）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_BAR = "  " + "─" * 50


def _title(text: str) -> None:
    print()
    print(_BAR)
    print(f"    {text}")
    print(_BAR)
    print()


def _say(text: str = "") -> None:
    print(f"  {text}" if text else "")


def _die(text: str, hint: str = "") -> int:
    """报错并停下。**必须给出下一步动作**，不能只说"错了"。"""
    print()
    print(f"  [出问题了] {text}")
    if hint:
        print()
        for line in hint.splitlines():
            _say(line)
    print()
    return 1


def _ask(prompt: str) -> str:
    """让用户输入。输入失败（比如管道里没有输入）时返回空串，不崩。"""
    try:
        return input(f"  {prompt}")
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


#: 密钥长什么样：sk- 开头，后面一串字母数字。
#:
#: 上下界都要有。**上界不是多余的**：`setx` 对超长的值会**悄悄截断**
#: （1024 字符封顶），剪贴板里若是一大段别的东西，就会被截成一个坏密钥
#: 存进去——现象是"AI 用不了"，但没有任何线索指向密钥。所以宁可严格：
#: DeepSeek 的密钥是 35 位，留到 128 已经非常宽松了。
_KEY_SHAPE = re.compile(r"^sk-[A-Za-z0-9_-]{16,128}$")


def _mask(key: str) -> str:
    """给用户看一眼"是不是这把"，但不把密钥完整打在屏幕上。

    跟 DeepSeek 官网自己的显示方式一致（sk-8281****）。省下的那 28 位
    谁也猜不出来，而屏幕上不留全文，就不会被旁边的人拍走。
    """
    return key[:7] + "*" * 8 if len(key) > 15 else "*" * 8


def _check_key(key: str) -> str:
    """问服务端一句：这把密钥认不认？返回 "ok" / "bad" / "unknown"。

    三种结果，处理方式**故意不一样**：

    - ``bad``：服务端明确说密钥不对（401/403）。**不能存**——存下去
      用户会得到一个"怎么都不对、又毫无线索"的局面。
    - ``ok``：认。存。
    - ``unknown``：网络问题，问不到。**照样存**，只提醒一句。
      不能因为学校网络挡了一下就让人连密钥都设不进去。

    为什么单独写这一个检查，而不复用 ``llm.py`` 里的 ``probe()``：
    ``probe()`` 同时管两件事——密钥对不对、配置里的模型名还在不在。
    两件事混在一起，模型名过期时它会报"自检失败"，我们就会**错怪密钥**，
    让用户反复重填一把本来好好的密钥。这里只回答密钥这一个问题。

    另外这里**不打印服务端返回的原文**——错误信息里可能带着密钥片段。
    """
    from wenhui.config import load_settings

    settings = load_settings().llm
    url = settings.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as resp:
            json.load(resp)
        return "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "bad"
        return "unknown"
    except Exception:  # noqa: BLE001 - 连不上就是问不到，不猜
        return "unknown"


def _read_clipboard() -> str:
    """把剪贴板里的文字读出来。读不到就返回空串。

    为什么要读剪贴板：用户刚从网页上"复制"了密钥，剪贴板里就是它。
    让他在黑窗口里按一下回车，比让他往一个**什么都看不见**的输入框里
     粘贴要可靠得多——看不到任何反应时，小白会以为没粘上，
    然后反复按、反复重来，最后放弃。少一步就少一类卡住的地方。
    """
    cmd = (
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"]
        if sys.platform == "win32"
        else ["pbpaste"]
    )
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 环境所限
        return ""
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _ask_secret(prompt: str) -> str:
    """让用户把密钥给我们。优先从剪贴板拿，拿不到再让他输入。

    **输入时屏幕上一个字都不显示**：老师多半在办公室当着人操作，密钥
    明晃晃打在屏幕上就有被同事、学生拍到的风险。

    **必须先判断 `isatty()`，这不是多余的保险**：Windows 上的 `getpass`
    是直接找控制台读键盘（msvcrt.getwch），**根本不看标准输入**。
    所以一旦输入被重定向（管道、被别的程序调起来），它不会像 Unix 那样
    读到 EOF 就返回，而是**一直干等**——实测卡死到超时。
    双击运行的场景 stdin 就是控制台，走 getpass；其余情况老实回显，
    宁可被看到也不要卡住。
    """
    clipboard = _read_clipboard()
    if _KEY_SHAPE.match(clipboard):
        _say(f"已经从剪贴板读到密钥：{_mask(clipboard)}（共 {len(clipboard)} 位）")
        answer = _ask("用这一把吗？直接回车 = 是，输入 n 再回车 = 不是：").strip().lower()
        if answer not in ("n", "no"):
            return clipboard
        print()

    if not sys.stdin.isatty():
        return _ask(prompt)

    import getpass

    try:
        return getpass.getpass(f"  {prompt}")
    except Exception:  # pragma: no cover - 没有控制台时兜底
        return _ask(prompt)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def cmd_start() -> int:
    _title("文汇 · 高校资料汇总")
    _say("把各下级单位交来的表格，合并成一张干净的总表。")
    print()

    # 密钥提示：没有密钥程序照样能跑（走名称相似度），只是准确率低、标黄多。
    # 所以这里只提醒，不拦着——万一用户就是急着先看一眼效果呢。
    if not os.environ.get("LLM_API_KEY", "").strip():
        _say("提示：还没设置 AI 密钥。程序能跑，但列名对得没那么准。")
        _say("双击「设置密钥」填一次，以后都不用管。")
        print()

    _say("正在启动界面…浏览器会自动打开。")
    print()
    _say("（这个黑窗口不要关——关了界面就没了。")
    _say("  用完之后，关掉这个窗口就等于退出程序。）")
    print()

    code = subprocess.call(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT_ROOT / "src" / "wenhui" / "app.py")]
    )

    print()
    _say("程序已退出。")
    print()
    return code


# --------------------------------------------------------------------------
# 设置密钥
# --------------------------------------------------------------------------


def cmd_setkey() -> int:
    _title("设置 AI 密钥")

    _say("这一步只需要做一次。")
    print()
    _say("密钥是什么？就是让程序能使用 AI 的一串密码，只有你自己有。")
    _say("不要发到微信群里、不要写进任何文件。")
    print()
    _say("还没有密钥？")
    _say("  1. 打开 https://platform.deepseek.com 注册")
    _say("  2. 左边找到「API keys」，点「创建」")
    _say("  3. 把生成的那串字符（sk- 开头）复制下来")
    _say("  4. 充一点点钱就够用很久（一次汇总几分钱）")
    print(_BAR)
    _say("最省事的做法：在网页上点「复制」之后，回到这个窗口")
    _say("直接按回车就行——程序会自己去剪贴板里拿，不用你粘贴。")
    print()
    _say("（密钥不会显示在屏幕上，这是故意藏起来的，防止被旁边的人看到。）")
    print(_BAR)
    print()

    # 拿密钥 -> 先验一遍 -> 验过了才存。
    #
    # **"先存再说"是不行的**：存进去一把坏密钥，用户看到的现象是
    # "AI 用不了"，但没有任何线索指向"密钥不对"——他只会以为是网络或者
    # 表格的问题，然后一遍遍重试、一遍遍怀疑自己。所以宁可在这里多等
    # 三秒钟问服务端一句。
    key = ""
    for attempt in range(3):
        candidate = _ask_secret("请粘贴密钥后按回车：").strip()
        if not candidate:
            return _die("没有输入内容，已取消。")

        if not _KEY_SHAPE.match(candidate):
            _say("这看着不像密钥——密钥都是 sk- 开头的一长串字符。")
            _say("在网页上对着那串字符点一下「复制」，再回到这里按回车。")
            print()
            continue

        _say("正在问服务端这把密钥认不认…")
        verdict = _check_key(candidate)
        if verdict == "ok":
            _say("服务端认了。")
            key = candidate
            break
        if verdict == "bad":
            _say("服务端说这把密钥不认。多半是这两种情况：")
            _say("  · 复制的时候少了几位（要复制完整的一整串）")
            _say("  · 这把密钥已经在网页上被删掉/作废了")
            _say("去网页上重新复制一次，再试。")
            print()
            continue

        # 问不到（网络问题）。**照样让他存**，不能因为网络挡了一下
        # 就让人连密钥都设不进去。只是提醒一句，把话说在前面。
        _say("连不上服务端，没法当场验证（可能是网络被挡了）。")
        _say("先把密钥存下来——如果之后界面提示「AI 现在用不了」，")
        _say("再回来重新设置一次。")
        print()
        key = candidate
        break
    else:
        return _die(
            "试了 3 次都不行，先停一下。",
            "把密钥在网页上重新复制一次（注意要完整的一整串），\n"
            "再双击「设置密钥」重来一遍。",
        )

    env_file = Path.home() / ".wenhui" / "env"
    env_file.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        # setx 写进**当前用户的注册表环境变量**，不是文件。
        # 这是刻意的：写文件的话，万一把项目文件夹发给别人，密钥就跟着出去了。
        result = subprocess.run(
            ["setx", "LLM_API_KEY", key],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return _die(
                "没能写入环境变量。",
                "请把下面这行发给技术同事：\n" + (result.stderr or result.stdout or "").strip(),
            )
        _say("✓ 设置好了。")
        print()
        _say("密钥存在 Windows 的用户环境变量里（注册表），项目文件夹里没有任何密钥。")
    else:
        # Mac：项目目录**之外**的个人目录，权限设成只有自己可读。
        # 放进项目文件夹的话，万一把整个文件夹发给别人，密钥就跟着出去了。
        old_umask = os.umask(0o077)
        try:
            env_file.write_text(f"export LLM_API_KEY={key!r}\n", encoding="utf-8")
        finally:
            os.umask(old_umask)
        env_file.chmod(0o600)
        subprocess.run(["launchctl", "setenv", "LLM_API_KEY", key], check=False)
        _say("✓ 设置好了。")
        print()
        _say(f"密钥存在 {env_file}")
        _say("这个文件在项目文件夹外面，权限只有你自己能读，不会被误发给别人。")

    print()
    _say("现在关掉所有窗口，重新双击「启动」就能用了。")
    print()
    return 0


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


def cmd_selfcheck() -> int:
    _title("自检（不花钱）")
    _say("这一步不联网、不花一分钱，用来确认程序本身是好的。")
    _say("如果这里报错，先说别的都没用——先把这个发给我。")
    print()

    _say("① 正在跑内置测试…")
    print()
    code = subprocess.call(
        [sys.executable, "-m", "pytest", "-q", str(PROJECT_ROOT / "tests")],
        cwd=str(PROJECT_ROOT),
    )
    if code != 0:
        return _die(
            "内置测试没通过。",
            "上面列出的失败项请发给技术同事。",
        )

    print()
    _say("② 正在用样例数据跑一遍完整流程…")
    print()
    code = subprocess.call(
        [sys.executable, "-X", "utf8", "-m", "wenhui.selfcheck"],
        cwd=str(PROJECT_ROOT),
    )
    if code != 0:
        return _die(
            "完整流程没跑通。",
            "上面显示的内容请发给技术同事。",
        )

    print()
    _say("自检全部通过。可以关掉这个窗口，去双击「启动」了。")
    print()
    return 0


# --------------------------------------------------------------------------

_COMMANDS = {
    "start": cmd_start,
    "setkey": cmd_setkey,
    "selfcheck": cmd_selfcheck,
}


def _set_console_title(title: str) -> None:
    """把黑窗口的标题设成中文。

    这件事必须在 Python 里做，不能写在 .bat 里——`title 文汇` 这行本身
    就是中文，又会把 cmd.exe 的读取位置带偏（见本模块开头那段说明）。
    """
    if sys.platform != "win32":  # pragma: no cover - Mac 走下面的 else
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:  # pragma: no cover - 标题设不上不影响使用
        pass


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认不是 UTF-8，不设的话中文会乱码。
    # （.bat 里已经 chcp 65001，这里再兜一层，直接跑 python 时也对。）
    #
    # line_buffering=True 不是可有可无的：这个脚本会**再启动一个子进程**
    # （pytest、自检），子进程直接往同一个句柄写。如果自己的输出攒在缓冲区里，
    # 屏幕上就会出现"子进程的话先说完，我自己的话才补上来"的错位——
    # 曾经实测到"自检（不花钱）"这个标题跑到整个自检结果**后面**去了。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", line_buffering=True)
            except (ValueError, OSError):  # pragma: no cover - 个别终端不支持
                pass

    _set_console_title("文汇 · 高校资料汇总")

    argv = list(sys.argv[1:] if argv is None else argv)
    name = argv[0] if argv else ""
    command = _COMMANDS.get(name)
    if command is None:
        print(f"  [用法] python -m wenhui.launcher {{{'|'.join(_COMMANDS)}}}", file=sys.stderr)
        return 2
    return command()


if __name__ == "__main__":
    raise SystemExit(main())
