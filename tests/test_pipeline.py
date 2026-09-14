"""端到端：把故意做乱的三份表跑成一张总表。

全程 ``dry_run=True``——**不连 AI、不花钱**。所以这里验的是
"代码是好的、脏数据能认出来"，而不是"AI 判断得准不准"。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from wenhui.config import load_settings
from wenhui.pipeline import PipelineError, run
from wenhui.store import Store

from make_samples import make_file1, make_file2, make_file3


def _write(directory: Path, name: str, headers: list, rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    path = directory / name
    wb.save(path)
    return path


def _run(tmp_path: Path, files: list[Path]):
    return run(
        files=files,
        settings=load_settings(),
        store=Store(tmp_path / "cache.db"),
        dry_run=True,
        output_dir=tmp_path / "out",
    )


def _messy_inbox(tmp_path: Path) -> list[Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_file1(inbox)
    make_file2(inbox)
    make_file3(inbox)
    return sorted(inbox.glob("*.xlsx"))


# --------------------------------------------------------------------------
# 三份乱表的整体验收
# --------------------------------------------------------------------------


def test_三份表合并成一张总表(tmp_path):
    result = _run(tmp_path, _messy_inbox(tmp_path))

    assert len(result.records) == 8                 # 2 + 2 + 4，一行不少
    assert result.dropped_total_rows == 1           # 合计行被剔掉了
    assert result.summary_path.exists()
    assert result.issues_path.exists()


def test_漏填的那一格被精确指出来(tmp_path):
    """验收标准写得很具体：必须准确指出文件 3 那一行漏填了姓名。"""
    result = _run(tmp_path, _messy_inbox(tmp_path))

    hits = [
        i for i in result.issues
        if i.column == "姓名" and i.row == 3 and "上报表" in i.file
    ]
    assert hits, "第 3 行漏填姓名这件事没被报出来"
    assert "空的" in hits[0].problem


def test_重复填报被当成必须处理(tmp_path):
    """文件 3 的第 4 行是第 2 行的完整复制。"""
    result = _run(tmp_path, _messy_inbox(tmp_path))

    dupes = [i for i in result.issues if "重复" in i.problem]
    assert dupes
    assert all(i.level == "error" for i in dupes)


def test_同一单位有多行数据不算重复(tmp_path):
    """一个学院本来就该有好几行——报成重复会让用户以为工具坏了。"""
    result = _run(tmp_path, _messy_inbox(tmp_path))

    dupes = [i for i in result.issues if "重复" in i.problem]
    assert len(dupes) == 1, f"只该有 1 条真重复，却报了 {len(dupes)} 条"
    assert "孙七" in dupes[0].problem


# --------------------------------------------------------------------------
# 清洗：改数据可以，但必须让用户看见
# --------------------------------------------------------------------------


def test_数字和日期的写法被规整(tmp_path):
    path = _write(
        tmp_path,
        "脏数据.xlsx",
        ["单位", "姓名", "课程数", "学生数", "填报日期"],
        [["外国语学院", "王　五", "1.2万", "3,500", "2026/9/1"]],
    )
    result = _run(tmp_path, [path])
    values = result.records[0].values

    assert values["姓名"] == "王五"            # 全角空格
    assert values["课程数"] == 12000           # "1.2万"
    assert values["学生数"] == 3500            # 千分位
    assert values["填报日期"] == date(2026, 9, 1)


def test_每一次改写都留下记录(tmp_path):
    """老师可以接受"有问题"，不能接受"悄悄改错了"。"""
    path = _write(
        tmp_path,
        "脏数据.xlsx",
        ["单位", "姓名", "课程数"],
        [["外国语学院", "王五", "1.2万"]],
    )
    result = _run(tmp_path, [path])

    notes = [i for i in result.issues if i.level == "info" and i.column == "课程数"]
    assert notes
    assert "1.2万" in notes[0].problem and "12000" in notes[0].problem


def test_整列有一个格子不像数字就一个都不动(tmp_path):
    """拿不准的宁可原样保留——人在总表里一眼能看出哪里怪。"""
    path = _write(
        tmp_path,
        "混着写.xlsx",
        ["单位", "课程数"],
        [["A学院", "1.2万"], ["B学院", "约二十"]],
    )
    result = _run(tmp_path, [path])
    values = {r.values["单位"]: r.values["课程数"] for r in result.records}

    assert values["A学院"] == "1.2万"       # 没被自作主张改成 12000
    assert values["B学院"] == "约二十"


def test_学号不会被当成日期(tmp_path):
    """20261001 既像日期又像学号——先判数字，学号就不会被改错。"""
    path = _write(
        tmp_path,
        "学号.xlsx",
        ["学号", "姓名"],
        [[20261001, "张三"], [20261002, "李四"]],
    )
    result = _run(tmp_path, [path])

    assert [r.values["学号"] for r in result.records] == [20261001, 20261002]


def test_各种没填的写法都变成空(tmp_path):
    path = _write(
        tmp_path,
        "空值.xlsx",
        ["单位", "姓名", "课程数"],
        [["A学院", "张三", "无"], ["A学院", "李四", "N/A"], ["A学院", "王五", "—"]],
    )
    result = _run(tmp_path, [path])

    assert all(r.values["课程数"] is None for r in result.records)


# --------------------------------------------------------------------------
# 出错时要说人话
# --------------------------------------------------------------------------


def test_收件箱空了要告诉用户怎么办(tmp_path):
    with pytest.raises(PipelineError, match="收件箱"):
        _run(tmp_path, [])


# --------------------------------------------------------------------------
# 缓存：不能因为一次"不花钱试跑"，就把以后问 AI 的机会堵死
# --------------------------------------------------------------------------


def test_不花钱试跑的结果不进缓存(tmp_path):
    """试跑走的是规则匹配，一分钱不花、结果也不算数。

    要是把它存进缓存，用户下次设好密钥、真想用 AI 的时候会直接命中缓存，
    一个字的 AI 判断都拿不到——而且界面上还显示"沿用上次"，看不出哪里不对。
    """
    store = Store(tmp_path / "cache.db")
    run(
        files=_messy_inbox(tmp_path),
        settings=load_settings(),
        store=store,
        dry_run=True,
        output_dir=tmp_path / "out",
    )

    assert store.cache_size() == 0


def test_试跑时该确认的列一列都不能少(tmp_path):
    """这些列本来就要交给人确认。缓存命中时也得照旧提醒，不许静音。"""
    result = _run(tmp_path, _messy_inbox(tmp_path))
    assert len(result.uncertain_columns) > 0
