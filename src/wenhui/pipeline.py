"""主流程：把「读 → 对齐 → 清洗 → 校验 → 输出」串起来。

## 每一步都是独立函数

阶段之间没有隐藏状态，每一步都能单独跑、单独测。
好处是第 7 份文件的表头识别错了，不用重跑前面 6 份（也不用重新付 API 费）。

## 一条贯穿全程的原则：绝不静默改数据

程序会自动清洗——把"1.2万"变成 12000、把"2026/9/1"变成"2026-09-01"。
但这些改动**必须让用户看见**，所以每次语义层面的改动都会生成一条
「仅供参考」的记录，写进问题清单。

老师可以接受"有问题"，不能接受"悄悄改错了"。
这条线是工具能不能被信任的分界。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import (
    INBOX_DIR,
    OUTPUT_DIR,
    TEMPLATE_DIR,
    AggregateSettings,
    ExcelSettings,
    PrivacySettings,
    Settings,
    get_api_key,
)
from .excel.cleaner import clean_text, is_empty, parse_date, parse_number
from .excel.mapper import (
    IGNORE_FIELD,
    UNKNOWN_FIELD,
    MappingResult,
    collect_field_samples,
    map_sheet,
    pick_target_fields,
    read_template_columns,
)
from .excel.reader import ExcelReadError, SheetData, read_workbook
from .excel.validator import Issue, Record, validate
from .excel.writer import default_output_paths, write_issues, write_summary
from .llm import LLMClient
from .privacy import mask_text
from .store import Store, signature_of

#: 进度回调：``(说明文字, 0~1 的进度)``
ProgressFn = Callable[[str, float], None]

#: 支持的文件后缀
SUPPORTED_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")


class PipelineError(RuntimeError):
    """流程走不下去，且原因要能说给不懂技术的人听。"""


@dataclass
class FileOutcome:
    """一个文件的处理结果，用于给用户看"每份表怎么样了"。"""

    path: Path
    ok: bool
    sheets: int = 0
    rows: int = 0
    mapping_source: str = ""      # 'llm' | 'cache' | 'rule' | 'user'
    uncertain: int = 0            # 需要人工确认的列数
    dropped_total_rows: int = 0
    unit_hint: str = ""
    error: str = ""


@dataclass
class PipelineResult:
    """一次汇总的全部产出。"""

    fields: list[str] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    outcomes: list[FileOutcome] = field(default_factory=list)
    mappings: list[tuple[str, MappingResult]] = field(default_factory=list)
    summary_path: Path | None = None
    issues_path: Path | None = None
    usage_line: str = ""
    warnings: list[str] = field(default_factory=list)
    run_id: int | None = None
    cost_cny: float = 0.0
    #: ``{文件标签: 表头指纹}``——用户确认映射时要用它写回缓存
    signatures: dict[str, str] = field(default_factory=dict)

    @property
    def dropped_total_rows(self) -> int:
        return sum(o.dropped_total_rows for o in self.outcomes)

    @property
    def n_errors(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def n_warnings(self) -> int:
        return sum(1 for i in self.issues if i.level == "warning")

    @property
    def n_infos(self) -> int:
        return sum(1 for i in self.issues if i.level == "info")

    @property
    def uncertain_columns(self) -> list[tuple[str, str, str]]:
        """需要人工确认的列：``(文件名, 原始列名, 依据)``。"""
        out: list[tuple[str, str, str]] = []
        for label, result in self.mappings:
            for column in result.uncertain:
                out.append((label, column, result.reasons.get(column, "")))
        return out


def list_inbox(inbox: Path | None = None) -> list[Path]:
    """列出收件箱里所有能处理的表格文件（跳过 Excel 打开时产生的临时文件）。"""
    inbox = inbox or INBOX_DIR
    if not inbox.exists():
        return []
    files = [
        p
        for p in sorted(inbox.iterdir())
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.startswith("~$")          # Excel 打开时的锁文件
        and not p.name.startswith(".")
    ]
    return files


def find_template(template_dir: Path | None = None) -> Path | None:
    """模板文件夹里如果恰好有一个表格文件，就用它当标准表。"""
    template_dir = template_dir or TEMPLATE_DIR
    if not template_dir.exists():
        return None
    candidates = [
        p
        for p in sorted(template_dir.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("~$")
    ]
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# 清洗一条记录
# --------------------------------------------------------------------------


def _classify_columns(
    pending: list[tuple[dict[str, object], SheetData, int]],
    fields: list[str],
    treat_as_empty: tuple[str, ...],
) -> dict[str, str]:
    """判断每一列该按哪一类清洗：``"number"`` / ``"date"`` / ``"text"``。

    **为什么必须看整列，不能看单个格子**：``"1.2万"`` 光看它自己，
    既可能是"这个人把数字写花了"（该算成 12000），也可能就是一句普通文字。
    只有确认这一列**全都是数字的各种写法**，规整它才是安全的。

    顺序上**先判数字、再判日期**：像 ``20261001`` 这样的 8 位数字，
    既可能是学号、也可能是日期。先判数字，学号就不会被错认成日期——
    而"没改错"比"改对了"重要得多。

    只要有一个格子不符合，整列就归为文本列、一个都不动：
    拿不准的宁可原样保留，人在总表里一眼能看出哪里怪。
    """
    kinds: dict[str, str] = {}
    for field_name in fields:
        values = [rv.get(field_name) for rv, _sheet, _row in pending]
        filled = [v for v in values if not is_empty(v, treat_as_empty)]
        if not filled:
            kinds[field_name] = "text"
        elif all(parse_number(v) is not None for v in filled):
            kinds[field_name] = "number"
        elif all(parse_date(v) is not None for v in filled):
            kinds[field_name] = "date"
        else:
            kinds[field_name] = "text"
    return kinds


def _clean_record_values(
    raw_values: dict[str, object],
    kinds: dict[str, str],
    excel: ExcelSettings,
) -> tuple[dict[str, object], list[tuple[str, object, object]]]:
    """清洗一条记录的值。

    返回 ``(清洗后的值, [(字段, 原值, 新值), ...])``——
    第二个元素是"值被改动过"的地方，要汇报给用户。

    只做"同一个值的不同写法"的规整（``1.2万`` → ``12000``、
    ``2026/9/1`` → ``2026-09-01``），**不做任何跨类型的猜测**。
    """
    cleaned: dict[str, object] = {}
    notable: list[tuple[str, object, object]] = []

    for field_name, raw in raw_values.items():
        if is_empty(raw, excel.treat_as_empty):
            cleaned[field_name] = None
            continue

        kind = kinds.get(field_name, "text")

        if kind == "number":
            number = parse_number(raw)
            if number is not None:
                value = _pretty_number(number)
                cleaned[field_name] = value
                if str(value) != str(raw).strip():
                    notable.append((field_name, raw, value))
                continue

        elif kind == "date":
            day = parse_date(raw)
            if day is not None:
                cleaned[field_name] = day
                # Excel 里本来就是日期格式的格子，转成 date 是正常化，
                # 不算"写法不统一"，不用打扰用户
                if not isinstance(raw, (date, datetime)) and str(raw).strip() != day.isoformat():
                    notable.append((field_name, raw, day.isoformat()))
                continue

        # 文本列；或者数字/日期列里个别解析不了的值 → 只做常规文本清洗
        result = clean_text(raw, excel.treat_as_empty)
        cleaned[field_name] = result.value

    return cleaned, notable


def _pretty_number(value) -> object:
    """把 Decimal 收拾成好看的数：整数就不带小数点。"""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(
    files: list[Path] | None = None,
    template: Path | None = None,
    settings: Settings | None = None,
    store: Store | None = None,
    progress: ProgressFn | None = None,
    dry_run: bool = False,
    output_dir: Path | None = None,
) -> PipelineResult:
    """跑一次完整汇总。

    :param dry_run: 不调用 AI（走规则匹配），**一分钱不花**。
                    用来验证代码是好的、或者你暂时不想联网。
    :param progress: 进度回调，界面用它显示"正在处理第 3/12 个文件"
    :param output_dir: 结果写到哪里；默认写到「输出」文件夹
    """
    settings = settings or Settings()
    store = store or Store()
    report = progress or (lambda _msg, _pct: None)

    files = list(files) if files is not None else list_inbox()
    if not files:
        raise PipelineError(
            "「收件箱」里没有找到表格文件。\n"
            "请把各下级单位交上来的 Excel 放进「收件箱」文件夹，再点开始。"
        )
    if len(files) > settings.aggregate.max_files_per_run:
        raise PipelineError(
            f"一次最多处理 {settings.aggregate.max_files_per_run} 个文件，"
            f"这次选了 {len(files)} 个。\n"
            "如果确实要处理这么多，请打开 config/settings.toml，把 max_files_per_run 调大。"
        )

    result = PipelineResult()
    result.run_id = store.start_run(len(files))

    # ---- 1. 读文件 ----------------------------------------------------
    report("正在读取表格…", 0.05)
    sheets: list[SheetData] = []
    for idx, path in enumerate(files, start=1):
        outcome = FileOutcome(path=path, ok=False)
        try:
            found = read_workbook(path, settings.excel)
        except ExcelReadError as exc:
            outcome.error = str(exc)
            result.outcomes.append(outcome)
            result.warnings.append(f"读不了 {path.name}：{exc}")
            continue

        outcome.ok = True
        outcome.sheets = len(found)
        outcome.rows = sum(s.n_rows for s in found)
        outcome.dropped_total_rows = sum(s.dropped_total_rows for s in found)
        outcome.unit_hint = next((s.title_hint for s in found if s.title_hint), "")
        result.outcomes.append(outcome)
        sheets.extend(found)
        report(f"正在读取表格…（{idx}/{len(files)}）", 0.05 + 0.2 * idx / len(files))

    if not sheets:
        detail = "\n".join(f"· {o.path.name}：{o.error}" for o in result.outcomes if o.error)
        raise PipelineError("所有文件都读不了，没法继续。\n\n" + detail)

    # ---- 2. 确定标准字段 -------------------------------------------------
    report("正在确定标准表的列…", 0.28)
    template_columns: list[str] = []
    if template is not None:
        try:
            template_columns = read_template_columns(template, settings.excel)
        except ExcelReadError as exc:
            result.warnings.append(f"模板读不了（{exc}），改用列最全的那份表当基准。")
    result.fields = pick_target_fields(sheets, template_columns)
    if not result.fields:
        raise PipelineError("没能确定标准表有哪些列，请检查表格是否有表头。")

    # ---- 3. 列语义对齐（这一步要用 AI）-----------------------------------
    client: LLMClient | None = None
    if not dry_run:
        api_key = get_api_key()
        if api_key:
            client = LLMClient(
                settings.llm, api_key, cost_limit_cny=settings.aggregate.cost_limit_cny
            )
        else:
            result.warnings.append(
                "没有检测到 API 密钥，这次用「名称相似度」匹配（不花钱，但准确率低）。"
                "双击「设置密钥.bat」设置密钥后可以启用 AI。"
            )

    field_samples = collect_field_samples(sheets, result.fields)
    mappings_by_sheet: list[MappingResult] = []

    for idx, sheet in enumerate(sheets, start=1):
        label = sheet.file_label
        signature = signature_of([c.name for c in sheet.columns])
        cached_entry = store.get_mapping(signature)

        # 缓存里记的是"上次这份表头是怎么对的"。用户亲手确认过的优先级最高，
        # 永远不会被 AI 的结果覆盖——这正是"越用越准"的机制。
        #
        # 但如果用户换了模板，旧缓存里会指向已经不存在的字段，map_sheet 会
        # 判定它不适用并重新对齐（见 mapper._cache_applies）。
        cached_mapping = cached_entry.mapping if cached_entry is not None else None
        cached_source = "user" if (cached_entry and cached_entry.source == "user") else "cache"

        mapping = map_sheet(
            sheet,
            result.fields,
            client,
            settings.aggregate,
            field_samples=field_samples,
            cached=cached_mapping,
            cache_source=cached_source,
            cached_confidence=cached_entry.confidence if cached_entry else None,
        )

        if mapping.error and client is not None:
            # AI 没调通、降级用规则了。**必须说出来**——不然用户只会看到
            # 满屏的"待确认"，还以为是自己表格的问题，白折腾半天。
            result.warnings.append(
                f"{label}：AI 没能用上，这份表改成按名称相似度对列名了，"
                f"所以标黄会比较多。原因：{mapping.error}"
            )

        if mapping.source == "user":
            result.warnings.append(f"{label}：用了你上次确认过的对应关系。")
        elif mapping.source == "cache":
            if client is not None:
                client.usage.cache_hits += 1
        elif mapping.source == "llm":
            # AI 的结果存下来，下次同样的表头就不再花钱问了。
            #
            # **把握程度必须一起存**：不然"上次没把握、需要你确认"的列，
            # 下次会被当成"确定无疑"，那几列错配了你也永远不会被提醒。
            store.put_mapping(
                signature, mapping.mapping, source="llm", confidence=mapping.confidence
            )
        # 规则兜底的结果**不存**：它一分钱不花、也不算数（全是"待确认"），
        # 存了只会让下次本该问 AI 的时候直接吃缓存，反而更差。

        mappings_by_sheet.append(mapping)
        result.mappings.append((label, mapping))
        result.signatures[label] = signature
        report(
            f"正在对齐列（{idx}/{len(sheets)}）…",
            0.3 + 0.35 * idx / len(sheets),
        )

    # 把每个文件的结果回填到 outcome，供界面显示
    for outcome in result.outcomes:
        outcome.uncertain = sum(
            1
            for label, m in result.mappings
            if label.startswith(outcome.path.name) and m.uncertain
        )
        sources = [
            m.source for label, m in result.mappings if label.startswith(outcome.path.name)
        ]
        outcome.mapping_source = sources[0] if sources else ""

    # ---- 4. 清洗 + 5. 合并 ----------------------------------------------
    report("正在清洗和合并数据…", 0.68)
    unit_field = _find_unit_field(result.fields)

    # 先把所有原始行攒起来，**不急着清洗**。
    # "这一列该怎么清洗"要看完整列才能定（见 _classify_columns）。
    pending: list[tuple[dict[str, object], SheetData, int]] = []
    for sheet, mapping in zip(sheets, mappings_by_sheet):
        # 哪一列对应哪个标准字段（一个字段只认一列）
        column_to_field: dict[int, str] = {}
        taken: set[str] = set()
        for column in sheet.columns:
            target = mapping.mapping.get(column.name)
            if not target or target in (IGNORE_FIELD, UNKNOWN_FIELD):
                continue
            if target in taken:
                continue
            column_to_field[column.index] = target
            taken.add(target)

        # 表体里没有"单位"列时，用标题里推断出来的单位兜底
        fallback_unit = ""
        if unit_field and unit_field not in taken and sheet.title_hint:
            fallback_unit = sheet.title_hint

        for raw_row, excel_row in zip(sheet.rows, sheet.row_numbers):
            raw_values: dict[str, object] = {}
            for column in sheet.columns:
                target = column_to_field.get(column.index)
                if target:
                    raw_values[target] = raw_row[column.index] if column.index < len(raw_row) else None
            if fallback_unit:
                raw_values[unit_field] = fallback_unit

            if all(v is None or (isinstance(v, str) and not v.strip()) for v in raw_values.values()):
                continue
            pending.append((raw_values, sheet, excel_row))

    if not pending:
        raise PipelineError(
            "读到了表格，但没能提取出任何数据行。\n"
            "最常见的原因：表头行没识别对。请确认表格有明确的表头行（如「单位」「姓名」「数量」）。"
        )

    kinds = _classify_columns(pending, result.fields, settings.excel.treat_as_empty)

    for raw_values, sheet, excel_row in pending:
        cleaned, notable = _clean_record_values(raw_values, kinds, settings.excel)
        record = Record(
            values=cleaned,
            raw=raw_values,
            file=sheet.path.name,
            sheet=sheet.sheet_name if sheet.sheet_name not in ("Sheet1", "Sheet") else "",
            row=excel_row,
        )
        result.records.append(record)

        for field_name, before, after in notable:
            result.issues.append(
                Issue(
                    level="info",
                    problem=f"「{field_name}」的写法不统一，已自动规整：{before} → {after}",
                    suggestion="如果规整错了，请在总表里手工改回来",
                    file=record.file,
                    sheet=record.sheet,
                    row=record.row,
                    column=field_name,
                    value=before,
                )
            )

    # ---- 6. 校验 --------------------------------------------------------
    report("正在检查数据…", 0.82)
    uncertain_fields = {
        c for _label, m in result.mappings for c in m.uncertain if c in set(result.fields)
    }
    result.issues.extend(validate(result.records, result.fields, uncertain_fields))

    # ---- 7. 输出 --------------------------------------------------------
    report("正在生成结果文件…", 0.92)
    summary_path, issues_path = default_output_paths(output_dir or OUTPUT_DIR)
    write_summary(result.records, result.fields, summary_path, uncertain_fields)
    result.summary_path = summary_path
    result.issues_path = issues_path

    if client is not None:
        result.usage_line = client.usage.describe(settings.llm.model)
        result.cost_cny = client.usage.cost_cny(settings.llm.model)

    write_issues(result.issues, issues_path, _run_note(result, store))
    if result.run_id is not None:
        store.finish_run(result.run_id, len(result.records), len(result.issues), result.cost_cny)

    report("完成", 1.0)
    return result


def _find_unit_field(fields: list[str]) -> str:
    """找出"单位"那一列——表体里没有时要用标题里的单位补上。"""
    for name in fields:
        if any(hint in name for hint in ("单位", "部门", "学院", "院系", "科室")):
            return name
    return ""


def apply_confirmation(
    store: Store,
    result: PipelineResult,
    signatures: dict[str, str],
    confirmed: dict[str, dict[str, str | None]],
) -> int:
    """把用户确认的列对应关系写进缓存，并重新生成问题清单。返回更新了几份表。

    这里存的是 ``source="user"``——**优先级最高**，下次遇到同样的表头直接按你教的
    对齐，既不会再问 AI，也不会被 AI 的结果覆盖。

    这正是这套工具"越用越准、越用越省钱"的机制：第一次需要你确认，
    第二次它就已经会了。

    :param signatures: ``{文件标签: 表头指纹}``，由 ``run()`` 一并返回
    """
    updated = 0
    for label, mapping in result.mappings:
        if label not in confirmed:
            continue
        for column, target in confirmed[label].items():
            mapping.mapping[column] = target
            mapping.confidence[column] = 1.0
            mapping.reasons[column] = "你确认过的"

        merged = dict(mapping.mapping)
        signature = signatures.get(label) or signature_of(list(merged.keys()))
        store.put_mapping(signature, merged, source="user")
        updated += 1

    # 确认之后，"拿不准"的提醒就过时了，重新生成问题清单
    if result.issues_path is not None:
        still_uncertain = {
            c for _label, m in result.mappings for c in m.uncertain if c in set(result.fields)
        }
        result.issues = [
            i
            for i in result.issues
            if not (i.level == "info" and i.column in result.fields and i.column not in still_uncertain)
        ]
        result.issues.extend(validate(result.records, result.fields, still_uncertain))
        write_issues(result.issues, result.issues_path, _run_note(result, store))

    if result.run_id is not None:
        store.finish_run(
            result.run_id, len(result.records), len(result.issues), result.cost_cny
        )
    return updated


def _run_note(result: PipelineResult, store: Store | None = None) -> str:
    """问题清单末尾的那段"本次做了什么"的说明。"""
    note = f"本次共处理 {len(result.outcomes)} 个文件、{len(result.records)} 行数据。"
    if result.usage_line:
        note += f"\n{result.usage_line}"
    if result.dropped_total_rows:
        note += f"\n已自动剔除 {result.dropped_total_rows} 行「合计」行，避免混进总表。"
    if store is not None:
        total = store.total_cost()
        if total:
            note += f"\n历史累计花费约 {total:.4f} 元。"
    return note


def rewrite_issues(result: PipelineResult, store: Store | None = None) -> None:
    """用当前的问题列表重写问题清单文件。"""
    if result.issues_path is not None:
        write_issues(result.issues, result.issues_path, _run_note(result, store))


def preview_masked(text: str, privacy: PrivacySettings) -> str:
    """给界面用：展示"这句话发给 AI 之后长什么样"，让用户亲眼看到脱敏效果。"""
    enabled = {
        name
        for name, on in (
            ("mask_id_card", privacy.mask_id_card),
            ("mask_phone", privacy.mask_phone),
            ("mask_bank_card", privacy.mask_bank_card),
            ("mask_email", privacy.mask_email),
        )
        if on
    }
    return mask_text(text, enabled)


__all__ = [
    "FileOutcome",
    "PipelineError",
    "PipelineResult",
    "apply_confirmation",
    "find_template",
    "list_inbox",
    "preview_masked",
    "rewrite_issues",
    "run",
]
