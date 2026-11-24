"""核心业务逻辑：旧卷核验签发、占用份数、分片替换保护。

不变量（任何已提交状态都成立）：
    对每一种整行 key，全部分片份数之和 >= 全部已签回执占用份数之和。

提交旧卷在单个 BEGIN IMMEDIATE 事务内完成“读旧卷绑定 -> 汇总未占用份数
-> 写回执/明细 -> 增加占用 -> 更新卷状态”，失败整体回滚。

大卷与一致性：
- 可用性汇总按连接的 SQL 变量上限自动分批（旧版 SQLite 上限仅 999，新版
  250000），批次数再多也只查本次涉及的整行键、且都在同一事务快照内，
  所有份数来自同一个已提交状态，不会因参数超限返回 500，也不会漏行；
- 只读核对（/check、/availability、详情）包在 read_tx 快照事务内，卷状态、
  回执号与逐行余量取自同一已提交状态；核对期间另一连接仍可提交补签。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .csvio import CsvSummary, Row, summarize
from .db import immediate_tx, read_tx
from .errors import NotFoundError, ShardCoverageConflict, VolumeContentMismatch


# 分批查询时为 SQL 里的固定参数预留的余量，避免贴边触发变量上限。
_SQL_VAR_RESERVE = 16
# 每个整行键在 VALUES 子句中占用的绑定参数数（instrument, ts, level）。
_PARAMS_PER_KEY = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_receipt_id() -> str:
    return "RCP-" + uuid.uuid4().hex[:16].upper()


def _clean_id(value: str, field: str) -> str:
    if value is None:
        raise NotFoundError(f"{field} 不能为空")
    value = value.strip()
    if not value:
        raise NotFoundError(f"{field} 不能为空")
    if len(value) > 128:
        value = value[:128]
    return value


# --------------------------------------------------------------------------- #
# 频次查询
# --------------------------------------------------------------------------- #


def _variable_limit(conn: sqlite3.Connection) -> int:
    """当前连接允许的单条 SQL 绑定参数上限（随 SQLite 版本/编译选项变化）。"""

    return int(conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))


def _keys_per_batch(conn: sqlite3.Connection) -> int:
    """单批最多放多少个整行键，保证 3*N + 固定余量不超过参数上限。"""

    limit = _variable_limit(conn)
    capacity = max(1, (limit - _SQL_VAR_RESERVE) // _PARAMS_PER_KEY)
    return capacity


def _fetch_availability_chunk(
    conn: sqlite3.Connection, keys: list[Row]
) -> dict[Row, dict[str, int]]:
    """对一批整行键做一条 CTE 汇总查询；调用方负责保证批大小不超参数上限。"""

    values_sql = ",".join("(?,?,?)" for _ in keys)
    params: list[Any] = []
    for key in keys:
        params.extend((key.instrument, key.ts, key.level))

    sql = f"""
    WITH k(instrument, ts, level) AS (VALUES {values_sql})
    SELECT k.instrument AS instrument, k.ts AS ts, k.level AS level,
           COALESCE((
               SELECT SUM(sr.copies) FROM shard_rows sr
               WHERE sr.instrument = k.instrument
                 AND sr.ts = k.ts
                 AND sr.level = k.level
           ), 0) AS total_copies,
           COALESCE((
               SELECT o.occupied FROM occupancy o
               WHERE o.instrument = k.instrument
                 AND o.ts = k.ts
                 AND o.level = k.level
           ), 0) AS occupied_copies
    FROM k
    """

    result: dict[Row, dict[str, int]] = {}
    for rec in conn.execute(sql, params):
        key = Row(rec["instrument"], rec["ts"], rec["level"])
        total = int(rec["total_copies"])
        occupied = int(rec["occupied_copies"])
        result[key] = {
            "total": total,
            "occupied": occupied,
            "available": total - occupied,
        }
    return result


def fetch_availability(conn: sqlite3.Connection, keys: Iterable[Row]) -> dict[Row, dict[str, int]]:
    """按本次涉及的整行键，汇总新仓总份数与已占用份数。

    只查传入的键（走 instrument/ts/level 索引），不扫全表。键的数量可能超过
    单条 SQL 的绑定参数上限（旧版 SQLite 仅 999，新版默认 250000），因此按
    连接的实际上限自动拆成多条查询；所有批次都在调用方开启的事务内执行，
    读到的是同一个已提交快照，拼起来仍是一致状态。
    """

    # 去重：同一整行键只需查一次，同时减少参数占用。
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return {}

    batch_size = _keys_per_batch(conn)
    result: dict[Row, dict[str, int]] = {}
    for start in range(0, len(unique_keys), batch_size):
        chunk = unique_keys[start : start + batch_size]
        result.update(_fetch_availability_chunk(conn, chunk))
    return result


def build_check_items(
    summary: CsvSummary | None,
    freq: dict[Row, int],
    availability: dict[Row, dict[str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    shortages: list[dict[str, Any]] = []
    for key in sorted(freq):
        required = freq[key]
        avail = availability.get(key, {"total": 0, "occupied": 0, "available": 0})
        available = int(avail["available"])
        short = max(0, required - available)
        item = {
            **key.as_dict(),
            "required": required,
            "need": required,
            "copies": required,
            "total": avail["total"],
            "in_new_repo": avail["total"],
            "occupied": avail["occupied"],
            "reserved_by_receipts": avail["occupied"],
            "available": available,
            "unoccupied": available,
            "short": short,
            "missing": short,
            "enough": short == 0,
        }
        items.append(item)
        if short > 0:
            shortages.append(item)
    return items, shortages


def _receipt_allocations(conn: sqlite3.Connection, key: Row) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rr.receipt_id AS receipt_id,
               rr.copies AS copies,
               r.volume_id AS volume_id,
               r.issued_at AS issued_at
        FROM receipt_rows rr
        JOIN receipts r ON r.receipt_id = rr.receipt_id
        WHERE rr.instrument = ? AND rr.ts = ? AND rr.level = ?
        ORDER BY rr.receipt_id
        """,
        (key.instrument, key.ts, key.level),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 回执
# --------------------------------------------------------------------------- #


def _receipt_dict(conn: sqlite3.Connection, receipt: sqlite3.Row) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT instrument, ts, level, copies
        FROM receipt_rows
        WHERE receipt_id = ?
        ORDER BY instrument, ts, level
        """,
        (receipt["receipt_id"],),
    ).fetchall()
    item_rows: list[dict[str, Any]] = []
    for r in rows:
        key = Row(r["instrument"], r["ts"], r["level"])
        item_rows.append({**key.as_dict(), "copies": r["copies"], "required": r["copies"]})

    return {
        "id": receipt["receipt_id"],
        "receipt_id": receipt["receipt_id"],
        "receipt_no": receipt["receipt_id"],
        "volume_id": receipt["volume_id"],
        "receipt_type": receipt["receipt_kind"],
        "kind": "clean_return",
        "receipt_kind_cn": "清退回执",
        "status": "issued",
        "sufficient": True,
        "issued_at": receipt["issued_at"],
        "created_at": receipt["issued_at"],
        "content_hash": receipt["content_hash"],
        "physical_rows": receipt["physical_rows"],
        "total_copies": receipt["total_copies"],
        "rows": item_rows,
    }


def _issue_receipt(
    conn: sqlite3.Connection,
    *,
    volume_id: str,
    content_hash: str,
    physical_rows: int,
    freq: dict[Row, int],
    at: str,
) -> dict[str, Any]:
    receipt_id = new_receipt_id()
    total_copies = sum(freq.values())

    conn.execute(
        """
        INSERT INTO receipts(receipt_id, volume_id, receipt_kind, content_hash,
                             physical_rows, total_copies, issued_at)
        VALUES (?, ?, 'clean_return', ?, ?, ?, ?)
        """,
        (receipt_id, volume_id, content_hash, physical_rows, total_copies, at),
    )
    conn.executemany(
        """
        INSERT INTO receipt_rows(receipt_id, instrument, ts, level, copies)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (receipt_id, key.instrument, key.ts, key.level, copies)
            for key, copies in freq.items()
        ],
    )
    # 占用份数：在同一事务内增量维护。
    conn.executemany(
        """
        INSERT INTO occupancy(instrument, ts, level, occupied)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(instrument, ts, level) DO UPDATE SET
            occupied = occupancy.occupied + excluded.occupied
        """,
        [
            (key.instrument, key.ts, key.level, copies)
            for key, copies in freq.items()
        ],
    )
    conn.execute(
        """
        UPDATE volumes
        SET status = 'issued', receipt_id = ?, last_checked_at = ?
        WHERE volume_id = ?
        """,
        (receipt_id, at, volume_id),
    )

    receipt = conn.execute(
        "SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,)
    ).fetchone()
    return _receipt_dict(conn, receipt)


def _insert_volume_rows(conn: sqlite3.Connection, volume_id: str, freq: dict[Row, int]) -> None:
    conn.executemany(
        """
        INSERT INTO volume_rows(volume_id, instrument, ts, level, copies)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (volume_id, key.instrument, key.ts, key.level, copies)
            for key, copies in freq.items()
        ],
    )


def _load_volume_freq(conn: sqlite3.Connection, volume_id: str) -> dict[Row, int]:
    records = conn.execute(
        "SELECT instrument, ts, level, copies FROM volume_rows WHERE volume_id = ?",
        (volume_id,),
    ).fetchall()
    return {
        Row(r["instrument"], r["ts"], r["level"]): int(r["copies"])
        for r in records
    }


# --------------------------------------------------------------------------- #
# 旧卷提交
# --------------------------------------------------------------------------- #


def submit_volume(
    conn: sqlite3.Connection,
    volume_id: str,
    text: str,
    *,
    header_mode: str = "auto",
) -> dict[str, Any]:
    volume_id = _clean_id(volume_id, "volume_id")
    summary = summarize(text, header_mode=header_mode)
    at = now_iso()

    with immediate_tx(conn):
        volume = conn.execute(
            "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()

        # 同卷内容绑定：不同内容一律拒绝（事务内，尚未做任何改动）。
        if volume is not None and volume["content_hash"] != summary.content_hash:
            raise VolumeContentMismatch(
                f"卷号 {volume_id!r} 已绑定另一份内容；同卷只接受原样重送，内容不同已拒绝",
                details={
                    "volume_id": volume_id,
                    "stored_hash": volume["content_hash"],
                    "submitted_hash": summary.content_hash,
                    "stored_status": volume["status"],
                    "receipt_id": volume["receipt_id"],
                },
            )

        # 已签发：原样重送 -> 原回执，直接返回，不再核对/占用。
        if volume is not None and volume["receipt_id"]:
            receipt = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (volume["receipt_id"],),
            ).fetchone()
            body = _receipt_dict(conn, receipt)
            body.update(
                outcome="already_issued",
                idempotent_replay=True,
                replay=True,
                message="同卷原样重送：返回原清退回执，不重复占用份数",
            )
            return body

        availability = fetch_availability(conn, summary.freq.keys())
        items, shortages = build_check_items(summary, summary.freq, availability)

        if volume is None:
            conn.execute(
                """
                INSERT INTO volumes(volume_id, content_text, content_hash, header_skipped,
                                    physical_rows, distinct_rows, status, receipt_id,
                                    first_submitted_at, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, 'short', NULL, ?, ?)
                """,
                (
                    volume_id,
                    text or "",
                    summary.content_hash,
                    1 if summary.header_skipped else 0,
                    summary.physical_rows,
                    summary.distinct_rows,
                    at,
                    at,
                ),
            )
            _insert_volume_rows(conn, volume_id, summary.freq)
        else:
            conn.execute(
                "UPDATE volumes SET last_checked_at = ? WHERE volume_id = ?",
                (at, volume_id),
            )

        if shortages:
            missing_copies = sum(int(item["short"]) for item in shortages)
            return {
                "outcome": "insufficient",
                "status": "insufficient",
                "sufficient": False,
                "volume_id": volume_id,
                "content_hash": summary.content_hash,
                "header_skipped": summary.header_skipped,
                "physical_rows": summary.physical_rows,
                "total_copies": summary.physical_rows,
                "distinct_rows": summary.distinct_rows,
                "checked_at": at,
                "shortage_kinds": len(shortages),
                "shortage_copies": missing_copies,
                "missing_copies": missing_copies,
                "rows": items,
                "shortages": shortages,
                "message": (
                    f"有 {len(shortages)} 种整行的新仓未占用副本不足，"
                    f"共缺 {missing_copies} 份；未签发回执、未占用份数"
                ),
            }

        receipt = _issue_receipt(
            conn,
            volume_id=volume_id,
            content_hash=summary.content_hash,
            physical_rows=summary.physical_rows,
            freq=summary.freq,
            at=at,
        )
        receipt.update(
            outcome="issued",
            idempotent_replay=False,
            replay=False,
            header_skipped=summary.header_skipped,
            message="核验通过：新仓未占用副本足够，已签发清退回执并占用对应份数",
        )
        return receipt


def check_stored_volume(conn: sqlite3.Connection, volume_id: str) -> dict[str, Any]:
    """只读：用已保存的旧卷明细核对当前未占用份数，绝不签发或写入。

    卷状态/回执号与逐行份数在同一个延迟读事务（固定快照）内读取，因此即使
    核对期间另一连接提交了补签，本响应的所有字段仍来自同一个已提交状态，
    不会出现“状态显示未签、余量却已扣减”的跨状态拼接。
    """

    volume_id = _clean_id(volume_id, "volume_id")
    with read_tx(conn):
        volume = conn.execute(
            "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if volume is None:
            raise NotFoundError(
                f"找不到旧卷 {volume_id!r}，请先提交 CSV",
                details={"volume_id": volume_id},
            )

        freq = _load_volume_freq(conn, volume_id)
        availability = fetch_availability(conn, freq.keys())
        items, shortages = build_check_items(None, freq, availability)
        return {
            "volume_id": volume_id,
            "stored_status": volume["status"],
            "status": volume["status"],
            "receipt_id": volume["receipt_id"],
            "sufficient": not shortages,
            "shortage_kinds": len(shortages),
            "shortages": shortages,
            "rows": items,
            "content_hash": volume["content_hash"],
        }


def recheck_volume(conn: sqlite3.Connection, volume_id: str) -> dict[str, Any]:
    """用已保存的旧卷明细重新核验（分片补副本后可由此签发）。"""

    volume_id = _clean_id(volume_id, "volume_id")
    at = now_iso()

    with immediate_tx(conn):
        volume = conn.execute(
            "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if volume is None:
            raise NotFoundError(
                f"找不到旧卷 {volume_id!r}，请先用 /volumes/{volume_id}/submit 提交 CSV",
                details={"volume_id": volume_id},
            )

        if volume["receipt_id"]:
            receipt = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (volume["receipt_id"],),
            ).fetchone()
            body = _receipt_dict(conn, receipt)
            body.update(
                outcome="already_issued",
                idempotent_replay=True,
                message="该卷已签发回执，返回原回执",
            )
            return body

        freq = _load_volume_freq(conn, volume_id)
        availability = fetch_availability(conn, freq.keys())
        items, shortages = build_check_items(None, freq, availability)

        if shortages:
            conn.execute(
                "UPDATE volumes SET last_checked_at = ? WHERE volume_id = ?",
                (at, volume_id),
            )
            missing_copies = sum(int(item["short"]) for item in shortages)
            return {
                "outcome": "insufficient",
                "status": "insufficient",
                "sufficient": False,
                "volume_id": volume_id,
                "content_hash": volume["content_hash"],
                "checked_at": at,
                "rows": items,
                "shortages": shortages,
                "shortage_kinds": len(shortages),
                "shortage_copies": missing_copies,
                "missing_copies": missing_copies,
                "message": f"重新核验仍不足：{len(shortages)} 种整行短缺，共缺 {missing_copies} 份",
            }

        receipt = _issue_receipt(
            conn,
            volume_id=volume_id,
            content_hash=volume["content_hash"],
            physical_rows=volume["physical_rows"],
            freq=freq,
            at=at,
        )
        receipt.update(
            outcome="issued",
            idempotent_replay=False,
            message="重新核验通过，已签发清退回执并占用对应份数",
        )
        return receipt


# --------------------------------------------------------------------------- #
# 新仓分片
# --------------------------------------------------------------------------- #


def _shard_freq(conn: sqlite3.Connection, shard_id: str) -> dict[Row, int]:
    records = conn.execute(
        "SELECT instrument, ts, level, copies FROM shard_rows WHERE shard_id = ?",
        (shard_id,),
    ).fetchall()
    return {
        Row(r["instrument"], r["ts"], r["level"]): int(r["copies"])
        for r in records
    }


def replace_shard(
    conn: sqlite3.Connection,
    shard_id: str,
    text: str,
    *,
    header_mode: str = "auto",
) -> dict[str, Any]:
    """按片号整片替换（不存在则创建）。

    替换后每种整行的全仓总份数仍须 >= 全部回执占用份数，否则
    ShardCoverageConflict 回滚，旧片保持不变。
    """

    shard_id = _clean_id(shard_id, "shard_id")
    summary = summarize(text if text is not None else "", header_mode=header_mode)
    at = now_iso()

    with immediate_tx(conn):
        old = conn.execute(
            "SELECT * FROM shards WHERE shard_id = ?", (shard_id,)
        ).fetchone()
        old_freq = _shard_freq(conn, shard_id) if old is not None else {}

        keys = set(old_freq) | set(summary.freq)
        availability = fetch_availability(conn, keys)

        violations: list[dict[str, Any]] = []
        for key in sorted(keys):
            old_copies = old_freq.get(key, 0)
            new_copies = summary.freq.get(key, 0)
            current_total = availability.get(key, {"total": 0})["total"]
            occupied = availability.get(key, {"occupied": 0})["occupied"]
            total_after = current_total - old_copies + new_copies
            if total_after < occupied:
                violations.append(
                    {
                        **key.as_dict(),
                        # 与短缺清单对齐的别名字段：这里“需求”就是回执要求保住的份数。
                        "required": occupied,
                        "need": occupied,
                        "old_shard_copies": old_copies,
                        "new_shard_copies": new_copies,
                        "current_total": current_total,
                        "total": total_after,
                        "total_after_replace": total_after,
                        "occupied": occupied,
                        "reserved_by_receipts": occupied,
                        "required_by_receipts": occupied,
                        "short": occupied - total_after,
                        "missing": occupied - total_after,
                        "enough": False,
                        "receipts": _receipt_allocations(conn, key),
                    }
                )

        if violations:
            raise ShardCoverageConflict(
                f"分片 {shard_id!r} 的替换内容会使 {len(violations)} 种整行无法覆盖"
                "全部已签回执所需份数，已拒绝；旧分片原样保留",
                details={
                    "shard_id": shard_id,
                    "violations": violations,
                    "violation_kinds": len(violations),
                },
            )

        if old is None:
            conn.execute(
                """
                INSERT INTO shards(shard_id, content_text, content_hash, header_skipped,
                                   physical_rows, distinct_rows, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shard_id,
                    text or "",
                    summary.content_hash,
                    1 if summary.header_skipped else 0,
                    summary.physical_rows,
                    summary.distinct_rows,
                    at,
                    at,
                ),
            )
            created = True
        else:
            conn.execute(
                """
                UPDATE shards
                SET content_text = ?, content_hash = ?, header_skipped = ?,
                    physical_rows = ?, distinct_rows = ?, updated_at = ?
                WHERE shard_id = ?
                """,
                (
                    text or "",
                    summary.content_hash,
                    1 if summary.header_skipped else 0,
                    summary.physical_rows,
                    summary.distinct_rows,
                    at,
                    shard_id,
                ),
            )
            created = False

        conn.execute("DELETE FROM shard_rows WHERE shard_id = ?", (shard_id,))
        conn.executemany(
            """
            INSERT INTO shard_rows(shard_id, instrument, ts, level, copies)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (shard_id, key.instrument, key.ts, key.level, copies)
                for key, copies in summary.freq.items()
            ],
        )

        return {
            "success": True,
            "outcome": "created" if created else "replaced",
            "created": created,
            "replaced": not created,
            "shard_id": shard_id,
            "content_hash": summary.content_hash,
            "header_skipped": summary.header_skipped,
            "physical_rows": summary.physical_rows,
            "total_copies": summary.physical_rows,
            "distinct_rows": summary.distinct_rows,
            "updated_at": at,
            "message": ("分片已创建" if created else "分片已整片替换")
            + "，全部已签回执所需份数仍然有着落",
        }


def delete_shard(conn: sqlite3.Connection, shard_id: str) -> dict[str, Any]:
    """删除整片等价于替换为空片，同样必须保住回执份数。"""

    shard_id = _clean_id(shard_id, "shard_id")
    with immediate_tx(conn):
        old = conn.execute(
            "SELECT * FROM shards WHERE shard_id = ?", (shard_id,)
        ).fetchone()
        if old is None:
            raise NotFoundError(
                f"找不到分片 {shard_id!r}", details={"shard_id": shard_id}
            )

        old_freq = _shard_freq(conn, shard_id)
        availability = fetch_availability(conn, old_freq.keys())
        violations: list[dict[str, Any]] = []
        for key in sorted(old_freq):
            old_copies = old_freq[key]
            current_total = availability.get(key, {"total": 0})["total"]
            occupied = availability.get(key, {"occupied": 0})["occupied"]
            total_after = current_total - old_copies
            if total_after < occupied:
                violations.append(
                    {
                        **key.as_dict(),
                        "required": occupied,
                        "need": occupied,
                        "old_shard_copies": old_copies,
                        "current_total": current_total,
                        "total": total_after,
                        "total_after_delete": total_after,
                        "occupied": occupied,
                        "required_by_receipts": occupied,
                        "short": occupied - total_after,
                        "missing": occupied - total_after,
                        "receipts": _receipt_allocations(conn, key),
                    }
                )
        if violations:
            raise ShardCoverageConflict(
                f"删除分片 {shard_id!r} 会使 {len(violations)} 种整行无法覆盖回执，已拒绝",
                details={"shard_id": shard_id, "violations": violations},
            )

        conn.execute("DELETE FROM shards WHERE shard_id = ?", (shard_id,))
        return {
            "success": True,
            "deleted": True,
            "shard_id": shard_id,
            "message": "分片已删除",
        }


# --------------------------------------------------------------------------- #
# 只读查询
# --------------------------------------------------------------------------- #


def _freq_item(rec: sqlite3.Row) -> dict[str, Any]:
    key = Row(rec["instrument"], rec["ts"], rec["level"])
    copies = int(rec["copies"])
    return {**key.as_dict(), "copies": copies, "required": copies}


def list_shards(conn: sqlite3.Connection) -> dict[str, Any]:
    records = conn.execute(
        """
        SELECT s.*, COALESCE((
            SELECT SUM(sr.copies) FROM shard_rows sr WHERE sr.shard_id = s.shard_id
        ), 0) AS total_copies
        FROM shards s
        ORDER BY s.shard_id
        """
    ).fetchall()
    items = []
    for r in records:
        items.append(
            {
                "shard_id": r["shard_id"],
                "content_hash": r["content_hash"],
                "header_skipped": bool(r["header_skipped"]),
                "physical_rows": r["physical_rows"],
                "distinct_rows": r["distinct_rows"],
                "total_copies": r["total_copies"],
                "row_count": r["physical_rows"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    return {"shards": items, "count": len(items)}


def get_shard(conn: sqlite3.Connection, shard_id: str) -> dict[str, Any]:
    shard_id = _clean_id(shard_id, "shard_id")
    with read_tx(conn):
        rec = conn.execute(
            "SELECT * FROM shards WHERE shard_id = ?", (shard_id,)
        ).fetchone()
        if rec is None:
            raise NotFoundError(f"找不到分片 {shard_id!r}", details={"shard_id": shard_id})
        rows = conn.execute(
            """
            SELECT instrument, ts, level, copies FROM shard_rows
            WHERE shard_id = ? ORDER BY instrument, ts, level
            """,
            (shard_id,),
        ).fetchall()
        freq_items = [_freq_item(r) for r in rows]
        return {
            "shard_id": shard_id,
            "content_hash": rec["content_hash"],
            "header_skipped": bool(rec["header_skipped"]),
            "physical_rows": rec["physical_rows"],
            "distinct_rows": rec["distinct_rows"],
            "total_copies": sum(int(r["copies"]) for r in rows),
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
            "csv": rec["content_text"],
            "content": rec["content_text"],
            "rows": freq_items,
        }


def list_volumes(conn: sqlite3.Connection) -> dict[str, Any]:
    records = conn.execute(
        "SELECT * FROM volumes ORDER BY first_submitted_at, volume_id"
    ).fetchall()
    items = [
        {
            "volume_id": r["volume_id"],
            "status": r["status"],
            "content_hash": r["content_hash"],
            "physical_rows": r["physical_rows"],
            "distinct_rows": r["distinct_rows"],
            "receipt_id": r["receipt_id"],
            "first_submitted_at": r["first_submitted_at"],
            "last_checked_at": r["last_checked_at"],
        }
        for r in records
    ]
    return {"volumes": items, "count": len(items)}


def get_volume(conn: sqlite3.Connection, volume_id: str) -> dict[str, Any]:
    volume_id = _clean_id(volume_id, "volume_id")
    with read_tx(conn):
        rec = conn.execute(
            "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if rec is None:
            raise NotFoundError(
                f"找不到旧卷 {volume_id!r}", details={"volume_id": volume_id}
            )
        rows = conn.execute(
            """
            SELECT instrument, ts, level, copies FROM volume_rows
            WHERE volume_id = ? ORDER BY instrument, ts, level
            """,
            (volume_id,),
        ).fetchall()
        body: dict[str, Any] = {
            "volume_id": volume_id,
            "status": rec["status"],
            "content_hash": rec["content_hash"],
            "header_skipped": bool(rec["header_skipped"]),
            "physical_rows": rec["physical_rows"],
            "distinct_rows": rec["distinct_rows"],
            "receipt_id": rec["receipt_id"],
            "first_submitted_at": rec["first_submitted_at"],
            "last_checked_at": rec["last_checked_at"],
            "csv": rec["content_text"],
            "content": rec["content_text"],
            "rows": [_freq_item(r) for r in rows],
        }
        if rec["receipt_id"]:
            receipt = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?", (rec["receipt_id"],)
            ).fetchone()
            if receipt is not None:
                body["receipt"] = _receipt_dict(conn, receipt)
        return body


def list_receipts(conn: sqlite3.Connection) -> dict[str, Any]:
    records = conn.execute(
        "SELECT * FROM receipts ORDER BY issued_at, receipt_id"
    ).fetchall()
    items = [
        {
            "receipt_id": r["receipt_id"],
            "id": r["receipt_id"],
            "receipt_no": r["receipt_id"],
            "volume_id": r["volume_id"],
            "issued_at": r["issued_at"],
            "total_copies": r["total_copies"],
            "physical_rows": r["physical_rows"],
            "content_hash": r["content_hash"],
        }
        for r in records
    ]
    return {"receipts": items, "count": len(items)}


def get_receipt(conn: sqlite3.Connection, receipt_id: str) -> dict[str, Any]:
    with read_tx(conn):
        rec = conn.execute(
            "SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if rec is None:
            raise NotFoundError(
                f"找不到回执 {receipt_id!r}", details={"receipt_id": receipt_id}
            )
        return _receipt_dict(conn, rec)


def get_receipt_by_volume(conn: sqlite3.Connection, volume_id: str) -> dict[str, Any]:
    volume_id = _clean_id(volume_id, "volume_id")
    with read_tx(conn):
        rec = conn.execute(
            "SELECT receipt_id FROM volumes WHERE volume_id = ?", (volume_id,)
        ).fetchone()
        if rec is None:
            raise NotFoundError(
                f"找不到旧卷 {volume_id!r}", details={"volume_id": volume_id}
            )
        if not rec["receipt_id"]:
            raise NotFoundError(
                f"旧卷 {volume_id!r} 尚未取得回执（可能副本不足）",
                details={"volume_id": volume_id, "receipt_id": None},
            )
        receipt = conn.execute(
            "SELECT * FROM receipts WHERE receipt_id = ?", (rec["receipt_id"],)
        ).fetchone()
        return _receipt_dict(conn, receipt)


def check_availability(
    conn: sqlite3.Connection, text: str, *, header_mode: str = "auto"
) -> dict[str, Any]:
    summary = summarize(text if text is not None else "", header_mode=header_mode)
    # 分批查询在同一快照事务内完成，逐行 total/occupied/available 状态一致。
    with read_tx(conn):
        availability = fetch_availability(conn, summary.freq.keys())
        items, shortages = build_check_items(summary, summary.freq, availability)
    return {
        "success": True,
        "sufficient": not shortages,
        "all_available": not shortages,
        "shortage_kinds": len(shortages),
        "shortages": shortages,
        "rows": items,
        "content_hash": summary.content_hash,
        "physical_rows": summary.physical_rows,
        "distinct_rows": summary.distinct_rows,
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "shards": scalar("SELECT COUNT(*) FROM shards"),
        "volumes": scalar("SELECT COUNT(*) FROM volumes"),
        "issued_volumes": scalar("SELECT COUNT(*) FROM volumes WHERE status='issued'"),
        "short_volumes": scalar("SELECT COUNT(*) FROM volumes WHERE status='short'"),
        "receipts": scalar("SELECT COUNT(*) FROM receipts"),
        "shard_copy_rows": scalar("SELECT COALESCE(SUM(copies),0) FROM shard_rows"),
        "occupied_copies": scalar("SELECT COALESCE(SUM(occupied),0) FROM occupancy"),
        "receipt_copies": scalar("SELECT COALESCE(SUM(copies),0) FROM receipt_rows"),
    }


def reset_all(conn: sqlite3.Connection) -> dict[str, Any]:
    with immediate_tx(conn):
        conn.execute("DELETE FROM receipt_rows")
        conn.execute("DELETE FROM receipts")
        conn.execute("DELETE FROM occupancy")
        conn.execute("DELETE FROM volume_rows")
        conn.execute("DELETE FROM volumes")
        conn.execute("DELETE FROM shard_rows")
        conn.execute("DELETE FROM shards")
    return {"success": True, "reset": True}
