from __future__ import annotations

import pytest

from app.csvio import (
    Row,
    frequencies,
    json_payload_to_csv,
    parse_csv,
    summarize,
)
from app.errors import CsvParseError


def test_parse_headerless_three_columns():
    rows, header = parse_csv("SQ-1,1760000000,612\nSQ-1,1760000060,588\n")
    assert header is False
    assert rows == [
        Row("SQ-1", 1760000000, 612),
        Row("SQ-1", 1760000060, 588),
    ]


def test_parse_english_header_auto_detected():
    text = "instrument,utc,level_tenths_db\nSQ-1,1760000000,612\n"
    rows, header = parse_csv(text)
    assert header is True
    assert rows == [Row("SQ-1", 1760000000, 612)]


def test_parse_chinese_header_and_crlf_and_bom():
    text = "﻿仪器号,UTC整数秒,读数\r\nSQ-1,1760000000,-12\r\n"
    rows, header = parse_csv(text)
    assert header is True
    assert rows == [Row("SQ-1", 1760000000, -12)]


def test_whitespace_trimmed_but_leading_zeros_preserved():
    rows, _ = parse_csv(" 007 , 1760000000 , 612 \n")
    assert rows[0].instrument == "007"
    assert rows[0].ts == 1760000000
    assert rows[0].level == 612


def test_duplicate_rows_become_copies():
    summary = summarize("M,1,10\nM,1,10\nM,1,10\nM,2,20\n")
    assert summary.physical_rows == 4
    assert summary.distinct_rows == 2
    assert summary.freq[Row("M", 1, 10)] == 3
    assert summary.freq[Row("M", 2, 20)] == 1


def test_hash_is_order_independent():
    a = summarize("M,1,10\nM,2,20\n")
    b = summarize("M,2,20\nM,1,10\n")
    assert a.content_hash == b.content_hash


def test_hash_detects_any_column_difference():
    base = summarize("M,1,10\n")
    assert summarize("M,1,11\n").content_hash != base.content_hash
    assert summarize("M,2,10\n").content_hash != base.content_hash
    assert summarize("X,1,10\n").content_hash != base.content_hash
    assert summarize("M,1,10\nM,1,10\n").content_hash != base.content_hash


def test_decimal_reading_is_rejected_with_line_number():
    with pytest.raises(CsvParseError) as exc:
        parse_csv("M,1,61.2\n")
    assert exc.value.status_code == 422
    issue = exc.value.details["issues"][0]
    assert issue["line"] == 1
    assert "整数" in issue["message"]


def test_wrong_column_count_reports_line():
    with pytest.raises(CsvParseError) as exc:
        parse_csv("M,1,10,extra\n")
    assert exc.value.details["issues"][0]["line"] == 1


def test_bad_second_line_reported_but_header_skipped():
    text = "instrument,utc,level\nM,not-an-int,10\n"
    with pytest.raises(CsvParseError) as exc:
        parse_csv(text)
    assert exc.value.details["issues"][0]["line"] == 2


def test_explicit_header_no_strict_mode():
    # 显式 no 时，像表头的首行也按数据解析并报错，而不是被吞掉。
    with pytest.raises(CsvParseError):
        parse_csv("a,b,c\n", header_mode="no")


def test_empty_instrument_rejected():
    with pytest.raises(CsvParseError):
        parse_csv(",1,10\n")


def test_json_rows_array_to_csv():
    csv_text = json_payload_to_csv({"rows": [["M", 1, 10], ["M", 1, 10]]})
    rows, _ = parse_csv(csv_text)
    assert frequencies(rows)[Row("M", 1, 10)] == 2


def test_json_dict_rows_aliases():
    csv_text = json_payload_to_csv(
        [{"instrument": "M", "timestamp": 1, "level_tenths_db": 10}]
    )
    rows, _ = parse_csv(csv_text)
    assert rows == [Row("M", 1, 10)]


def test_json_embedded_csv_passthrough():
    assert json_payload_to_csv({"csv": "M,1,10\n"}) == "M,1,10\n"
