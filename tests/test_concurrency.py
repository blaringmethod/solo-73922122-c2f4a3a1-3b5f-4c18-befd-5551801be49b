from __future__ import annotations

import threading

from app.db import connect, init_db
from app.errors import AppError
from app import services

X = "M-HOT,1760000000,612"


def _seed(db_path, copies: int) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        services.replace_shard(conn, "S-HOT", text="\n".join([X] * copies) + "\n")
    finally:
        conn.close()


def test_exactly_one_volume_wins_single_copy(tmp_path):
    db_path = tmp_path / "race1.db"
    _seed(db_path, copies=1)

    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        conn = connect(db_path)
        barrier.wait()
        try:
            result = services.submit_volume(conn, f"V-{i}", text=X + "\n")
            outcome = result["outcome"]
        except AppError as exc:  # 内容冲突等不应发生
            outcome = f"error:{exc.code}"
        finally:
            conn.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count("issued") == 1, outcomes
    assert outcomes.count("insufficient") == n - 1, outcomes

    check = connect(db_path)
    try:
        stats = services.stats(check)
        assert stats["receipts"] == 1
        assert stats["occupied_copies"] == 1
        assert stats["issued_volumes"] == 1
    finally:
        check.close()


def test_exactly_three_win_with_three_copies(tmp_path):
    db_path = tmp_path / "race3.db"
    _seed(db_path, copies=3)

    n = 10
    barrier = threading.Barrier(n)
    issued = 0
    insufficient = 0
    lock = threading.Lock()

    def worker(i: int) -> None:
        nonlocal issued, insufficient
        conn = connect(db_path)
        barrier.wait()
        try:
            result = services.submit_volume(conn, f"V-{i}", text=X + "\n")
            kind = result["outcome"]
        finally:
            conn.close()
        with lock:
            if kind == "issued":
                issued += 1
            elif kind == "insufficient":
                insufficient += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert issued == 3
    assert insufficient == n - 3

    conn = connect(db_path)
    try:
        assert services.stats(conn)["occupied_copies"] == 3
    finally:
        conn.close()


def test_concurrent_replays_do_not_double_occupy(tmp_path):
    db_path = tmp_path / "replay.db"
    _seed(db_path, copies=5)

    # 先签发一次
    conn = connect(db_path)
    first = services.submit_volume(conn, "V-SAME", text=f"{X}\n{X}\n")
    rid = first["receipt_id"]
    conn.close()

    n = 8
    barrier = threading.Barrier(n)
    receipt_ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            r = services.submit_volume(c, "V-SAME", text=f"{X}\n{X}\n")
            with lock:
                receipt_ids.append(r["receipt_id"])
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert receipt_ids == [rid] * n
    c = connect(db_path)
    try:
        assert services.stats(c)["receipts"] == 1
        assert services.stats(c)["occupied_copies"] == 2
    finally:
        c.close()
