"""本次修复的回归测试：

1. 大卷（键数量超过单条 SQL 参数容量）在 submit / verify / availability /
   replace_shard / recheck 各路径都能查全，不再 500；
2. 大卷有缺份时列出短缺，不签发回执、不占份数；
3. 换片会使已签回执缺份时拒绝并保留旧片（大键集，走分批路径）；
4. 只读 /check 与并发补签之间，响应的卷状态/回执号/余量必须来自同一个
   已提交状态（快照一致性）；
5. 两卷争用最后一份读数最多签一张回执；同卷并发原样重送仍取原回执
   （既有并发测试在 test_concurrency.py，这里补 HTTP 级短缺语义）。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import services
from app.db import connect, init_db
from app.errors import AppError

X = "M-DEMO,1760000000,612"
Y = "M-DEMO,1760000060,588"


@pytest.fixture()
def client(tmp_path):
    from app.main import create_app

    app = create_app(tmp_path / "large.db")
    with TestClient(app) as c:
        yield c


def _big_csv(n: int, *, instrument: str = "MB", level_base: int = 600) -> str:
    """n 个互不相同的整行键 -> 3*n 个绑定参数，方便越过参数上限。"""

    return "".join(
        f"{instrument}{i % 997},{1760000000 + i},{level_base + i % 50}\n"
        for i in range(n)
    )


# --------------------------------------------------------------------------- #
# 1. 参数上限：超过单条 SQL 容量也必须查全（service 层，真实 250000 上限）
# --------------------------------------------------------------------------- #


def test_fetch_availability_batches_beyond_variable_limit(tmp_path):
    db_path = tmp_path / "big.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        n = 90000  # 270000 个参数 > 单条 SQL 的 250000 上限
        text = _big_csv(n)
        rep = services.replace_shard(conn, "SH-BIG", text)
        assert rep["distinct_rows"] == n

        body = services.check_availability(conn, text)
        assert len(body["rows"]) == n  # 每个涉及行都查到，没有被参数上限截断
        assert body["sufficient"] is True
        assert all(row["total"] == 1 and row["available"] == 1 for row in body["rows"])
    finally:
        conn.close()


def test_fetch_availability_chunk_helper_respects_tiny_limit(tmp_path):
    """用临时压低的变量上限验证分批逻辑本身（不依赖具体 SQLite 编译上限）。"""

    db_path = tmp_path / "tiny.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.setlimit(__import__("sqlite3").SQLITE_LIMIT_VARIABLE_NUMBER, 30)
        # 上限 30：每键 3 参数，扣 16 预留后每批最多 4 个键。
        assert services._keys_per_batch(conn) == 4
        text = _big_csv(10)
        services.replace_shard(conn, "SH-T", text)
        from app.csvio import summarize
        summary = summarize(text)
        avail = services.fetch_availability(conn, summary.freq.keys())
        assert len(avail) == 10  # 分 3 批查全
        assert all(v["total"] == 1 for v in avail.values())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2. 大卷缺份：列短缺、不签回执、不占份数；补够后 recheck 可签发
# --------------------------------------------------------------------------- #


def test_large_volume_shortage_lists_and_does_not_occupy(tmp_path):
    db_path = tmp_path / "short.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        n = 1000
        text = _big_csv(n)
        services.replace_shard(conn, "SH-BIG", text)

        # 每种整行仓里只有 1 份，却要 2 份 -> 全部短缺
        result = services.submit_volume(conn, "V-BIG", text + text)
        assert result["outcome"] == "insufficient"
        assert result["shortage_kinds"] == n
        assert result["shortage_copies"] == n

        stats = services.stats(conn)
        assert stats["receipts"] == 0
        assert stats["occupied_copies"] == 0  # 短缺不占份数
        assert stats["short_volumes"] == 1

        # 只读 /availability 同样走分批且不留痕
        body = services.check_availability(conn, text + text)
        assert body["sufficient"] is False
        assert len(body["shortages"]) == n
        assert services.stats(conn)["occupied_copies"] == 0

        # 补够副本（每个键 2 份）后 recheck 签发
        services.replace_shard(conn, "SH-BIG", text + text)
        rc = services.recheck_volume(conn, "V-BIG")
        assert rc["outcome"] == "issued"
        assert rc["total_copies"] == 2 * n
        assert services.stats(conn)["receipts"] == 1
        assert services.stats(conn)["occupied_copies"] == 2 * n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. 大键集换片：使已签回执缺份则拒绝，旧片保留
# --------------------------------------------------------------------------- #


def test_large_shard_replacement_protects_receipts(tmp_path):
    db_path = tmp_path / "rep.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        n = 1000
        text = _big_csv(n)
        services.replace_shard(conn, "SH-BIG", text)
        issued = services.submit_volume(conn, "V-BIG", text)
        assert issued["outcome"] == "issued"
        before = services.get_shard(conn, "SH-BIG")

        # 整片换成不同读数：旧片每个键的副本被删，回执全部失去着落
        other = _big_csv(n, level_base=900)
        with pytest.raises(AppError) as exc:
            services.replace_shard(conn, "SH-BIG", other)
        assert exc.value.code == "SHARD_COVERAGE_CONFLICT"
        assert exc.value.details["violation_kinds"] == n

        after = services.get_shard(conn, "SH-BIG")
        assert after["content_hash"] == before["content_hash"]  # 旧片保留
        assert services.stats(conn)["occupied_copies"] == n
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4. 快照一致性：只读核对期间另一连接提交补签
# --------------------------------------------------------------------------- #


def test_check_uses_one_committed_snapshot_under_concurrent_reissue(tmp_path):
    db_path = tmp_path / "snap.db"
    init_db(db_path)
    seed = connect(db_path)
    try:
        services.replace_shard(seed, "S1", f"{X}\n")
        services.submit_volume(seed, "VOL-A", f"{X}\n")
        services.submit_volume(seed, "VOL-V", f"{X}\n")  # 短缺卷
        services.replace_shard(seed, "S1", f"{X}\n{X}\n")  # 补到 2 份但暂不签
    finally:
        seed.close()

    reader_conn = connect(db_path)
    writer_conn = connect(db_path)
    entered = threading.Event()
    proceed = threading.Event()
    original_load = services._load_volume_freq

    def hooked_load(conn, volume_id):
        if conn is reader_conn:
            entered.set()
            proceed.wait(10)
        return original_load(conn, volume_id)

    services._load_volume_freq = hooked_load
    try:
        box: dict = {}

        def reader():
            box["body"] = services.check_stored_volume(reader_conn, "VOL-V")

        t = threading.Thread(target=reader)
        t.start()
        assert entered.wait(10)

        reissued = services.recheck_volume(writer_conn, "VOL-V")
        assert reissued["outcome"] == "issued"
        proceed.set()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        services._load_volume_freq = original_load
        writer_conn.close()
        reader_conn.close()

    body = box["body"]
    row = body["rows"][0]
    before_snapshot = (
        body["stored_status"] == "short"
        and body["receipt_id"] is None
        and row["occupied"] == 1
        and row["available"] == 1
        and body["sufficient"] is True
    )
    after_snapshot = (
        body["stored_status"] == "issued"
        and body["receipt_id"] == reissued["receipt_id"]
        and row["occupied"] == 2
        and row["available"] == 0
        and body["sufficient"] is False
    )
    assert before_snapshot or after_snapshot, (
        "卷状态、回执号与余量来自不同已提交状态："
        f"status={body['stored_status']} receipt={body['receipt_id']} "
        f"occupied={row['occupied']} available={row['available']}"
    )

    # 补签提交后的新连接必须完整看到已签状态
    later = connect(db_path)
    try:
        fresh = services.check_stored_volume(later, "VOL-V")
        assert fresh["stored_status"] == "issued"
        assert fresh["receipt_id"] == reissued["receipt_id"]
        assert fresh["rows"][0]["occupied"] == 2
        assert fresh["sufficient"] is False
    finally:
        later.close()


# --------------------------------------------------------------------------- #
# 5. HTTP 级：大卷经 API 不再 500；争用与幂等语义保持
# --------------------------------------------------------------------------- #


def test_http_large_volume_beyond_variable_limit(client):
    n = 90000
    text = _big_csv(n)
    put = client.put(
        "/shards/SH-HUGE", content=text, headers={"Content-Type": "text/csv"}
    )
    assert put.status_code == 200, put.text

    avail = client.post(
        "/availability", content=text, headers={"Content-Type": "text/csv"}
    )
    assert avail.status_code == 200, avail.text
    assert len(avail.json()["rows"]) == n

    submitted = client.post(
        "/volumes/V-HUGE/submit",
        content=text,
        headers={"Content-Type": "text/csv"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["outcome"] == "issued"


def test_http_contention_and_idempotent_replay_unchanged(client):
    client.put("/shards/S1", content=f"{X}\n", headers={"Content-Type": "text/csv"})
    a = client.post(
        "/volumes/V-A/submit", content=f"{X}\n", headers={"Content-Type": "text/csv"}
    )
    assert a.status_code == 200
    rid = a.json()["receipt_id"]

    b = client.post(
        "/volumes/V-B/submit", content=f"{X}\n", headers={"Content-Type": "text/csv"}
    )
    assert b.status_code == 409
    assert b.json()["code"] == "INSUFFICIENT_COPIES"

    replay = client.post(
        "/volumes/V-A/submit", content=f"{X}\n", headers={"Content-Type": "text/csv"}
    )
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "already_issued"
    assert replay.json()["receipt_id"] == rid

    stats = client.get("/stats").json()
    assert stats["receipts"] == 1 and stats["occupied_copies"] == 1
