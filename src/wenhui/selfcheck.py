"""自检：用"故意做乱"的三份样例表跑一遍完整流程，**不联网、不花钱**。

双击「验收」时会调用它。它证明的是"这台电脑上代码是好的"——
Python 装好了、依赖装齐了、读表、清洗、校验、写文件全都能跑通。

**它不验证 AI 判断得准不准**，那要真的连上网、真的花几分钱才能验。
所以自检通过 ≠ 效果一定好；但自检不通过，就一定有问题。

跑完的结果会真的写进「输出」文件夹，可以打开看看长什么样。
"""

from __future__ import annotations

from pathlib import Path

from .config import OUTPUT_DIR, PROJECT_ROOT, get_api_key, load_settings
from .excel.reader import read_workbook
from .pipeline import run
from .store import Store

SAMPLES_DIR = PROJECT_ROOT / "tests" / "samples"


def _rule(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def _make_samples() -> list[Path]:
    """现造样例表，保证每次自检用的都是同一批"脏数据"。"""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    import make_samples  # type: ignore[import-not-found]

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    return [make_samples.make_file1(SAMPLES_DIR), make_samples.make_file2(SAMPLES_DIR), make_samples.make_file3(SAMPLES_DIR)]


def main() -> int:
    _rule("第一步：读表")
    samples = _make_samples()
    settings = load_settings()
    for path in samples:
        try:
            sheets = read_workbook(path, settings.excel)
        except Exception as exc:  # noqa: BLE001 - 自检要如实报告任何异常
            print(f"  ✗ {path.name}：读不了（{exc}）")
            return 1
        sheet = sheets[0]
        columns = "、".join(c.name for c in sheet.columns)
        print(f"  ✓ {path.name}")
        print(f"      表头在第 {sheet.header_rows} 行，共 {sheet.n_rows} 行数据")
        print(f"      列名：{columns}")
        if sheet.title_hint:
            print(f"      从标题里认出单位：{sheet.title_hint}")
        if sheet.dropped_total_rows:
            print(f"      剔掉了 {sheet.dropped_total_rows} 行合计")

    _rule("第二步：完整跑一遍（不调用 AI，零费用）")
    print("  说明：这一步用「名称相似度」代替 AI 判断列的含义。")
    print("  所以看到「待确认」比较多是正常的——真跑的时候 AI 会把这些对上。")
    print()

    result = run(
        files=samples,
        settings=settings,
        store=Store(PROJECT_ROOT / "data" / "selfcheck.db"),
        dry_run=True,
        output_dir=OUTPUT_DIR,
    )

    print(f"  合并后共 {len(result.records)} 行数据")
    print(f"  剔掉合计 {result.dropped_total_rows} 行")
    print(f"  必须处理 {result.n_errors} 条 / 请确认 {result.n_warnings} 条")

    _rule("第三步：问题清单（这就是你每次会拿到的东西）")
    for issue in result.issues[:12]:
        print(f"  [{issue.level_label}] {issue.location}")
        print(f"        {issue.problem}")
    if len(result.issues) > 12:
        print(f"  …… 还有 {len(result.issues) - 12} 条，见问题清单文件")

    _rule("结果")
    print(f"  总表：{result.summary_path}")
    print(f"  问题清单：{result.issues_path}")
    print()
    # 注意措辞：这一步**故意**不调 AI（自检永远不花钱），所以"没密钥"
    # 不是出错，只是说明真跑的时候会差一点。别写成像是做错了什么。
    if get_api_key():
        print("  刚才这一步是故意不联网的——自检永远不花钱。")
        print("  密钥已经设置好了。双击「启动」就能用 AI 真跑一遍。")
    else:
        print("  刚才这一步是故意不联网的——自检永远不花钱。")
        print("  顺便发现：还没设置密钥。不设也能用，只是列名对得没那么准。")
        print("  双击「设置密钥」填一次，之后 AI 就能帮你把列名对上。")
    print()
    print("  ✓ 自检通过：代码是好的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
