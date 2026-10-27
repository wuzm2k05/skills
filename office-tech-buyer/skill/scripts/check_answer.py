#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""办公设备易采买 —— 交付前答案自检。

宿主 AI 渲染完比价回答后，把回答原文喂给本脚本；只要「购买链接跑到表格外面」就以
非零退出码报错，宿主 AI 据此重新生成。检查覆盖三种走样形态：

1. 比价表压根没有【购买链接】列（该列被删掉）；
2. 有该列但单元格为空 / 占位符（`-`、`url` 之类），或直接粘了裸长链接；
3. 链接写在表格下方、回答末尾、或另起一段的「购买链接：」清单里。

用法::

    uv run scripts/check_answer.py --file answer.md     # 从文件读（推荐）
    cat answer.md | uv run scripts/check_answer.py      # 从 stdin 读
    uv run scripts/check_answer.py --text '回答原文'     # 直接传入

输出 JSON（与 ``main.py`` 保持一致的形态）：``status`` 为 ``PASS`` / ``FAIL``，
``violations`` 逐条给出规则名、行号与原文片段，``instruction`` 是给宿主 AI 的重写指令。

退出码：0 = 无必须修正项（可能仍有 warning）；1 = 存在 error，必须重新生成；
2 = 输入有问题（未提供内容、文件读不到）。仅依赖标准库。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from dataclasses import asdict, dataclass

# --------------------------------------------------------------------------- #
# 规则常量
# --------------------------------------------------------------------------- #
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: 链接列里「确实拿不到链接」的合法写法（answer-rules 第 5 节：没有拿到链接的写"未获取"）
MISSING_ACK_MARKERS = ("未获取", "未提供", "暂无链接", "未取到")

#: 链接列里属于「根本没写」的占位写法。归一化后精确匹配，一律算 error。
PLACEHOLDER_CELLS = {
    "", "-", "--", "---", "—", "――", "——", "/", "\\", "·", ".", "…",
    "n/a", "na", "none", "null", "tbd", "todo",
    "无", "空", "略", "待补", "待填", "待补充", "待定",
    "见下方", "详见下方", "见下表", "见表格", "文末", "回答末尾",
    "链接", "购买链接", "url", "点击购买", "点击查看",
}

# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #
#: 表格分隔行单元格：``---`` / ``:---`` / ``---:`` / ``:---:``
RE_SEP_CELL = re.compile(r"^:?-{2,}:?$")
#: Markdown 链接；开头的 ``(?<!!)`` 用来排除图片 ``![alt](url)``
RE_MD_LINK = re.compile(
    r"(?<!!)\[([^\]\n]*)\]\(\s*(<[^>\n]*>|[^()\s]*(?:\([^()]*\)[^()\s]*)*)\s*\)"
)
#: 裸 URL（不在 Markdown 链接里的那种）
RE_BARE_URL = re.compile(r"https?://[^\s<>()\[\]\"'“”‘’，。；;、）】]+", re.I)
#: 淘宝/天猫淘口令，如 ``￥AbC123xyz￥``
RE_TAOCODE = re.compile(r"￥[0-9A-Za-z]{6,}￥")
RE_HTTP = re.compile(r"^https?://", re.I)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Violation:
    """一条不合规记录。``line`` 为 1 起的原始行号，``None`` 表示整表级问题。"""

    rule: str
    severity: str
    message: str
    line: int | None = None
    snippet: str = ""


@dataclass
class Table:
    """一个解析出来的 Markdown 表格。"""

    start_line: int
    header: list[str]
    rows: list[tuple[int, list[str]]]
    is_price_table: bool = False
    link_column: int | None = None


# --------------------------------------------------------------------------- #
# Markdown 表格解析
# --------------------------------------------------------------------------- #
def has_unescaped_pipe(text: str) -> bool:
    """行内是否存在「真正的」分隔竖线（跳过 ``\\|`` 转义与行内代码里的竖线）。"""
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "`":
            close = text.find("`", i + 1)
            i = n if close == -1 else close + 1
            continue
        if char == "|":
            return True
        i += 1
    return False


def split_row(line: str) -> list[str]:
    """按未转义的 ``|`` 切分表格行，行内代码段整体保留在一个单元格里。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]

    cells: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n:
            buf.append(text[i : i + 2])
            i += 2
            continue
        if char == "`":
            close = text.find("`", i + 1)
            if close == -1:
                buf.append(text[i:])
                i = n
                continue
            buf.append(text[i : close + 1])
            i = close + 1
            continue
        if char == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(char)
        i += 1
    cells.append("".join(buf))
    return [cell.strip() for cell in cells]


def is_separator_row(cells: list[str]) -> bool:
    """``| --- | :---: |`` 这类分隔行。"""
    real = [cell.replace(" ", "") for cell in cells if cell.strip()]
    if not real:
        return False
    return all(RE_SEP_CELL.match(cell) for cell in real)


def parse_tables(lines: list[str]) -> tuple[list[Table], set[int]]:
    """解析全部表格，同时返回「属于表格的行号集合」。

    只把以 ``|`` 开头的行当作表格行；续行额外要求列数与表头一致，这样表格后面
    恰好含竖线的散文（如 ``价格 ¥100 | 规格 A4``）不会被误吞进表格。
    """
    tables: list[Table] = []
    table_lines: set[int] = set()

    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip().startswith("|"):
            i += 1
            continue

        block: list[tuple[int, list[str]]] = [(i + 1, split_row(lines[i]))]
        width = len(block[0][1])
        i += 1
        while i < n:
            raw = lines[i]
            if not raw.strip():
                break
            if raw.strip().startswith("|"):
                block.append((i + 1, split_row(raw)))
                i += 1
                continue
            if has_unescaped_pipe(raw) and len(split_row(raw)) == width:
                block.append((i + 1, split_row(raw)))
                i += 1
                continue
            break

        for line_no, _ in block:
            table_lines.add(line_no)

        header = block[0][1]
        body = block[1:]
        if body and is_separator_row(body[0][1]):
            body = body[1:]
        tables.append(Table(start_line=block[0][0], header=header, rows=body))

    return tables, table_lines


# --------------------------------------------------------------------------- #
# 单元格与链接解析
# --------------------------------------------------------------------------- #
def strip_markup(cell: str) -> str:
    """去掉强调/代码/链接语法，只留可读文本，用于判断列名与占位符。"""
    text = RE_MD_LINK.sub(r"\1", cell)
    text = re.sub(r"[*`_~#]", "", text)
    return text.strip()


def normalize_cell(cell: str) -> str:
    """占位符归一化：去掉标记、首尾空白与句末标点，小写化。"""
    text = strip_markup(cell).strip().strip("：:。.、,，")
    return text.strip().lower()


def extract_links(text: str) -> list[dict]:
    """抽取一行/一格里的全部链接，区分 Markdown 链接与裸 URL（不重复计同一处）。"""
    found: list[dict] = []
    spans: list[tuple[int, int]] = []
    for match in RE_MD_LINK.finditer(text):
        found.append(
            {
                "kind": "markdown",
                "label": match.group(1).strip(),
                "url": match.group(2).strip().strip("<>"),
            }
        )
        spans.append((match.start(), match.end()))
    for match in RE_BARE_URL.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        found.append(
            {
                "kind": "bare",
                "label": "",
                "url": match.group(0).rstrip(".,;:!?"),
            }
        )
    return found


def has_real_url(links: list[dict]) -> bool:
    return any(RE_HTTP.match(link["url"]) for link in links)


def is_data_source_line(raw: str) -> bool:
    """数据来源声明是允许出现在表格外的带链接行（第 6 节要求原文粘贴）。"""
    return "数据来源" in raw


def is_link_column_header(cell: str) -> bool:
    return "链接" in strip_markup(cell)


def looks_like_price_table(header: list[str]) -> bool:
    """比价表的特征：既有价格列，又有平台/店铺/渠道列。

    渠道覆盖状态表（平台｜状态｜说明）没有价格列，因此不会被要求补链接列。
    """
    texts = [strip_markup(cell) for cell in header]
    has_price = any("价" in text for text in texts)
    has_platform = any(
        keyword in text for text in texts for keyword in ("平台", "店铺", "渠道")
    )
    return has_price and has_platform


def find_link_column(header: list[str]) -> int | None:
    for index, cell in enumerate(header):
        if is_link_column_header(cell):
            return index
    return None


# --------------------------------------------------------------------------- #
# 检查项
# --------------------------------------------------------------------------- #
def check_link_column(table: Table) -> list[Violation]:
    """逐行检查比价表的【购买链接】列。"""
    violations: list[Violation] = []
    index = table.link_column
    assert index is not None  # 由调用方保证

    for line_no, cells in table.rows:
        if index >= len(cells):
            violations.append(
                Violation(
                    rule="LINK_CELL_MISSING",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"第 {line_no} 行的单元格数（{len(cells)}）少于表头"
                        f"（{len(table.header)}），【购买链接】单元格缺失。"
                    ),
                    line=line_no,
                    snippet=row_snippet(cells),
                )
            )
            continue

        cell = cells[index]
        links = extract_links(cell)

        if has_real_url(links):
            # 有真实链接。规则 2.3.3 要求写成 [查看详情](url)，不能直接粘裸链接
            if not any(link["kind"] == "markdown" for link in links):
                violations.append(
                    Violation(
                        rule="BARE_URL_IN_TABLE",
                        severity=SEVERITY_ERROR,
                        message=(
                            "【购买链接】列没有用 `[查看详情](真实url)` 格式"
                            f"（当前内容：{trim(cell)}）。规则 2.3.3 禁止直接粘裸链接。"
                        ),
                        line=line_no,
                        snippet=row_snippet(cells),
                    )
                )
            continue

        text = normalize_cell(cell)
        if links:
            # 有链接语法但 URL 不是 http(s)，典型是照抄了模板里的占位符 `(url)`
            violations.append(
                Violation(
                    rule="PLACEHOLDER_URL",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"【购买链接】列是占位符（{trim(cell)}），不是真实链接。"
                        "必须用该行 goodsId + source 调 detail 拿到 data.buy_url。"
                    ),
                    line=line_no,
                    snippet=row_snippet(cells),
                )
            )
        elif any(marker in cell for marker in MISSING_ACK_MARKERS):
            violations.append(
                Violation(
                    rule="LINK_UNAVAILABLE",
                    severity=SEVERITY_WARNING,
                    message=(
                        "【购买链接】列写了「未获取」。规则允许这样写，但请确认不是漏调 detail。"
                    ),
                    line=line_no,
                    snippet=row_snippet(cells),
                )
            )
        elif text in PLACEHOLDER_CELLS:
            violations.append(
                Violation(
                    rule="EMPTY_LINK_CELL",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"【购买链接】列为空或占位（{trim(cell) or '空'}）。"
                        "每一行的链接都必须写在表格内，不能省略。"
                    ),
                    line=line_no,
                    snippet=row_snippet(cells),
                )
            )
        else:
            violations.append(
                Violation(
                    rule="LINK_CELL_UNPARSABLE",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"【购买链接】列没有可解析的 http(s) 链接（当前内容：{trim(cell)}）。"
                    ),
                    line=line_no,
                    snippet=row_snippet(cells),
                )
            )
    return violations


def check_outside_links(lines: list[str], table_lines: set[int]) -> list[Violation]:
    """表格之外的任何链接/淘口令都算违规——这正是「链接被放到表格外面」。"""
    violations: list[Violation] = []
    for line_no, raw in enumerate(lines, start=1):
        if line_no in table_lines or not raw.strip():
            continue
        if is_data_source_line(raw):
            continue

        links = extract_links(raw)
        if links:
            urls = "、".join(trim(link["url"], 60) for link in links[:3])
            violations.append(
                Violation(
                    rule="LINK_OUTSIDE_TABLE",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"表格外出现购买链接：{urls}。"
                        "链接必须作为【购买链接】列写在对应商品那一行内。"
                    ),
                    line=line_no,
                    snippet=trim(raw),
                )
            )
            continue

        tcode = RE_TAOCODE.search(raw)
        if tcode:
            violations.append(
                Violation(
                    rule="TAOCODE_OUTSIDE_TABLE",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"表格外出现淘口令 {tcode.group(0)}。"
                        "淘口令与购买链接同属下单信息，必须写在表格对应行内。"
                    ),
                    line=line_no,
                    snippet=trim(raw),
                )
            )
    return violations


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def trim(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def row_snippet(cells: list[str]) -> str:
    return trim(" | ".join(cell for cell in cells))


def analyze(text: str, strict: bool) -> dict:
    lines = text.splitlines()
    tables, table_lines = parse_tables(lines)

    violations: list[Violation] = []
    for table in tables:
        table.is_price_table = looks_like_price_table(table.header)
        table.link_column = find_link_column(table.header)
        if not table.is_price_table:
            continue
        if table.link_column is None:
            violations.append(
                Violation(
                    rule="MISSING_LINK_COLUMN",
                    severity=SEVERITY_ERROR,
                    message=(
                        "比价表缺少【购买链接】列。规则 2.3.1 严禁删除该列，"
                        "链接必须作为一列存在，不能整列省略。"
                    ),
                    line=table.start_line,
                    snippet=row_snippet(table.header),
                )
            )
            continue
        violations.extend(check_link_column(table))

    violations.extend(check_outside_links(lines, table_lines))

    if not tables:
        violations.append(
            Violation(
                rule="NO_TABLE",
                severity=SEVERITY_WARNING,
                message=(
                    "未检测到任何 Markdown 表格。若本次回答包含商品比价结论，"
                    "必须用表格呈现并把链接写在表格内。"
                ),
            )
        )
    elif not any(table.is_price_table for table in tables):
        violations.append(
            Violation(
                rule="NO_PRICE_TABLE",
                severity=SEVERITY_WARNING,
                message=(
                    "检测到表格，但没有「平台 + 价格」列的比价表，无法校验链接列。"
                    "请确认商品是否按 references/answer-rules.md 的模板渲染。"
                ),
            )
        )

    errors = [v for v in violations if v.severity == SEVERITY_ERROR]
    warnings = [v for v in violations if v.severity == SEVERITY_WARNING]
    failed = bool(errors) or (strict and bool(warnings))

    return {
        "status": "FAIL" if failed else "PASS",
        "counts": {"errors": len(errors), "warnings": len(warnings)},
        "tables": [
            {
                "header_line": table.start_line,
                "columns": len(table.header),
                "rows": len(table.rows),
                "is_price_table": table.is_price_table,
                "link_column": (
                    strip_markup(table.header[table.link_column])
                    if table.link_column is not None
                    else None
                ),
            }
            for table in tables
        ],
        "violations": [asdict(v) for v in violations],
        "instruction": build_instruction(errors) if failed else "",
    }


def build_instruction(errors: list[Violation]) -> str:
    """给宿主 AI 的重写指令：逐条列出必须改的点，并明确重写要求。"""
    lines = [
        f"回答未通过交付前自检，共 {len(errors)} 项必须修正。购买链接必须写在表格里：",
        "1) 表格保留【购买链接】列，不得删除；",
        "2) 每个商品那一行的链接单元格填 `[查看详情](https://真实链接)`，不得留空、"
        "写占位符或直接粘长链接；",
        "3) 严禁把链接、淘口令单独列在表格下方或回答末尾——表格外的链接一律要删掉并"
        "填回对应行。",
        "请重新生成完整回答（保留原有价格/规格/专票等数据与数据来源声明），逐条修正：",
    ]
    for index, violation in enumerate(errors, start=1):
        where = f"第 {violation.line} 行" if violation.line else "整表"
        lines.append(f"  {index}. [{where}] {violation.message}")
    return "\n".join(lines)


def read_input(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.text is not None:
        return args.text, None
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(), None
        except OSError as exc:
            return None, f"无法读取文件 {args.file}：{exc}"
    data = sys.stdin.read()
    if not data.strip():
        return None, (
            "未提供回答内容：请用 --file 指定文件、--text 直接传入，"
            "或从 stdin 管道输入（cat answer.md | check_answer.py）。"
        )
    return data, None


def fix_stdout() -> None:
    """修复 Windows GBK 控制台下输出中文的 UnicodeEncodeError。"""
    encoding = (sys.stdout.encoding or "").upper()
    if encoding != "UTF-8":
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_answer.py",
        description="办公设备易采买 —— 交付前答案自检（购买链接是否写在表格内）",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="回答原文所在的 Markdown 文件")
    source.add_argument("--text", help="直接传入回答原文")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把 warning 也当作必须修正（默认只有 error 会导致 FAIL）",
    )
    return parser


def main() -> int:
    fix_stdout()
    args = build_parser().parse_args()

    text, error = read_input(args)
    if error:
        print(
            json.dumps(
                {"status": "INPUT_ERROR", "message": error},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    report = analyze(text or "", strict=args.strict)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
