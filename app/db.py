"""SQLite 连接、表结构与事务边界。

并发正确性：
- 每个请求使用独立连接；
- 所有“核对 + 写回执 + 改占用份数 / 换分片”都在一个 ``BEGIN IMMEDIATE``
  事务内完成，SQLite 保证同一时刻只有一个写事务，后来者在 busy_timeout
  内等待，因此两个旧卷争用同一份读数时只会有一个成功；
- 事务内任何异常都会 ROLLBACK，回执、占用份数、分片保持原状。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shards (
    shard_id      TEXT PRIMARY KEY,
    content_text  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    header_skipped INTEGER NOT NULL DEFAULT 0,
    physical_rows INTEGER NOT NULL,
    distinct_rows INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shard_rows (
    shard_id   TEXT NOT NULL REFERENCES shards(shard_id) ON DELETE CASCADE,
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    copies     INTEGER NOT NULL CHECK (copies > 0),
    PRIMARY KEY (shard_id, instrument, ts, level)
) WITHOUT ROWID;

-- 以整行三列为前导的索引，支撑按行键跨分片汇总份数。
CREATE INDEX IF NOT EXISTS idx_shard_rows_key
    ON shard_rows(instrument, ts, level, copies);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id          TEXT PRIMARY KEY,
    content_text       TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    header_skipped     INTEGER NOT NULL DEFAULT 0,
    physical_rows      INTEGER NOT NULL,
    distinct_rows      INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('short', 'issued')),
    receipt_id         TEXT,
    first_submitted_at TEXT NOT NULL,
    last_checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volume_rows (
    volume_id  TEXT NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    copies     INTEGER NOT NULL CHECK (copies > 0),
    PRIMARY KEY (volume_id, instrument, ts, level)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,
    volume_id     TEXT NOT NULL UNIQUE REFERENCES volumes(volume_id),
    receipt_kind  TEXT NOT NULL DEFAULT 'clean_return',
    content_hash  TEXT NOT NULL,
    physical_rows INTEGER NOT NULL,
    total_copies  INTEGER NOT NULL,
    issued_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_rows (
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    copies     INTEGER NOT NULL CHECK (copies > 0),
    PRIMARY KEY (receipt_id, instrument, ts, level)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_receipt_rows_key
    ON receipt_rows(instrument, ts, level, copies);

-- 全部已签回执对每种整行的累计占用份数（由签发事务维护）。
CREATE TABLE IF NOT EXISTS occupancy (
    instrument TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    level      INTEGER NOT NULL,
    occupied   INTEGER NOT NULL CHECK (occupied >= 0),
    PRIMARY KEY (instrument, ts, level)
) WITHOUT ROWID;
"""

DEFAULT_DB_PATH = Path("data") / "sound_archive.db"


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    import os

    if db_path is None:
        db_path = os.environ.get("SOUND_DB_PATH") or str(DEFAULT_DB_PATH)
    return Path(db_path)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """新建一个配置好的 SQLite 连接（调用方负责关闭）。"""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=15.0,
        isolation_level=None,  # 关闭隐式事务，显式 BEGIN IMMEDIATE
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    # WAL 允许读写并发；写事务仍然全局串行，语义不受影响。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """单个已提交状态的写事务：成功 COMMIT，任何异常 ROLLBACK。"""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """一致性只读事务：延迟 ``BEGIN`` 后的第一条 SELECT 固定一个快照。

    只读核对（/check、/availability、详情查询等）可能要发多条语句（卷状态、
    回执号、逐行余量、分批可用性查询）。包在同一个读事务里，这些语句读到的
    是**同一个已提交状态**：即使核对期间另一连接提交了补签，本事务的快照也
    不会变，响应里不会出现“状态显示未签、余量却已扣减”的跨状态拼接。
    WAL 模式下读事务不阻塞写事务，另一个连接仍可正常提交。
    """

    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
