"""对话界面。**这个包（agent/）里唯一允许出现 ``st.`` 的文件。**

## 调用位置是有讲究的 —— 改了会出怪事

``render_ask_panel(settings, store)`` **必须在 ``render_workspace`` 之前调用**。

理由：``render_workspace`` 在「收件箱是空的」时候会调 ``st.stop()``
（app.py 里那句），``st.stop()`` 会把**它后面所有的渲染全部掐掉**。
所以对话区要是排在它后面，用户一把收件箱清空，对话框就会
"莫名其妙消失"——不报错、不留痕迹，只是没了。那种 bug 最难查。

排在前面，不管收件箱有没有东西，对话框都在。

## 为什么输入框不用 ``st.chat_input``

``st.chat_input`` 会把自己**钉在浏览器窗口的最底部**。
我们的对话区在页面中间（下面还有"任务执行记录"），
钉到底部的话，输入框和它自己的答案隔着一整个屏幕，很怪。
``st.form`` 一样支持回车提交，还多一个看得见的按钮。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..config import Settings, get_api_key
from ..llm import LLMClient
from ..store import Store
from .actions import ActionSink
from .chat import ActionContext, AskOutcome, ask, new_memory, resume
from .table import QueryTable, build_query_table

#: session_state 里用的键，集中放一处免得手抄错
_KEY_TABLE = "ask_table"
_KEY_TABLE_SOURCE = "ask_table_source"
_KEY_MEMORY = "ask_memory"
_KEY_HISTORY = "ask_history"
_KEY_DRAFT = "ask_draft"

#: 界面上最多展示多少行查询结果。
#: 一次查询最多能捞 5000 行，原样画到页面上会卡；而且没人会盯着看 5000 行。
#: 超出部分只说"共 N 行"——不影响判断，界面也不卡。
_DISPLAY_ROWS = 200

#: 助手说话时的头像。**必须是一个 emoji 或者图片路径**——
#: 界面上那些装饰符号（◈ ▤ ⌘）都不是 emoji，放进去 Streamlit 会直接抛错。
_AVATAR = "📚"

#: 没问过话时给几个例子。**新手最大的问题是"不知道该问什么"**，
#: 给三个现成的，他一眼就知道这功能能干嘛。
_EXAMPLES = (
    "一共有多少人？",
    "指导学生最多的前三个人是谁？",
    "各个单位分别有多少人？",
)

#: 把工具名翻成一句人话，给用户看。
#:
#: **不能直接把工具名甩给用户看**（``rerun_merge`` 他看不懂），
#: 但也不能自己编一句——编的那句一旦和工具真正做的事对不上，
#: 用户就是在给一件他没看懂的事签字。所以这里的句子是按工具的实际行为写的，
#: 而且**要连着说清楚它的后果**（"换掉现在这张总表"）。
_ACTION_LABELS = {
    "rerun_merge": "重新汇总收件箱里所有的表，用新结果换掉现在这张总表",
}

#: 用户点「先别动」时替他说的话。
#:
#: **给个填好的默认值，不要让小白面对一个空输入框**——他要么不知道该写什么，
#: 要么随手写一句，那句话还会被当作"用户的拒绝理由"发回给模型。
#: 这句写明了"我只是问问"，模型才不会以为他是否决了整个方向。
_REJECT_MESSAGE = "先别动，我只是问问，不需要真的重新汇总。"


def _pending() -> list[dict]:
    """还没答复的动作（如果有）。

    **从对话记录的最后一条推出来，不另存一份。** 存两份的话，
    它们早晚会不一致——而"界面上有按钮、系统却以为没有待批动作"
    这种不一致，会让用户点完按钮什么都不发生。
    """
    history = _history()
    if history and history[-1].get("role") == "assistant":
        return history[-1].get("pending") or []
    return []


# --------------------------------------------------------------------------
# 内存表的取用
# --------------------------------------------------------------------------


def _get_table(result: Any, settings: Settings) -> QueryTable:
    """拿到（必要时重建）这次汇总结果对应的查询表。

    **同一个结果问 10 个问题，共用一张表**——每次提问都重建的话，
    900 行数据要重新灌 10 遍，白等。

    判断"要不要重建"用的是 ``is``（是不是同一个对象），不是 ``run_id``。
    因为 ``run_id`` 在"不花钱试跑"时可能是 ``None``，而且两次汇总
    万一撞上同一个编号，就会拿旧表回答新数据——那种错**看不出来**。
"""
    cached = st.session_state.get(_KEY_TABLE)
    if cached is not None and st.session_state.get(_KEY_TABLE_SOURCE) is result:
        return cached

    # 换了新结果，把旧连接的**内存**释放掉。不关的话，每汇总一次
    # 就在内存里留一份 900 行的表，问一天下来能攒出几十份。
    if cached is not None:
        cached.close()

    table = build_query_table(
        result,
        privacy=settings.privacy,
        mask_name=settings.agent.mask_name_in_answer,
    )
    st.session_state[_KEY_TABLE] = table
    st.session_state[_KEY_TABLE_SOURCE] = result
    # 数据变了，之前的对话记忆就过期了——它还记着旧表的行数、
    # 旧表的列，接着聊会答出上一个版本的数据。**必须一起清掉。**
    st.session_state.pop(_KEY_MEMORY, None)
    return table


def _get_memory() -> Any:
    """对话记忆。**跨提问复用同一个**，否则追问"那李四呢"它不知道你在说谁。"""
    if _KEY_MEMORY not in st.session_state:
        st.session_state[_KEY_MEMORY] = new_memory()
    return st.session_state[_KEY_MEMORY]


def _history() -> list[dict]:
    return st.session_state.setdefault(_KEY_HISTORY, [])


def _reset_conversation() -> None:
    """清空对话。**记忆也一起清**——只清屏幕不清记忆的话，
    它还记得你刚才问了什么，用户会觉得"我明明清空了它怎么还记得"。"""
    st.session_state.pop(_KEY_MEMORY, None)
    st.session_state[_KEY_HISTORY] = []


def _forget() -> None:
    """把记忆丢掉，只丢记忆、不动屏幕上已经显示出来的对话。

    **这是"出不来"那个死角的正解。**

    助手停下来等人点头的时候，"它还停在那儿"这件事是记在**记忆**里的。
    要是那件事最后没走成（没密钥、超预算、网络断了），卡片被摘掉了、
    记忆里却还卡着那个中断——接下来每一句提问都会被"上一件事还没答复"
    挡回去，而屏幕上**已经没有按钮可点了**。用户就此卡死，只能重启程序。

    **丢记忆是有代价的**：它会忘掉之前聊过的内容，追问"那李四呢"就接不上了。
    所以只在真的卡住时才丢——屏幕上那些对话还是留着的，他能接着往下问，
    只是助手不记得上文了。这比"永远出不来"强得多。
    """
    st.session_state.pop(_KEY_MEMORY, None)


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------


def render_ask_panel(settings: Settings, store: Store) -> None:
    """画对话区。**必须在 ``render_workspace`` 之前调用**，理由见文件开头。"""
    if not settings.agent.enabled:
        # 用户把 [agent] enabled 设成 false 了，整块消失
        return

    with st.container(border=True):
        st.subheader("问一句，直接得到答案")
        st.caption(
            "比如「乙老师这学期要上多少课」。"
            "答案是用查询从总表里**查出来的**，不是 AI 凭印象写的。"
        )

        result = st.session_state.get("result")
        if result is None:
            # 不画输入框。一个能打字、但永远回答"没数据"的框，
            # 只会让人以为程序坏了。
            st.info("还没有可查的数据。先在下面点「开始汇总」，汇总完就能在这里提问。")
            return

        api_key = get_api_key()
        if not api_key:
            st.warning("还没设置密钥，问答功能用不了。双击项目里的「设置密钥」填一次即可。")
            return

        _render_history(settings, store)
        if _pending():
            # 还有没答复的动作，就**先别让他接着问**。
            #
            # 硬要接着问的话，记忆里还存着上一次没走完的中断，新问题
            # 会跟它搅在一起——最后那个回答到底是在回答哪一句，谁都说不清。
            # 还不如把话说白："先把这个了结掉"。
            st.caption("上面那件事还没答复，先点一下「照做」或者「先别动」，然后我们再接着聊。")
            return

        _render_examples_if_new()
        _render_input_and_actions(settings, store, result, api_key)


def _render_history(settings: Settings, store: Store) -> None:
    """把问过的和答过的都画出来。"""
    for index, entry in enumerate(_history()):
        if entry["role"] == "user":
            with st.chat_message("user"):
                st.markdown(entry["text"])
            continue
        # 头像只能用**一个 emoji** 或者图片。别把界面上的装饰符号（比如 ◈）
        # 放进来——它不是 emoji，Streamlit 会当场抛错，整个页面变红。
        with st.chat_message("assistant", avatar=_AVATAR):
            if entry.get("pending"):
                _render_pending(entry["pending"], index, settings, store)
                continue
            if entry.get("error"):
                st.error(entry["error"])
            else:
                st.markdown(entry["text"])
            if entry.get("uncounted"):
                st.caption("⚠ 这次的用量没能记上账，实际花费可能比下面显示的多一点。")
            _render_ledger(entry.get("runs") or [])


def _render_pending(
    pending: list[dict], index: int, settings: Settings, store: Store
) -> None:
    """画那张"等你点头"的卡片。

    :param index: 这条记录在对话历史里的位置。**按钮的 key 要用它**——
        不带位置的话，万一历史里有两条待批记录，两个按钮的 key 会撞上，
        Streamlit 直接抛 ``DuplicateElementKey``。
    """
    st.markdown("**这件事我得先问过你才敢做。**")
    for item in pending:
        label = _ACTION_LABELS.get(item.get("name", ""), item.get("name", "做一件事"))
        reason = (item.get("args") or {}).get("reason") or "它没有说明原因"
        st.markdown(f"- {label}")
        st.caption(f"　它给的理由：{reason}")

    st.caption(_effect_note(settings, store))

    multi = len(pending) > 1
    approve_label = "好，全都照做" if multi else "好，照做"
    reject_label = "全都先别动" if multi else "先别动"

    cols = st.columns(2)
    if cols[0].button(approve_label, key=f"ask-do-{index}", type="primary", width="stretch"):
        _handle_decision(settings, store, index, approve=True)
    if cols[1].button(reject_label, key=f"ask-stop-{index}", width="stretch"):
        _handle_decision(settings, store, index, approve=False)


def _effect_note(settings: Settings, store: Store) -> str:
    """告诉用户"点了照做会发生什么"。**尤其是会不会花钱。**

    为什么要费劲去数据库里翻上次的花费：用户看到的只有两个按钮，
    如果卡片上不提钱，那个「好，照做」就是在让他**闭着眼睛签字**。
    """
    if st.session_state.get("last_dry_run", True):
        # 沿用"不花钱试跑"：一分钱不花，但结果没那么准
        return (
            "这次是**不花钱试跑**（只用名称相似度对齐列名，不调用 AI）。"
            "结果可能没有上次准，但不会产生任何费用。"
        )
    last = store.last_paid_run()
    if last is None:
        return (
            "⚠ 这次会**真的调用 AI**，会产生费用。"
            "以前没有跑过花钱的汇总，说不准具体多少，一般一次几分钱。"
        )
    n_files, cost = last
    return (
        f"⚠ 这次会**真的调用 AI**，会产生费用。"
        f"上次汇总 {n_files} 个文件花了 ¥{cost:.4f}，这次大概是这个量级。"
    )


def _render_ledger(runs: list[dict]) -> None:
    """展示"AI 到底查了什么"。

    **这是这个功能能被信任的关键。** 用户看到一个数字，第一反应是
    "真的假的"。让他能点开看见那条查询和原始结果，他才能自己判断。
    藏在后面的 AI 是不可信的 AI。
    """
    if not runs:
        return
    with st.expander(f"AI 查了什么（{len(runs)} 次查询）", expanded=False):
        for i, run in enumerate(runs, 1):
            if run.get("error"):
                st.caption(f"第 {i} 次：没跑通——{run['error']}")
                continue
            st.code(run["sql"], language="sql")
            rows = run.get("rows") or []
            n = run.get("n_rows", len(rows))
            if not rows:
                st.caption(f"第 {i} 次：查到 0 行。")
                continue
            st.caption(f"第 {i} 次：查到 {n} 行" + ("（下面只显示前几行）" if n > len(rows) else ""))
            # 交给 st.dataframe 的必须是"列名 → 一列的值"这种字典。
            # 直接给 list[list] 的话，列头会变成 0/1/2 这种数字，
            # 用户根本不知道哪列是什么——那这张表就白显示了。
            st.dataframe(
                {name: [row[j] for row in rows] for j, name in enumerate(run["columns"])},
                hide_index=True,
                width="stretch",
            )


def _render_examples_if_new() -> None:
    """第一次进来给三个例子。点一下就直接问，不用自己打字。"""
    if _history():
        return
    st.caption("不知道怎么问？试试这几个：")
    cols = st.columns(len(_EXAMPLES))
    for col, example in zip(cols, _EXAMPLES):
        if col.button(example, key=f"ask-example-{example}", width="stretch"):
            st.session_state[_KEY_DRAFT] = example
            st.rerun()


def _render_input_and_actions(
    settings: Settings, store: Store, result: Any, api_key: str
) -> None:
    with st.form("ask-form", clear_on_submit=True):
        cols = st.columns([5, 1])
        question = cols[0].text_input(
            "想问什么？",
            key=_KEY_DRAFT,
            placeholder="例如：乙老师这学期要上多少课？",
            label_visibility="collapsed",
        )
        submitted = cols[1].form_submit_button("提问", type="primary", width="stretch")

    left, right = st.columns([3, 1])
    left.caption(
        f"问答累计花费 ¥{store.query_cost():.4f}　·　问过 {store.n_queries()} 次"
        + (f"　·　上限 ¥{settings.agent.cost_limit_cny:.2f}" if settings.agent.cost_limit_cny > 0 else "")
    )
    if _history() and right.button("重新开始对话", width="stretch"):
        _reset_conversation()
        st.rerun()

    if submitted and question.strip():
        _handle_question(settings, store, result, api_key, question.strip())


# --------------------------------------------------------------------------
# 处理一次提问
# --------------------------------------------------------------------------


def _actions(store: Store, sink: ActionSink) -> ActionContext:
    """把"动手"类工具要用的东西打包好。

    **``dry_run`` 这一行是这次改动里最要紧的一行。**
    "重新汇总"会花钱，而用户上一次点「开始汇总」时勾没勾"不花钱试跑"，
    决定了他心里对"重跑一次"的预期。不沿用的话，他上次勾了试跑、
    这次让助手重跑，点一下「好，照做」就**悄悄花了钱**——
    而那张卡片上还写着"不花钱"。
    """
    return ActionContext(
        sink=sink,
        store=store,
        dry_run=st.session_state.get("last_dry_run", True),
    )


def _prepare(settings: Settings, store: Store, api_key: str) -> LLMClient | None:
    """花钱和连网这两道关都过了，才给客户端；过不了就往对话里写一句人话。

    :returns: 可以用的客户端；``None`` 表示这次先别问了（已经写好了原因）。
    """
    limit = settings.agent.cost_limit_cny
    if limit > 0:
        spent = store.query_cost()
        if spent >= limit:
            _history().append({
                "role": "assistant",
                "error": (
                    f"问答累计已经花了 ¥{spent:.4f}，到了你设的上限 ¥{limit:.2f}，先停下了。\n\n"
                    "想继续就把 config/settings.toml 里 [agent] 下面那个 "
                    "cost_limit_cny 调大。"
                ),
            })
            return None
        # 上限是**累计**的，所以这里把额度算成"总共还剩多少"再交给客户端。
        # 直接传 limit 的话，每问一次都重置一次额度，等于没有上限。
        remaining = limit - spent
    else:
        remaining = 0.0

    client = LLMClient(settings.llm, api_key, cost_limit_cny=remaining)

    # 提问前先确认连得上，**不花一分钱**（走的是列出可用模型那个接口）。
    # 这个检查本身就会重试几次，所以"AI 连不上时自动重试"是白送的。
    with st.spinner("正在检查 AI 连接…"):
        ok, message = client.probe()
    if not ok:
        _history().append({
            "role": "assistant",
            "error": (
                f"AI 现在连不上，所以这次没有去查，也就**没有给你任何答案**"
                f"（宁可说不知道，也不能给你一个可能是错的数字）。\n\n{message}"
            ),
        })
        return None
    return client


def _finish(
    store: Store,
    label: str,
    outcome: AskOutcome,
    sink: ActionSink,
    replying: bool = False,
) -> None:
    """一次问答的收尾：存进历史、应用重跑结果、记一笔账，然后重画。

    :param label: 写进账本的那句话（用户问了什么 / 他同意了什么）。
    :param replying: 这一次是**用户在答复**（而不是新提问）吗。
        是的话，出了错就得把记忆丢掉——见下面那段。
    """
    _history().append(_entry_of(outcome))

    # 助手真的重跑了，界面就得换成新结果。**放在这里而不是工具里**——
    # 工具跑在工作线程，碰不了 session_state。
    if sink.produced:
        st.session_state["result"] = sink.new_result
        st.session_state["upload_notice"] = "助手已经重新汇总完了，下面的总表是最新的。"
    elif sink.error:
        # 跑了但失败了。**不能一声不吭**——用户点了「好，照做」，
        # 总得知道那件事到底做没做成。
        _history().append({
            "role": "assistant",
            "error": f"重新汇总没有成功：{sink.error}",
        })

    # ★ 答复出了错 = 那件事没走完 = 记忆里还卡着一个没答复的中断。
    #   不丢掉的话，用户接下来每一句都会被"上一件事还没答复"挡回去，
    #   而屏幕上已经没有按钮可点了——**永久卡死，只能重启**。
    if replying and not outcome.ok:
        _forget()

    # 记一笔账。**问答的花费和汇总的分开存**，不然用户在侧边栏
    # 看到一个笼统的"累计花费"，没法判断聊天这件事贵不贵。
    if outcome.ok:
        try:
            store.record_query(label, outcome.cost_cny, n_turns=outcome.n_recorded)
        except Exception:  # noqa: BLE001 - 记不上账不该让用户看不到答案
            pass

    st.rerun()


def _handle_question(
    settings: Settings, store: Store, result: Any, api_key: str, question: str
) -> None:
    """问一句、记一笔、存进历史，然后重画一次页面。"""
    _history().append({"role": "user", "text": question})

    client = _prepare(settings, store, api_key)
    if client is None:
        st.rerun()
        return

    sink = ActionSink()
    table = _get_table(result, settings)
    with st.spinner("正在查表…"):
        outcome = ask(
            settings, client, table, question, _get_memory(),
            actions=_actions(store, sink),
        )
    _finish(store, question, outcome, sink)


def _handle_decision(
    settings: Settings, store: Store, index: int, approve: bool
) -> None:
    """用户点了「好，照做」或者「先别动」。

    :param index: 那张"等你点头"的卡片在对话历史里的位置。
    """
    entry = _history()[index]
    pending = entry.get("pending") or []

    # 先过"花钱"和"连网"两道关，**过了才把卡片摘掉**。
    # 顺序反过来的话，卡片先没了、事情又没做成，用户看到的就是
    # "我点了按钮，它凭空少了一段"。
    api_key = get_api_key()
    client = _prepare(settings, store, api_key) if api_key else None
    if client is None:
        if not api_key:
            _history().append({
                "role": "assistant",
                "error": "密钥没了（可能被清掉了），这件事没能执行。设置好密钥再试一次。",
            })
        del _history()[index]
        # ★ 同 _finish 里那段：没走成，就得把卡在记忆里的中断一起清掉，
        #   否则用户会卡在"上面那件事还没答复"上，而按钮已经没了。
        _forget()
        st.rerun()
        return

    # 到这儿才摘卡片：不管他点的是同意还是拒绝，这张卡片都已经过期了。
    # 不摘的话它会一直留在那儿，用户会以为还能再点一次。
    del _history()[index]

    # ★ 一个待批动作配一条答复，**数量必须对得上**。框架会校验，
    #   对不上直接抛 ValueError（实测：Number of human decisions (0)
    #   does not match number of hanging tool calls (1)）。
    #   所以这里是"按 pending 的个数生成"，不是写死一条。
    decisions = [
        {"type": "approve"}
        if approve
        else {"type": "reject", "message": _REJECT_MESSAGE}
        for _ in pending
    ]

    sink = ActionSink()
    table = _get_table(st.session_state.get("result"), settings)
    what = "、".join(
        _ACTION_LABELS.get(item.get("name", ""), item.get("name", "")) for item in pending
    )
    label = f"（{'同意' if approve else '先别动'}）{what}"

    with st.spinner("正在照做…" if approve else "正在告诉它先别动…"):
        outcome = resume(
            settings, client, table, _get_memory(), decisions,
            actions=_actions(store, sink),
        )
    _finish(store, label, outcome, sink, replying=True)


def _entry_of(outcome: AskOutcome) -> dict:
    """把一次问答的结果转成能存进 session_state 的普通结构。

    **只留要显示的那几行**：一次查询最多能捞 5000 行，历史攒十条就是五万行，
    全塞进 session_state 纯属浪费内存。
    """
    runs = []
    for run in outcome.ledger.runs:
        rows = [list(row) for row in run.rows[:_DISPLAY_ROWS]]
        runs.append({
            "sql": run.sql,
            "columns": list(run.columns),
            "rows": rows,
            "n_rows": run.n_rows,
            "error": run.error,
        })
    return {
        "role": "assistant",
        "text": outcome.answer,
        "error": outcome.error,
        "uncounted": outcome.usage_uncounted,
        "runs": runs,
        "cost": outcome.cost_cny,
        # 助手停下来了，在等用户点头。界面看到这个就画那张卡片，
        # 而不是显示"AI 没有给出回答"（它明明在等答复）。
        "pending": [
            {"name": p.name, "args": p.args, "description": p.description}
            for p in outcome.pending
        ],
    }


__all__ = ["render_ask_panel"]
