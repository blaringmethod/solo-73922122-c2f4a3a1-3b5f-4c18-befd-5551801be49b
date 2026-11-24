"""“两卷旧档争用同一份读数”的确定性演示。

每次运行使用带随机后缀的卷号/片号/**仪器号**，因此即使在同一个长期运行的库上
反复调用 ``/demo/two-volumes-contend``，本次争用的整行键也是全新的，不会被历史
演示数据的全局占用份数污染，总能完整复现：
第一卷成功占用唯一副本，第二卷列出短缺；原样重送第一卷返回同一回执；篡改内容被拒；
破坏性换片被拒且旧片不变；补足副本后第二卷成功。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable

from . import services
from .errors import AppError


def _payloads(instrument: str) -> dict[str, str]:
    contested = f"{instrument},1760000000,612"   # 61.2 dB
    other = f"{instrument},1760000060,588"
    return {
        "contested": contested,
        "other": other,
        # 新仓最初：争用行只有 1 份，另有一行。
        "shard": (
            "instrument,utc,level_tenths_db\n"
            f"{contested}\n{other}\n"
        ),
        "volume_a": f"{contested}\n{other}\n",
        "volume_b": f"{contested}\n",
        "volume_a_tampered": (
            f"{instrument},1760000000,613\n{other}\n"  # 612 -> 613
        ),
        # 删掉争用行 -> 会让 A 的回执失去着落，必须被拒。
        "bad_replacement": (
            "instrument,utc,level_tenths_db\n"
            f"{other}\n"
        ),
        # 争用行补到 2 份 -> 同时覆盖 A、B。
        "good_replacement": (
            "instrument,utc,level_tenths_db\n"
            f"{contested}\n{contested}\n{other}\n"
        ),
    }


def _invoke(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        data = fn()
        return {"ok": True, "status_code": 200, "response": data}
    except AppError as exc:
        return {
            "ok": False,
            "status_code": exc.status_code,
            "code": exc.code,
            "response": exc.to_body(),
        }


def run_contention_demo(conn_factory: Callable[[], sqlite3.Connection]) -> dict[str, Any]:
    token = uuid.uuid4().hex[:8]
    instrument = f"M-DEMO-{token}"
    shard_id = f"SH-DEMO-{token}"
    volume_a = f"VOL-A-{token}"
    volume_b = f"VOL-B-{token}"
    payload = _payloads(instrument)

    steps: list[dict[str, Any]] = []

    def step(title: str, fn: Callable[[sqlite3.Connection], Any]) -> dict[str, Any]:
        conn = conn_factory()
        try:
            outcome = _invoke(lambda: fn(conn))
        finally:
            conn.close()
        outcome["title"] = title
        steps.append(outcome)
        return outcome

    put = step(
        "1. 放入新仓分片（争用行在本片仅 1 份）",
        lambda c: services.replace_shard(c, shard_id, payload["shard"]),
    )

    first_a = step(
        "2. 提交甲卷 VOL-A：核验通过并签发清退回执",
        lambda c: services.submit_volume(c, volume_a, payload["volume_a"]),
    )
    receipt_a_1 = first_a["response"].get("receipt_id") if first_a["ok"] else None

    first_b = step(
        "3. 提交乙卷 VOL-B 争用同一读数：副本不足，列出短缺行与份数",
        lambda c: services.submit_volume(c, volume_b, payload["volume_b"]),
    )

    replay_a = step(
        "4. 甲卷原样重送：返回同一张原回执，不重复占用",
        lambda c: services.submit_volume(c, volume_a, payload["volume_a"]),
    )
    receipt_a_2 = replay_a["response"].get("receipt_id") if replay_a["ok"] else None

    tampered_a = step(
        "5. 用同卷号提交内容不同的甲卷：拒绝（VOLUME_CONTENT_MISMATCH）",
        lambda c: services.submit_volume(c, volume_a, payload["volume_a_tampered"]),
    )

    bad_replace = step(
        "6. 整片替换为缺少争用行的内容：因保不住甲卷回执份数被拒绝",
        lambda c: services.replace_shard(c, shard_id, payload["bad_replacement"]),
    )
    shard_after_reject = step(
        "7. 回读被拒后的分片：仍是原内容（失败保留原状）",
        lambda c: services.get_shard(c, shard_id),
    )

    good_replace = step(
        "8. 整片替换为争用行有 2 份的新内容：能保住全部回执，允许",
        lambda c: services.replace_shard(c, shard_id, payload["good_replacement"]),
    )

    b_after = step(
        "9. 乙卷用已存明细重新核验：补足后通过并签发",
        lambda c: services.recheck_volume(c, volume_b),
    )

    final_stats = step("10. 全库统计（累计值，可能含历史演示）", lambda c: services.stats(c))

    original_hash = put["response"].get("content_hash") if put["ok"] else None
    shard_hash_after_reject = (
        shard_after_reject["response"].get("content_hash")
        if shard_after_reject["ok"]
        else None
    )

    # service 层把“不足”作为 200 返回体（HTTP 路由才映射为 409）。
    b_shortage = None
    b_responded_short = False
    if first_b["ok"] and first_b["response"].get("outcome") == "insufficient":
        b_responded_short = True
        b_shortage = (first_b["response"].get("shortages") or [None])[0]
    elif not first_b["ok"] and first_b.get("code") == "INSUFFICIENT_COPIES":
        b_responded_short = True
        b_shortage = (first_b["response"].get("shortages") or [None])[0]

    checks = {
        "a_issued_first": first_a["ok"] and first_a["response"].get("outcome") == "issued",
        "b_short_on_contested_copy": (
            b_responded_short
            and b_shortage is not None
            and b_shortage.get("required") == 1
            and b_shortage.get("total") == 1
            and b_shortage.get("occupied") == 1
            and b_shortage.get("available") == 0
            and b_shortage.get("short") == 1
        ),
        "replay_returns_same_receipt": bool(receipt_a_1) and receipt_a_1 == receipt_a_2,
        "tampered_content_rejected": (
            not tampered_a["ok"]
            and tampered_a.get("code") == "VOLUME_CONTENT_MISMATCH"
        ),
        "bad_replacement_rejected": (
            not bad_replace["ok"]
            and bad_replace.get("code") == "SHARD_COVERAGE_CONFLICT"
        ),
        "rejected_replacement_leaves_shard_unchanged": (
            original_hash is not None and original_hash == shard_hash_after_reject
        ),
        "good_replacement_accepted": good_replace["ok"],
        "b_issued_after_replenishment": (
            b_after["ok"] and b_after["response"].get("status") == "issued"
        ),
    }

    return {
        "demo": "two-old-volumes-contend-for-one-copy",
        "description_cn": "两卷旧档争用同一份读数的完整核验流程（本次运行使用独立行键）",
        "ids": {
            "run": token,
            "shard_id": shard_id,
            "volume_a": volume_a,
            "volume_b": volume_b,
            "instrument": instrument,
        },
        "all_checks_passed": all(checks.values()),
        "checks": checks,
        "contested_row": {
            "instrument": instrument,
            "ts": 1760000000,
            "utc": 1760000000,
            "level": 612,
            "level_db": 61.2,
        },
        "b_first_shortage": b_shortage,
        "steps": steps,
        "final_stats": final_stats.get("response") if final_stats["ok"] else None,
    }
