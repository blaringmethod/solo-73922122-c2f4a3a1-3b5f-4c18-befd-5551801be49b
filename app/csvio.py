"""CSV 解析与“整行多重集”语义。

CSV 每行三列：``仪器号, UTC整数秒, 十分之一分贝整数读数``。
完全相同的整行按多份副本计，因此一卷/一片的内容就是整行的多重集。

设计要点：
- 仪器号按去除首尾空白后的原样字符串处理（保留 ``007`` 这类前导零）；
- 时间戳与读数必须是整数（读数单位为 0.1 dB，例如 ``612`` 表示 61.2 dB）；
- 自动识别常见表头，也可用 ``?header=yes|no`` 显式指定；
- 内容哈希基于排序后的整行多重集，因此行顺序不同不算“内容不同”。
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import CsvParseError

# 用单元分隔符拼接规范键，避免仪器号中出现逗号导致歧义。
_KEY_SEP = "\x1f"

_INTEGER_RE = __import__("re").compile(r"^[+-]?[0-9]+$")


@dataclass(frozen=True, order=True)
class Row:
    """一条完整记录；三元组完全一致才是“相同行”。"""

    instrument: str
    ts: int
    level: int

    def key(self) -> str:
        return _KEY_SEP.join((self.instrument, str(self.ts), str(self.level)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "ts": self.ts,
            "utc": self.ts,
            "timestamp": self.ts,
            "level": self.level,
            "level_tenths_db": self.level,
            "db_tenths": self.level,
        }


@dataclass(frozen=True)
class CsvSummary:
    rows: tuple[Row, ...]
    freq: dict[Row, int]
    canonical: str
    content_hash: str
    physical_rows: int
    distinct_rows: int
    header_skipped: bool


def is_integer_token(value: str) -> bool:
    return bool(_INTEGER_RE.match(value.strip()))


def _normalize_header_cell(value: str) -> str:
    out = []
    for ch in value.strip().lower():
        if ch.isalnum() or "一" <= ch <= "鿿":
            out.append(ch)
    return "".join(out)


def _cell_looks_like_header(norm: str, kind: str) -> bool:
    if not norm:
        return False
    if kind == "ts":
        return (
            "utc" in norm
            or "timestamp" in norm
            or "time" in norm
            or "epoch" in norm
            or norm in {"ts", "seconds", "sec", "秒", "时间戳", "整数秒"}
            or "时间" in norm
            or "时刻" in norm
        )
    if kind == "level":
        return (
            "level" in norm
            or "db" in norm
            or "spl" in norm
            or "reading" in norm
            or norm == "value"
            or "读数" in norm
            or "分贝" in norm
            or "声级" in norm
        )
    # instrument
    return (
        "instr" in norm
        or "device" in norm
        or "meter" in norm
        or norm in {"id", "inst", "code", "仪器号", "仪器编号", "设备号", "设备编号"}
        or "仪器" in norm
        or "设备" in norm
    )


def _first_record_looks_like_header(fields: list[str]) -> bool:
    """只有在“正面证据”足够时才把首行当表头，避免吞掉错误数据行。"""
    if len(fields) < 3:
        return False
    a, b, c = fields[0], fields[1], fields[2]
    if is_integer_token(b) and is_integer_token(c):
        return False  # 两列数字都合法，按数据行处理
    norms = [_normalize_header_cell(x) for x in (a, b, c)]
    hits = sum(
        (
            _cell_looks_like_header(norms[0], "instrument"),
            _cell_looks_like_header(norms[1], "ts"),
            _cell_looks_like_header(norms[2], "level"),
        )
    )
    return hits >= 1


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(text: str, *, header_mode: str = "auto") -> tuple[list[Row], bool]:
    """解析 CSV 文本为 Row 列表（保留重复行与顺序）。

    header_mode: ``auto``（默认）| ``yes`` | ``no``。
    返回 (rows, header_skipped)。
    """

    if text is None:
        raise CsvParseError("未收到 CSV 内容")
    if text and text[0] == "\ufeff":  # UTF-8 BOM
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    records = list(reader)

    # 找到第一条非空记录以判断表头。
    first_idx: int | None = None
    for idx, record in enumerate(records):
        if record and not (len(record) == 1 and not record[0].strip()):
            first_idx = idx
            break

    skip_header_idx: int | None = None
    header_mode = (header_mode or "auto").lower()
    if first_idx is not None:
        first = [c.strip() for c in records[first_idx]]
        if header_mode in {"yes", "true", "1", "header"}:
            skip_header_idx = first_idx
        elif header_mode in {"no", "false", "0", "none"}:
            skip_header_idx = None
        elif _first_record_looks_like_header(first):
            skip_header_idx = first_idx

    rows: list[Row] = []
    issues: list[dict[str, Any]] = []

    for idx, record in enumerate(records, start=1):
        zero_based = idx - 1
        if zero_based == skip_header_idx:
            continue
        # 物理空行：csv.reader 返回 []；纯空白行返回 ['   ']。
        if not record:
            continue
        if len(record) == 1 and not record[0].strip():
            continue

        fields = [c.strip() for c in record]
        if len(fields) != 3:
            issues.append(
                {
                    "line": idx,
                    "raw": ",".join(record)[:200],
                    "message": f"每行必须恰好有 3 列（仪器号, UTC整数秒, 读数），实际 {len(fields)} 列",
                }
            )
            continue

        instrument, ts_token, level_token = fields
        line_issues: list[str] = []
        if not instrument:
            line_issues.append("仪器号不能为空")
        if not is_integer_token(ts_token):
            line_issues.append(f"UTC 秒必须是整数，收到 {ts_token!r}")
        if not is_integer_token(level_token):
            line_issues.append(
                "读数必须是十分之一分贝计的整数（如 612 表示 61.2 dB），"
                f"收到 {level_token!r}"
            )
        if line_issues:
            issues.append(
                {
                    "line": idx,
                    "raw": ",".join(record)[:200],
                    "message": "；".join(line_issues),
                }
            )
            continue

        rows.append(Row(instrument=instrument, ts=int(ts_token), level=int(level_token)))

    if issues:
        raise CsvParseError(
            f"CSV 中有 {len(issues)} 行无法解析，请按行修正后重传",
            details={"issues": issues[:50], "issue_count": len(issues)},
        )

    return rows, skip_header_idx is not None


def frequencies(rows: Iterable[Row]) -> dict[Row, int]:
    freq: dict[Row, int] = {}
    for row in rows:
        freq[row] = freq.get(row, 0) + 1
    return freq


def canonical_text(freq: dict[Row, int]) -> str:
    """排序后的整行多重集规范表示：每行一份物理记录（含重复）。"""

    lines: list[str] = []
    for row in sorted(freq):
        for _ in range(freq[row]):
            lines.append(row.key())
    return "\n".join(lines)


def hash_frequency(freq: dict[Row, int]) -> str:
    digest = hashlib.sha256(canonical_text(freq).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def summarize(text: str, *, header_mode: str = "auto") -> CsvSummary:
    rows, header_skipped = parse_csv(text, header_mode=header_mode)
    freq = frequencies(rows)
    canonical = canonical_text(freq)
    return CsvSummary(
        rows=tuple(rows),
        freq=freq,
        canonical=canonical,
        content_hash=hash_frequency(freq),
        physical_rows=len(rows),
        distinct_rows=len(freq),
        header_skipped=header_skipped,
    )


def freq_to_csv(freq: dict[Row, int], *, header: bool = True) -> str:
    """把频次多重集还原为带重复行的 CSV（用于 JSON 输入转换/导出）。"""

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if header:
        writer.writerow(["instrument", "utc", "level_tenths_db"])
    for row in sorted(freq):
        for _ in range(freq[row]):
            writer.writerow([row.instrument, row.ts, row.level])
    return buf.getvalue()


def rows_to_csv(rows: Iterable[Row], *, header: bool = False) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if header:
        writer.writerow(["instrument", "utc", "level_tenths_db"])
    for row in rows:
        writer.writerow([row.instrument, row.ts, row.level])
    return buf.getvalue()


_INSTRUMENT_KEYS = (
    "instrument",
    "instrument_id",
    "instrument_no",
    "device",
    "device_id",
    "meter",
    "inst",
    "id",
    "仪器号",
    "仪器编号",
    "设备号",
)
_TS_KEYS = (
    "ts",
    "utc",
    "timestamp",
    "utc_seconds",
    "utc_sec",
    "time",
    "epoch",
    "seconds",
    "UTC",
    "时间戳",
    "时间",
)
_LEVEL_KEYS = (
    "level",
    "level_tenths_db",
    "db_tenths",
    "tenths_db",
    "reading",
    "value",
    "spl",
    "db",
    "读数",
    "整数读数",
    "分贝",
)


def _pick(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    # 再做一次小写归一化匹配。
    lowered = {str(k).strip().lower(): v for k, v in record.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] is not None:
            return lowered[key.lower()]
    return None


def json_payload_to_csv(data: Any) -> str:
    """把宽松的 JSON 输入转换为 CSV 文本。

    支持：``{"csv": "a,b,c"}``、``{"rows": [[...], ...]}``、
    ``{"records": [{...}]}`` 或顶层数组。
    """

    if isinstance(data, str):
        return data

    raw_rows: Any = None
    embedded_csv: str | None = None

    if isinstance(data, list):
        raw_rows = data
    elif isinstance(data, dict):
        for key in ("csv", "csv_text", "content", "text", "data", "raw"):
            value = data.get(key)
            if isinstance(value, str):
                embedded_csv = value
                break
        if embedded_csv is None:
            for key in ("rows", "records", "items", "entries", "data"):
                if isinstance(data.get(key), list):
                    raw_rows = data[key]
                    break
    else:
        raise CsvParseError("JSON 请求体必须是 CSV 字符串、行数组或包含 rows 的对象")

    if embedded_csv is not None:
        return embedded_csv
    if raw_rows is None:
        raise CsvParseError(
            "JSON 请求体中没有找到 CSV：可用 {\"csv\": \"...\"}、{\"rows\": [...]} 或顶层数组"
        )

    out_rows: list[Row] = []
    for index, item in enumerate(raw_rows, start=1):
        if isinstance(item, (list, tuple)):
            if len(item) != 3:
                raise CsvParseError(f"JSON 第 {index} 行必须是 3 个元素，实际 {len(item)} 个")
            inst, ts, level = item
        elif isinstance(item, dict):
            inst = _pick(item, _INSTRUMENT_KEYS)
            ts = _pick(item, _TS_KEYS)
            level = _pick(item, _LEVEL_KEYS)
        else:
            raise CsvParseError(f"JSON 第 {index} 行必须是数组或对象")
        if inst is None or ts is None or level is None:
            raise CsvParseError(f"JSON 第 {index} 行缺少 instrument/utc/level 字段")
        out_rows.append(Row(str(inst).strip(), int(ts), int(level)))

    return rows_to_csv(out_rows)
