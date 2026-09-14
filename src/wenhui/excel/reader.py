"""读 Excel，把"人写的表"翻译成"程序能对齐的表"。

这是整个项目最难、也最容易翻车的一步。下面几件事必须做对：

1. **合并单元格**：下级交上来的表，表头几乎一定有合并单元格（"基本信息"横跨三列）。
   读取时要把它展开——每个被合并覆盖的格子都填上左上角的值，
   否则那三列里有两列是空的，表头就丢了。

2. **表头在第几行**：有人从第 1 行开始，有人前面写了标题、文号、盖章位置，
   表头在第 4 行甚至第 7 行。所以要**打分挑出最像表头的那一行**。

3. **多级表头**：常见"单位 | 招生人数 | 毕业人数"这种两行表头，
   要合并成"单位"、"招生人数"这样的一维列名。

4. **保留行号**：出问题时得能告诉用户"第 12 行少填了"，所以原始行号一路带着走。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ExcelSettings

# --------------------------------------------------------------------------
# 判断一个格子"像什么"
# --------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"^[-+]?[\d,，\s]*(\.\d+)?%?$")
_DATE_RE = re.compile(r"^\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?$")
_PURE_NUMBER_RE = re.compile(r"^[-+]?\d+(\.\d+)?$")

#: 表头里常见的字眼。命中越多，这一行越可能是表头。
#: 这只是"加分项"不是"必需项"——不认识的表头照样能靠其它特征认出来。
_HEADER_HINTS = (
    "单位", "部门", "学院", "名称", "序号", "编号", "姓名", "学号", "工号",
    "数量", "人数", "金额", "合计", "小计", "总计", "日期", "时间", "备注",
    "电话", "手机", "地址", "专业", "年级", "班级", "性别", "比例", "比例",
    "招生", "毕业", "就业", "教师", "学生", "经费", "预算", "面积", "类型",
    "状态", "说明", "内容", "项目", "指标", "得分", "结果",
)

#: 多级表头拼接时用的连接符
HEADER_JOINER = "·"

#: "合计行"的标记词。几乎每份下级表最后都有一行合计，
#: 不剔掉的话，总表里会多出一条"张三=合计"这样的假数据。
_TOTAL_MARKERS = ("合计", "总计", "小计", "共计", "累计", "总和", "汇总")

#: "填报单位：XX学院"这类元信息的模式
_META_UNIT_RE = re.compile(
    r"(?:填报单位|报送单位|单位名称|二级单位|所在学院|院系|部门|单位)\s*[：:]\s*(\S{2,30})"
)

#: 安全上限，防止格式错乱的文件把内存吃光
_MAX_ROWS = 20_000
_MAX_COLS = 200


class ExcelReadError(RuntimeError):
    """文件读不了，且原因要说给不懂技术的人听。"""


@dataclass
class ColumnInfo:
    """一列的信息。"""

    index: int                      # 在表格里的第几列（从 0 数）
    name: str                       # 合并多级表头之后的列名，给模型看的
    parts: list[str] = field(default_factory=list)   # 分级表头的原始各层
    samples: list[str] = field(default_factory=list)  # 原始样本值（**未脱敏**，脱敏在发给模型前做）

    @property
    def is_blank(self) -> bool:
        return not self.name.strip() and not self.samples


@dataclass
class SheetData:
    """一张工作表读出来的结果。"""

    path: Path
    sheet_name: str
    header_rows: list[int]          # 表头占了哪几行（Excel 里的行号，从 1 开始）
    columns: list[ColumnInfo]
    rows: list[list[object]]        # 数据行，按 columns 对齐
    row_numbers: list[int]          # 每行在 Excel 里的原始行号
    merged_count: int = 0           # 展开了多少个合并区（用于自检报告）
    #: 表头**上方**的文字里推断出的"报送单位"。
    #: 下级表常把单位写在标题或"填报单位：XX"里，而表体里根本没有这一列——
    #: 没有它，总表的"单位"列会整列空着。
    title_hint: str = ""
    dropped_total_rows: int = 0     # 剔掉了几行合计（报告给用户看）

    @property
    def file_label(self) -> str:
        """给用户看的文件标识。"""
        if self.sheet_name and self.sheet_name not in ("Sheet1", "Sheet"):
            return f"{self.path.name}（工作表：{self.sheet_name}）"
        return self.path.name

    @property
    def n_rows(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------
# 格子级判断
# --------------------------------------------------------------------------


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _looks_like_number(value: object) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and bool(_PURE_NUMBER_RE.match(text))
    return False


def _looks_like_date(value: object) -> bool:
    if hasattr(value, "year") and hasattr(value, "month"):  # datetime / date
        return True
    if isinstance(value, str):
        return bool(_DATE_RE.match(value.strip()))
    return False


def _looks_like_text(value: object) -> bool:
    """像是"人写的标题/名称"，而不是数字或日期。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if _looks_like_date(text):
        return False
    # 纯数字（含千分位、百分号）不算文字
    if _PURE_NUMBER_RE.match(text):
        return False
    if _NUMERIC_RE.match(text) and any(ch.isdigit() for ch in text):
        return False
    return True


def _hint_score(cells: list[object]) -> int:
    """这一行里出现了多少个"表头常见字眼"。"""
    hits = 0
    for cell in cells:
        if isinstance(cell, str):
            text = cell.strip()
            if any(hint in text for hint in _HEADER_HINTS):
                hits += 1
    return hits


# --------------------------------------------------------------------------
# 表头探测
# --------------------------------------------------------------------------


def score_header_row(cells: list[object], next_cells: list[object] | None = None) -> float:
    """给"这一行有多像表头"打分。分数越高越像。

    判据（都是常见表格的统计规律，不依赖某一种模板）：

    - 非空格子越多越像（表头通常横跨整表）
    - 文字格子占比越高越像（表头是字，数据行是数）
    - 重复值越少越像（表头一般不重复）
    - 命中"单位/姓名/金额"这类字眼加分
    - **下面一行如果是数据（有数字），强烈加分** —— 表头底下就该是数据
    - 自己如果是数字/日期居多，扣分
    """
    values = [c for c in cells if not _is_blank(c)]
    if not values:
        return -1.0

    total = len(values)
    text_count = sum(1 for v in values if _looks_like_text(v))
    number_count = sum(1 for v in values if _looks_like_number(v))
    date_count = sum(1 for v in values if _looks_like_date(v))

    score = 0.0
    # 覆盖面：非空格子数（封顶 15，避免超宽表碾压正常表）
    score += min(total, 15) * 1.0
    # 文字占比
    score += (text_count / total) * 12.0
    # 去重度
    unique = len({str(v).strip() for v in values})
    score += (unique / total) * 4.0
    # 常见字眼
    score += min(_hint_score(values), 6) * 1.5
    # 惩罚：数字/日期太多的行，更像数据行而不是表头
    score -= (number_count / total) * 8.0
    score -= (date_count / total) * 6.0

    # 关键判据：下一行是数据 → 这一行极可能是表头
    if next_cells:
        below = [c for c in next_cells if not _is_blank(c)]
        if below:
            below_data = sum(1 for v in below if _looks_like_number(v) or _looks_like_date(v))
            score += (below_data / len(below)) * 8.0

    return score


def detect_header_block(
    grid: list[list[object]], scan_rows: int
) -> tuple[int, int]:
    """找出表头占的行区间，返回 ``(起始下标, 结束下标)``，都是 0 起的闭区间。

    做法：先挑出**最像表头的那一行**作为表头块的最后一行，
    再往上看——如果上面一行也像表头（且没有数字/日期），就把它一起并进来，
    这样两行、三行的多级表头都能收进来。
    """
    if not grid:
        raise ExcelReadError("这个文件是空的，里面没有任何内容。")

    limit = max(1, min(scan_rows, len(grid)))
    best_idx, best_score = -1, float("-inf")
    for i in range(limit):
        nxt = grid[i + 1] if i + 1 < len(grid) else None
        s = score_header_row(grid[i], nxt)
        if s > best_score:
            best_idx, best_score = i, s

    if best_idx < 0 or best_score <= 0:
        # 完全找不到像表头的行——退而求其次，用第一个非空行
        for i, row in enumerate(grid[:limit]):
            if any(not _is_blank(c) for c in row):
                return i, i
        raise ExcelReadError("这个文件里没找到表头，也找不到任何内容，请确认文件没选错。")

    # 往上扩展：把多级表头的上层并进来
    start = best_idx
    while start - 1 >= 0:
        upper = grid[start - 1]
        upper_values = [c for c in upper if not _is_blank(c)]
        if not upper_values:
            break

        # 关键判据：整行只有一种取值的，不是表头层。
        #
        # 合并单元格展开之后，跨整行的大标题（"XX学院2026年统计表"）会变成
        # 一整行重复的同一句话，看上去"像表头"。但它只有一个取值，
        # 而真正的多级表头分组（"基本信息"横跨两列 + "教学情况"横跨四列）
        # 至少有两种取值。用这个把它们区分开。
        if len({_normalize_cell(v) for v in upper_values}) < 2:
            break

        # 上层必须"像表头"：以文字为主，不能是数字/日期
        text_ratio = sum(1 for v in upper_values if _looks_like_text(v)) / len(upper_values)
        has_numbers = any(_looks_like_number(v) or _looks_like_date(v) for v in upper_values)
        if text_ratio >= 0.6 and not has_numbers:
            start -= 1
        else:
            break

    return start, best_idx


def _normalize_cell(value: object) -> str:
    """比较用：去掉所有空白、统一大小写。"""
    return re.sub(r"[\s　]+", "", str(value)).casefold() if value is not None else ""


def _is_total_row(row: list[object]) -> bool:
    """判断是不是"合计/总计"行。

    只看**前两个**非空单元格——因为合计行的标记词总是在最左边，
    而数据行里"备注"列也可能出现"合计"字样（"合计 3 人"），不能误伤。
    """
    checked = 0
    for cell in row:
        if _is_blank(cell):
            continue
        text = _normalize_cell(cell)
        if any(marker in text for marker in _TOTAL_MARKERS):
            return True
        checked += 1
        if checked >= 2:
            break
    return False


def _extract_unit_hint(rows_above_header: list[list[object]]) -> str:
    """从表头上方的文字里，尽力猜出"这份表是哪个单位报的"。

    两种常见写法：

    - 元信息行：``填报单位：计算机学院`` —— 直接取值
    - 大标题：``计算机学院2026年师资情况统计表`` —— 去掉年份和常见后缀

    猜不到就返回空字符串——**宁可空着，也不能猜错单位**，猜错会导致整列数据挂错单位。
    """
    texts: list[str] = []
    for row in rows_above_header:
        for cell in row:
            if isinstance(cell, str) and cell.strip():
                texts.append(cell.strip())

    for text in texts:
        match = _META_UNIT_RE.search(text)
        if match:
            candidate = match.group(1).strip()
            if 2 <= len(candidate) <= 30:
                return candidate

    # 退一步：从标题里剥掉年份、常见后缀，剩下的可能就是单位名
    for text in texts:
        if len(text) < 4 or len(text) > 60:
            continue
        candidate = re.sub(r"^\d{4}\s*年?", "", text)
        candidate = re.sub(
            r"(情况)?(统计)?(汇总)?(填)?(报)?(申)?(表|报表|清单|一览表|统计表)\s*$", "", candidate
        )
        candidate = re.sub(r"\d{4}\s*年?", "", candidate).strip(" -—·（()）")
        if 2 <= len(candidate) <= 30:
            return candidate
    return ""


#: 这些分组名只是"抽屉标签"，本身不带信息量。
#: "基本信息·姓名" 和 "姓名" 是一回事，加上前缀只会让列名变长、变难认。
#: 但像"招生人数 / 毕业人数"里的"招生""毕业"是有信息量的，必须保留——
#: 所以这里只列真正通用的那几个词。
_GENERIC_GROUPS = (
    "基本信息", "基本情况", "基础信息", "教学情况", "其他", "其它",
    "备注", "说明", "补充", "详细", "相关", "情况", "信息", "具体",
)


def flatten_headers(header_block: list[list[object]]) -> list[str]:
    """把多行表头压成一维列名。

    ``["基本信息", "基本信息"] + ["姓名", "学号"]`` → ``["姓名", "学号"]``
    （"基本信息"是通用抽屉名，丢掉不影响含义）

    ``["招生", "招生"] + ["人数", "比例"]`` → ``["招生·人数", "招生·比例"]``
    （"招生"有信息量，必须保留，否则和"毕业人数"分不清）

    上层和下层文字相同时只留一个（合并单元格展开后常出现这种情况）。
    下层为空时用上层兜底，避免这一列没有名字。
    """
    if not header_block:
        return []
    width = max(len(r) for r in header_block)
    names: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in header_block:
            value = row[col] if col < len(row) else None
            text = str(value).strip() if value is not None else ""
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        # 丢掉开头的通用分组名，但至少保留一层，别把列名清空
        while len(parts) > 1 and parts[0] in _GENERIC_GROUPS:
            parts.pop(0)
        names.append(HEADER_JOINER.join(parts))
    return names


# --------------------------------------------------------------------------
# 读文件
# --------------------------------------------------------------------------


def _build_grid(ws) -> tuple[list[list[object]], int]:
    """把工作表读成二维列表，**并把合并单元格展开**。

    返回 ``(grid, 展开了多少个合并区)``。
    """
    max_row = min(ws.max_row or 0, _MAX_ROWS)
    max_col = min(ws.max_column or 0, _MAX_COLS)
    if max_row == 0 or max_col == 0:
        return [], 0

    grid: list[list[object]] = [
        [ws.cell(row=r, column=c).value for c in range(1, max_col + 1)]
        for r in range(1, max_row + 1)
    ]

    merged_count = 0
    for rng in ws.merged_cells.ranges:
        if rng.min_row > max_row or rng.min_col > max_col:
            continue
        top_left = grid[rng.min_row - 1][rng.min_col - 1]
        if top_left is None:
            continue
        merged_count += 1
        for r in range(rng.min_row, min(rng.max_row, max_row) + 1):
            for c in range(rng.min_col, min(rng.max_col, max_col) + 1):
                if r == rng.min_row and c == rng.min_col:
                    continue
                grid[r - 1][c - 1] = top_left

    return grid, merged_count


def _trim_grid(
    grid: list[list[object]], first_row_number: int = 1
) -> tuple[list[list[object]], list[int]]:
    """裁掉四周完全空白的行与列，并给出每行在 Excel 里的原始行号。"""
    row_numbers = [first_row_number + i for i in range(len(grid))]

    # 去尾部空行
    while grid and all(_is_blank(c) for c in grid[-1]):
        grid.pop()
        row_numbers.pop()

    # 去头部空行
    while grid and all(_is_blank(c) for c in grid[0]):
        grid.pop(0)
        row_numbers.pop(0)

    if not grid:
        return [], []

    # 去尾部空列 / 头部空列
    width = max(len(r) for r in grid)
    keep = [
        c
        for c in range(width)
        if any(c < len(r) and not _is_blank(r[c]) for r in grid)
    ]
    if not keep:
        return [], []
    grid = [[(r[c] if c < len(r) else None) for c in keep] for r in grid]

    return grid, row_numbers


def _sheet_to_data(
    grid_all: list[list[object]],
    path: Path,
    sheet_name: str,
    settings: ExcelSettings,
    merged_count: int,
) -> SheetData | None:
    """把已经读好的二维网格，变成带列信息和数据行的 ``SheetData``。"""
    # _trim_grid 会就地裁剪，所以传副本进去
    grid, row_numbers = _trim_grid([row[:] for row in grid_all])
    if not grid:
        return None

    start, end = detect_header_block(grid, settings.header_scan_rows)
    header_block = grid[start : end + 1]
    names = flatten_headers(header_block)

    # 表头上方的文字里，常常藏着"这份表是哪个单位报的"
    title_hint = _extract_unit_hint(grid[:start])

    body = grid[end + 1 :]
    body_row_numbers = row_numbers[end + 1 :]

    # 丢掉空行（下级表常拖着几十行空白）和合计行（否则总表里会多出假数据）
    keep_idx: list[int] = []
    dropped_totals = 0
    for i, row in enumerate(body):
        if all(_is_blank(c) for c in row):
            continue
        if _is_total_row(row):
            dropped_totals += 1
            continue
        keep_idx.append(i)
    body = [body[i] for i in keep_idx]
    body_row_numbers = [body_row_numbers[i] for i in keep_idx]

    # 组装列信息，同时收集样本值
    columns: list[ColumnInfo] = []
    for col_idx, name in enumerate(names):
        samples: list[str] = []
        for row in body:
            if col_idx < len(row):
                value = row[col_idx]
                if not _is_blank(value):
                    text = str(value).strip()
                    if text and text not in samples:
                        samples.append(text)
        columns.append(
            ColumnInfo(
                index=col_idx,
                name=name,
                parts=[str(h[col_idx]).strip() for h in header_block if col_idx < len(h) and h[col_idx] is not None],
                samples=samples,
            )
        )

    # 去掉完全没用的空列（表头空、数据也全空）
    useful = [c for c in columns if not c.is_blank]
    if not useful:
        return None
    useful_idx = [c.index for c in useful]

    return SheetData(
        path=path,
        sheet_name=sheet_name,
        header_rows=[row_numbers[start] + i for i in range(end - start + 1)],
        columns=useful,
        rows=[[row[i] if i < len(row) else None for i in useful_idx] for row in body],
        row_numbers=body_row_numbers,
        merged_count=merged_count,
        title_hint=title_hint,
        dropped_total_rows=dropped_totals,
    )


def read_workbook(path: Path, settings: ExcelSettings) -> list[SheetData]:
    """读一个 Excel 文件里的**所有**工作表，返回非空的工作表列表。

    支持 ``.xlsx`` / ``.xlsm`` / ``.xls`` / ``.csv``。
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return _read_csv(path, settings)

    if suffix == ".xls":
        return _read_legacy_xls(path, settings)

    if suffix not in (".xlsx", ".xlsm"):
        raise ExcelReadError(
            f"不认识的文件类型：{path.name}\n"
            "目前支持 .xlsx、.xlsm、.xls、.csv 这几种表格文件。"
        )

    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 依赖没装好才会走到
        raise ExcelReadError("缺少 openpyxl 组件，请重新运行「启动.bat」安装依赖。") from exc

    try:
        # data_only=True：取公式算出来的值，而不是公式本身
        wb = load_workbook(filename=str(path), data_only=True, read_only=False)
    except Exception as exc:
        raise ExcelReadError(
            f"打不开文件：{path.name}\n"
            f"原因：{exc}\n"
            "常见情况：文件正在 Excel 里打开着（请先关闭）、文件损坏、或者是加密文件。"
        ) from exc

    results: list[SheetData] = []
    for ws in wb.worksheets:
        grid, merged_count = _build_grid(ws)
        if not grid:
            continue
        data = _sheet_to_data(grid, path, ws.title, settings, merged_count)
        if data is not None:
            results.append(data)
    wb.close()

    if not results:
        raise ExcelReadError(
            f"{path.name} 里没读到任何有效表格。\n"
            "请确认：表格不在隐藏工作表里、不是图片截图、确实有表头和数据。"
        )
    return results


def _read_legacy_xls(path: Path, settings: ExcelSettings) -> list[SheetData]:
    """读老的 .xls 格式。用 python-calamine，它读不了合并单元格信息，要如实告知。"""
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as exc:  # pragma: no cover
        raise ExcelReadError(
            f"读 .xls 需要 python-calamine 组件，请重新运行「启动.bat」安装依赖。\n"
            f"（或者：用 Excel 把 {path.name} 另存为 .xlsx 再试）"
        ) from exc

    try:
        wb = CalamineWorkbook.from_path(str(path))
        sheets = wb.sheet_names
    except Exception as exc:
        raise ExcelReadError(f"打不开文件：{path.name}\n原因：{exc}") from exc

    results: list[SheetData] = []
    for name in sheets:
        rows = wb.get_sheet_by_name(name).to_python()
        grid = [list(r) for r in rows]
        if not grid:
            continue
        data = _sheet_to_data(grid, path, name, settings, 0)
        if data is not None:
            results.append(data)

    if not results:
        raise ExcelReadError(f"{path.name} 里没读到任何有效表格。")
    return results


def _read_csv(path: Path, settings: ExcelSettings) -> list[SheetData]:
    """读 CSV。国内导出的 CSV 常是 GBK，所以编码要试探着来。"""
    text: str | None = None
    used_encoding = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            used_encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ExcelReadError(
            f"读不了 {path.name} 的文字编码。\n"
            "建议：用 Excel 打开它，另存为 .xlsx 再试一次。"
        )

    import csv as _csv

    rows = list(_csv.reader(text.splitlines()))
    grid = [list(r) for r in rows]
    if not grid:
        raise ExcelReadError(f"{path.name} 是空文件。")

    data = _sheet_to_data(grid, path, used_encoding, settings, 0)
    if data is None:
        raise ExcelReadError(f"{path.name} 里没读到任何有效表格。")
    return [data]
