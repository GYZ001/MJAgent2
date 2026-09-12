"""CSV 批量导入：纯解析层，零 db 依赖（``app/LAYERS.toml::app.provisioning.csv_parse`` = 1）。

EP-03 §4/§7 明确要求「1000 行样例走内存，不碰库」——本文件因此只做三件事：
编码探测解码、表头/行数结构校验、批次内 username 重复检测。凡是需要查库才能
判定的事（团队/角色是否存在、tier 是否合法、用户是否已存在）一律留给
``app.provisioning.importer``（L2），本文件不 import ``app.db``、不 import
任何 L2 及以上模块。

编码探测顺序 UTF-8-SIG → UTF-8 → GBK：国内 Excel 默认导出 GBK，Windows
记事本另存 UTF-8 常见 BOM。探测不出三种编码都解不出的文件时**明确报错**，
不做「猜一个凑合用」的兜底（CLAUDE.md「不得兜底填充」同一条精神）。

同一批次内出现重复 ``username``：**全部**沾边的行都标记成 error，不是只留
第二次出现的那一行——谁先谁后本身就是没有权威答案的问题，「后写覆盖先写」
或「先到先得」都是编造出来的规则（EP-03 §9 已知陷阱 1）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace

MAX_ROWS = 1000
REQUIRED_HEADER_COLUMN = "username"
KNOWN_COLUMNS = (
    "username", "display_name", "email", "employee_no", "team", "role", "tier_or_quota_plan",
)
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk")


class CsvParseError(ValueError):
    """批次级致命错误：编码探测失败、缺表头、超过单批行数上限。

    不区分具体是哪一行——这类错误发生时整份文件都没法可靠地按行解释，
    与「某一行数据有问题」的行级 error（见 ``ParsedRow.error``）是两个维度。
    """


@dataclass(frozen=True)
class ParsedRow:
    """解析出的一行，字段全部是裁剪后的原始字符串（未经业务校验）。

    ``line_no`` 从 2 开始：表头占第 1 行，与用户在 Excel 里看到的行号一致，
    报告里定位问题行时不需要再加一次偏移。
    """

    line_no: int
    username: str
    display_name: str
    email: str
    employee_no: str
    team: str
    role: str
    tier_or_quota_plan: str
    error: str | None = None
    error_column: str | None = None


@dataclass(frozen=True)
class ParseResult:
    encoding: str
    rows: list[ParsedRow]


def detect_and_decode(raw: bytes) -> tuple[str, str]:
    """按 UTF-8-SIG → UTF-8 → GBK 顺序尝试解码；三种都失败则明确报错，不猜。"""
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise CsvParseError(
        "无法识别文件编码（已尝试 UTF-8 / UTF-8-BOM / GBK），"
        "请在 Excel 中另存为「CSV UTF-8」或「CSV（逗号分隔）」后重新上传"
    )


def parse_csv(raw: bytes) -> ParseResult:
    """解码 + 表头/行数校验 + 逐行结构化 + 批次内重复 username 检测。

    表头顺序无关、多余列忽略（``csv.DictReader`` 天然支持）；缺失
    ``username`` 列或行数超过 ``MAX_ROWS`` 时整份文件拒绝，不做分片重试。
    """
    text, encoding = detect_and_decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvParseError("CSV 文件缺少表头，无法确定各列含义")
    header = {(name or "").strip() for name in reader.fieldnames}
    if REQUIRED_HEADER_COLUMN not in header:
        raise CsvParseError(f"表头缺少必需列：{REQUIRED_HEADER_COLUMN}")

    raw_rows = list(reader)
    if len(raw_rows) > MAX_ROWS:
        raise CsvParseError(
            f"单批最多 {MAX_ROWS} 行，本次上传 {len(raw_rows)} 行，请拆分后分批上传"
        )

    rows = [_to_parsed_row(line_no, entry) for line_no, entry in enumerate(raw_rows, start=2)]
    _mark_duplicate_usernames(rows)
    return ParseResult(encoding=encoding, rows=rows)


def _field(entry: dict, key: str) -> str:
    value = entry.get(key)
    return (value or "").strip()


def _to_parsed_row(line_no: int, entry: dict) -> ParsedRow:
    username = _field(entry, "username")
    row = ParsedRow(
        line_no=line_no, username=username,
        display_name=_field(entry, "display_name"), email=_field(entry, "email"),
        employee_no=_field(entry, "employee_no"), team=_field(entry, "team"),
        role=_field(entry, "role"), tier_or_quota_plan=_field(entry, "tier_or_quota_plan"),
    )
    if not username:
        return replace(row, error="用户名不能为空", error_column="username")
    return row


def _mark_duplicate_usernames(rows: list[ParsedRow]) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        if row.username:
            counts[row.username] = counts.get(row.username, 0) + 1
    for idx, row in enumerate(rows):
        if row.error is None and row.username and counts[row.username] > 1:
            rows[idx] = replace(
                row, error=f"同一批次中用户名重复：{row.username}", error_column="username",
            )
