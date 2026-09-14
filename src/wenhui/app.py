"""文汇 · 网页界面。

这是**唯一需要双击启动的东西**（双击「启动.bat」或「启动.command」）。

## 界面为什么长这样

用这个工具的人是教务老师，不是程序员。所以：

- 首页上先告诉他"收件箱里有几份表、模板放了没有"，而不是先给一堆按钮
- 出错时说人话并且**给出下一步动作**，不抛英文异常
- "待确认"的地方让他改一次就够——改完记进缓存，下次同样的表不再问
- 每一步都在页面上留下痕迹（进度、每份表的结果、改过哪些值）
"""

from __future__ import annotations

import os
from html import escape
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8，不设的话中文日志会乱码。
# 必须在 import streamlit 之前做。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - 个别终端不支持
            pass

import streamlit as st  # noqa: E402

from wenhui import pipeline  # noqa: E402
from wenhui.config import (  # noqa: E402
    INBOX_DIR,
    OUTPUT_DIR,
    TEMPLATE_DIR,
    describe_api_key,
    get_api_key,
    load_settings,
)
from wenhui.excel.mapper import IGNORE_FIELD  # noqa: E402
from wenhui.llm import LLMClient  # noqa: E402
from wenhui.store import Store  # noqa: E402

#: 待确认区域的下拉框选项
_KEEP = "（保持不变）"
_IGNORE_LABEL = "（这一列不用管）"

st.set_page_config(page_title="文汇 · 资料汇总", page_icon="📚", layout="wide")


# --------------------------------------------------------------------------
# 资源
# --------------------------------------------------------------------------


@st.cache_resource
def get_store() -> Store:
    return Store()


@st.cache_data(ttl=10)
def get_settings():
    """配置改了不用重启——10 秒后就自动重新读一遍。"""
    return load_settings()


def open_folder(path: Path) -> None:
    """在系统的文件管理器里打开一个文件夹。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        st.warning(f"打不开文件夹，请手动打开：{path}")


# --------------------------------------------------------------------------
# 侧边栏：状态与安全说明
# --------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def check_ai(model: str, base_url: str) -> tuple[bool, str]:
    """开机自检：密钥能不能用、配置里那个模型名还存不存在。

    **为什么非要在开机时查一次**：模型名字会变（DeepSeek 已经变过两次）。
    如果配置里是个已经不存在的名字，程序不会报错——它会**悄悄降级**成
    按名称相似度匹配。用户看到的是"满屏标黄"，根本想不到是模型名写错了，
    白白折腾半天。所以宁可开机多花半秒，当场说清楚。

    结果缓存 5 分钟，不会每刷新一次页面就查一遍。**但缓存也是一把双刃剑**：
    开机那一下万一赶上网络抖动，红字就会一直挂 5 分钟，用户看着它、
    什么也做不了，只会以为程序坏了。所以侧边栏配了一个「重新检查」按钮
    （见 render_sidebar），点了就把这个缓存清掉重查。
    """
    api_key = get_api_key()
    if not api_key:
        return False, ""
    try:
        return LLMClient(load_settings().llm, api_key).probe()
    except Exception as exc:  # noqa: BLE001 - 自检本身不能把界面搞崩
        return False, f"自检没跑起来：{exc}"


def render_sidebar(settings) -> None:
    with st.sidebar:
        st.subheader("状态")

        api_key = get_api_key()
        if api_key:
            ok, message = check_ai(settings.llm.model, settings.llm.base_url)
            if ok:
                st.success(f"AI 已就绪（密钥 {describe_api_key()}）")
                st.caption(message)
            else:
                st.error("AI 现在用不了")
                st.caption(message)
                st.caption(
                    "**这不影响程序运行**——它会改用名称相似度来对列名，"
                    "不花钱，但标黄会多很多。"
                )
                # 给用户一个"我能做点什么"的出口。不然他只能盯着红字，
                # 而自检结果缓存了 5 分钟——刚才是网络抖了一下的话，
                # 他得干等 5 分钟才有第二次机会，多半会以为程序坏了。
                if st.button("重新检查一次", key="recheck_ai"):
                    check_ai.clear()
                    st.rerun()
                st.caption(
                    "如果一直不通：先看电脑上的**代理软件（Clash / VPN）**"
                    "是不是开着——它常把这类请求拦掉。"
                )
        else:
            st.warning("还没设置密钥")
            st.caption("双击项目里的「设置密钥」填一次，以后都不用管。")

        st.caption(f"当前模型：{settings.llm.model}")

        store = get_store()
        if store.cache_size():
            st.caption(f"已记住 {store.cache_size()} 种表头的对应关系")

        st.divider()

        with st.expander("发给 AI 的内容长什么样", expanded=False):
            st.caption(
                "程序只把**列名 + 几个打了码的样本**发给 AI，用来判断这一列是什么意思。"
                "它不需要、也看不到真实内容。"
            )
            st.caption("**你自己拿到的总表里仍是完整原文**，脱敏只作用于发出去的那一小段。")
            demo = st.text_input(
                "试着输入一个值看看：",
                value="张三 330102199001011234 13812345678",
            )
            if demo:
                st.code(pipeline.preview_masked(demo, settings.privacy), language=None)

        with st.expander("记住的对应关系", expanded=False):
            st.caption(
                "你确认过的对应关系会被记住，下次遇到同样的表头直接照做，"
                "既不再问 AI、也不会被 AI 改掉。"
            )
            if st.button("全部忘掉，重新学", width="stretch"):
                n = get_store().forget_all()
                st.success(f"已忘掉 {n} 条。下次会重新判断。")


# --------------------------------------------------------------------------
# 结果展示
# --------------------------------------------------------------------------


def render_overview(result) -> None:
    cols = st.columns(4)
    cols[0].metric("合并后行数", len(result.records))
    cols[1].metric("必须处理", result.n_errors)
    cols[2].metric("请确认", result.n_warnings)
    cols[3].metric("本次花费", f"¥{result.cost_cny:.4f}" if result.cost_cny else "0")

    if result.dropped_total_rows:
        st.caption(f"已自动剔除 {result.dropped_total_rows} 行「合计」行，避免混进总表。")

    for warning in result.warnings:
        st.info(warning)


def render_outcomes(result) -> None:
    rows = []
    for outcome in result.outcomes:
        rows.append(
            {
                "文件": outcome.path.name,
                "状态": "✅ 读到了" if outcome.ok else "❌ 读不了",
                "数据行数": outcome.rows,
                "列怎么对的": {
                    "llm": "AI 判断",
                    "user": "你确认过的",
                    "cache": "沿用上次",
                    "rule": "名称相似度",
                    "": "—",
                }.get(outcome.mapping_source, outcome.mapping_source),
                "剔掉合计": outcome.dropped_total_rows,
                "说明": outcome.error or outcome.unit_hint,
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


def render_downloads(result) -> None:
    cols = st.columns(2)
    for col, path, label in (
        (cols[0], result.summary_path, "⬇️ 下载总表"),
        (cols[1], result.issues_path, "⬇️ 下载问题清单"),
    ):
        if path is None or not Path(path).exists():
            continue
        col.download_button(
            label,
            data=Path(path).read_bytes(),
            file_name=Path(path).name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    if st.button("打开「输出」文件夹", width="stretch"):
        open_folder(OUTPUT_DIR)


def render_issues(result) -> None:
    if not result.issues:
        st.success("没有发现任何问题。")
        return

    st.subheader(f"要处理的问题（{len(result.issues)} 条）")
    st.caption("按「必须先看的排前面」排序。问题清单里也有同样一份。")

    rows = [
        {
            "轻重": issue.level_label,
            "在哪儿": issue.location,
            "什么问题": issue.problem,
            "建议怎么办": issue.suggestion,
        }
        for issue in result.issues
    ]
    st.dataframe(rows, width="stretch", hide_index=True, height=340)


def _current_target(result, label: str, column: str) -> str | None:
    for sheet_label, mapping in result.mappings:
        if sheet_label == label:
            return mapping.mapping.get(column)
    return None


def render_uncertain(result) -> None:
    """低置信度的对应关系，让用户点一下就能纠正，并记住。"""
    uncertain = result.uncertain_columns
    if not uncertain:
        st.success("所有列都对上了，没有需要你确认的地方。")
        return

    st.subheader(f"待确认的对应关系（{len(uncertain)} 处）")
    st.caption(
        "这几列程序不太确定自己对得对不对。你已经确认过的，下次同样的表头它就直接照做，不再问。"
    )

    options = [_KEEP, *result.fields, _IGNORE_LABEL]

    with st.form("confirm_mapping"):
        choices: dict[tuple[str, str], str] = {}
        for label, column, reason in uncertain:
            current = _current_target(result, label, column)
            index = options.index(current) if current in options else 0
            picked = st.selectbox(
                f"「{column}」  ←  {label}",
                options,
                index=index,
                key=f"pick::{label}::{column}",
                help=reason or None,
            )
            choices[(label, column)] = picked

        submitted = st.form_submit_button("保存我的确认", type="primary")

    if not submitted:
        return

    confirmed: dict[str, dict[str, str | None]] = {}
    for (label, column), picked in choices.items():
        if picked == _KEEP:
            continue
        target = IGNORE_FIELD if picked == _IGNORE_LABEL else picked
        confirmed.setdefault(label, {})[column] = target

    if not confirmed:
        st.info("这次没有改动。")
        return

    n = pipeline.apply_confirmation(get_store(), result, result.signatures, confirmed)
    try:
        with st.spinner("已记住你的确认，正在重新生成总表…"):
            st.session_state["result"] = pipeline.run(
                files=list(dict.fromkeys(o.path for o in result.outcomes)),
                template=pipeline.find_template(), settings=get_settings(), store=get_store(),
                dry_run=st.session_state.get("last_dry_run", True), output_dir=OUTPUT_DIR,
            )
    except Exception:
        st.error("对应关系已保存，但重新生成失败，请回到上方再次点击「开始汇总」。")
        return
    st.session_state["upload_notice"] = f"已记住 {n} 份表的对应关系，并重新生成总表，请下载最新结果。"
    st.rerun()


def render_result(result) -> None:
    st.divider()
    render_overview(result)
    render_downloads(result)

    st.subheader("每份表怎么样了")
    render_outcomes(result)

    render_issues(result)
    st.divider()
    render_uncertain(result)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def render_workspace(settings) -> None:

    inbox_files = pipeline.list_inbox()
    template = pipeline.find_template()

    cols = st.columns(3)
    cols[0].metric("收件箱里的表", len(inbox_files))
    cols[1].metric("标准模板", template.name if template else "没放")
    cols[2].metric("累计花费", f"¥{get_store().total_cost():.4f}")

    if not inbox_files:
        st.info(
            "把各下级单位交来的 Excel 放进「收件箱」文件夹，然后回到这里点开始。\n\n"
            "如果有一份标准格式的表，放进「模板」文件夹，总表就会完全按它来对齐。"
        )
        if st.button("打开「收件箱」文件夹", type="primary"):
            open_folder(INBOX_DIR)
        st.stop()

    with st.expander(f"收件箱里的 {len(inbox_files)} 个文件", expanded=False):
        for path in inbox_files:
            st.caption(f"· {path.name}")
        if st.button("打开「收件箱」文件夹"):
            open_folder(INBOX_DIR)

    if template is None:
        st.caption(
            "提示：「模板」文件夹是空的，程序会自己挑一份列最全的表当基准。"
            "如果你有一份标准表，放进去会对得更准。"
        )

    api_key = get_api_key()
    dry_run = st.checkbox(
        "不花钱试跑（不调用 AI）",
        value=not api_key,
        help="只用名称相似度匹配，零费用、不联网，但列名对得没那么准。想先看看效果就勾上。",
    )

    if st.button("开始汇总", type="primary", width="stretch"):
        st.session_state["last_dry_run"] = dry_run or not api_key
        bar = st.progress(0.0, text="准备中…")

        def on_progress(message: str, pct: float) -> None:
            bar.progress(min(max(pct, 0.0), 1.0), text=message)

        try:
            result = pipeline.run(
                files=inbox_files,
                template=template,
                settings=settings,
                store=get_store(),
                progress=on_progress,
                dry_run=dry_run or not api_key,
                output_dir=OUTPUT_DIR,
            )
        except pipeline.PipelineError as exc:
            bar.empty()
            st.error(str(exc))
            st.stop()
        except Exception as exc:  # noqa: BLE001 - 界面层兜底，不能让白屏
            bar.empty()
            st.error(f"出了点意外：{exc}")
            st.caption("如果是第一次运行，可以先勾上「不花钱试跑」看看流程通不通。")
            st.stop()

        bar.empty()
        st.session_state["result"] = result
        st.success("汇总完成")

    result = st.session_state.get("result")
    if result is not None:
        render_result(result)



def render_library() -> None:
    st.subheader("我的资料库")
    files = pipeline.list_inbox()
    st.caption(f"收件箱共有 {len(files)} 份 Excel；点击文件可查看原始内容。")
    for path in files:
        with st.expander(path.name):
            try:
                from wenhui.excel.reader import read_workbook
                sheets = read_workbook(path, get_settings().excel)
                for sheet in sheets:
                    st.caption(f"识别到 {sheet.n_rows} 行数据")
                    st.write("、".join(c.name for c in sheet.columns))
                st.download_button("下载原文件", path.read_bytes(), file_name=path.name, key=f"source-{path.name}")
            except Exception:
                st.error("这份表暂时读不了，请检查是否加密，或另存为 xlsx 后重试。")


def render_archive() -> None:
    st.subheader("汇总归档")
    st.caption("这里是已经保存到本机的真实汇总结果与问题清单。")
    files = sorted(OUTPUT_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("还没有汇总记录。到智能助手上传表格，开始第一次汇总。")
    for path in files:
        with st.container(border=True):
            st.write(path.name)
            st.download_button("下载文件", path.read_bytes(), file_name=path.name, key=f"archive-{path.name}")


def show_library() -> None:
    """在控件重新创建之前切换导航，避免修改已实例化控件的状态。"""
    st.session_state["section"] = "我的资料库"


def render_resources() -> None:
    files = pipeline.list_inbox()
    with st.container(border=True):
        st.subheader(f"本次参考资料 · {len(files)} 份")
        st.caption("来自本机收件箱，问题可追溯到原表行号。")
        for i, path in enumerate(files[:3], 1):
            st.markdown(f'<div class="source-card"><span class="file-icon">XLS</span><div><b>[{i}] {escape(path.stem)}</b><small>Excel · 本地资料<br>来源：收件箱</small></div></div>', unsafe_allow_html=True)
        if not files:
            st.caption("尚未添加资料")
        st.button("查看全部来源 →", width="stretch", on_click=show_library)
    with st.container(border=True):
        st.subheader("处理范围")
        st.caption("只处理你放入收件箱的 Excel 文件。")
        st.checkbox(f"收件箱资料 · {len(files)} 份", value=True, disabled=True)
        template = pipeline.find_template()
        st.caption(f"标准模板：{template.name if template else '未添加，自动选择列最全的表'}")
        if st.button("打开模板文件夹", width="stretch"):
            open_folder(TEMPLATE_DIR)
    with st.container(border=True):
        st.subheader("上传资料")
        st.caption("让助手整理各学院交来的表格")
        uploaded = st.file_uploader("添加 Excel 资料", type=["xlsx", "xls"], accept_multiple_files=True)
        if uploaded and st.button("保存到收件箱", type="primary", width="stretch"):
            INBOX_DIR.mkdir(parents=True, exist_ok=True)
            for item in uploaded:
                name = Path(item.name.replace("\\", "/")).name
                target = INBOX_DIR / name
                counter = 1
                while target.exists():
                    target = INBOX_DIR / f"{Path(name).stem}_{counter}{Path(name).suffix}"
                    counter += 1
                target.write_bytes(item.getbuffer())
            st.session_state.pop("result", None)
            st.session_state["upload_notice"] = f"已添加 {len(uploaded)} 份资料，可以开始汇总。"
            st.rerun()
        st.caption("支持 XLSX、XLS。PDF、Word 和文献问答尚未接入。")


def main() -> None:
    st.markdown((Path(__file__).with_name("ui.css")).read_text(encoding="utf-8"), unsafe_allow_html=True)
    settings = get_settings()
    with st.sidebar:
        st.markdown('<div class="brand"><span>◈</span><div>文汇<small>CAMPUS KNOWLEDGE</small></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="workspace-label">▣　校园工作空间</div>', unsafe_allow_html=True)
        section = st.radio("工作空间", ["智能助手", "我的资料库", "汇总归档", "设置与状态"], key="section", label_visibility="collapsed")
        st.divider()
        st.caption("◷　最近任务")
        recent = sorted(OUTPUT_DIR.glob("汇总表*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        for path in recent:
            st.markdown(f'<div class="recent">▤　{escape(path.stem)}</div>', unsafe_allow_html=True)
        if not recent:
            st.caption("完成汇总后，记录会显示在这里。")
        st.markdown('<div class="sidebar-footer"><b>校园材料工作台</b><br>好学 · 思考 · 前行<small>Powered by LangChain</small></div>', unsafe_allow_html=True)
    connection_label = "本地资料已就绪 · AI 待检查" if get_api_key() else "本地模式 · AI 未配置"
    st.markdown(f'<div class="topbar">⌂　 ›　 工作空间　 /　 <b>{section}</b><span>●　{connection_label}</span></div>', unsafe_allow_html=True)
    content, resources = st.columns([3.1, 1.15], gap="large")
    with resources:
        render_resources()
    with content:
        st.markdown('<div class="eyebrow">校园智能资料助手</div><h1>让资料成为知识，让工作更进一步</h1><p class="subtitle">汇集各学院表格，自动对齐与检查，生成可追溯的总表。</p>', unsafe_allow_html=True)
        if notice := st.session_state.pop("upload_notice", None):
            st.success(notice)
        if section == "我的资料库":
            render_library()
        elif section == "汇总归档":
            render_archive()
        elif section == "设置与状态":
            st.subheader("设置与状态")
            st.info("左侧可查看 AI 连接状态、重新检查连接和管理列名记忆。")
            render_sidebar(settings)
        else:
            cols = st.columns(4)
            for col, symbol, title, detail in zip(cols, ["▤", "⌘", "◎", "▱"], ["表格汇总", "智能对齐", "问题检查", "资料归档"], ["多份表格一键合并", "识别不同列名含义", "定位漏填与重复项", "下载与追溯结果"]):
                col.markdown(f'<div class="feature"><span>{symbol}</span><b>{title}</b><small>{detail}</small></div>', unsafe_allow_html=True)
            st.markdown('<div class="assistant-note"><span>◈</span><div><b>资料整理，从这里开始</b><p>在右侧添加 Excel，点击「开始汇总」。我会整理表格，并列出需要你确认的地方。</p></div></div>', unsafe_allow_html=True)
            with st.container(border=True):
                st.subheader("任务执行记录")
                st.caption("① 读取资料　 →　 ② 对齐与检查　 →　 ③ 生成总表")
                render_workspace(settings)
            st.caption("AI 用于辅助判断列名，汇总结果请结合原表核对。连接状态可在「设置与状态」查看。")


main()
