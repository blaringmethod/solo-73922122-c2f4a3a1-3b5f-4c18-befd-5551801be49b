#!/usr/bin/env python3
"""用标准库驱动“两卷旧档争用同一份读数”的完整 HTTP 演示。

用法：
    python scripts/demo.py                 # 默认 http://127.0.0.1:8000
    python scripts/demo.py --base http://127.0.0.1:8012
    BASE_URL=http://... python scripts/demo.py

每次运行使用独立的仪器号/卷号/片号，因此在同一个库上重复运行也不会被历史
数据的全局占用份数干扰。退出码 0 表示所有关键断言符合预期。仅用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def build_payloads(instrument: str) -> dict[str, str]:
    contested = f"{instrument},1760000000,612"  # 61.2 dB
    other = f"{instrument},1760000060,588"
    return {
        "contested": contested,
        "other": other,
        "shard": f"instrument,utc,level_tenths_db\n{contested}\n{other}\n",
        "a": f"{contested}\n{other}\n",
        "b": f"{contested}\n",
        "tampered": f"{instrument},1760000000,613\n{other}\n",
        "bad": f"instrument,utc,level_tenths_db\n{other}\n",
        "good": (
            "instrument,utc,level_tenths_db\n"
            f"{contested}\n{contested}\n{other}\n"
        ),
    }


def request(base: str, method: str, path: str, body: str | None = None):
    url = base.rstrip("/") + path
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "text/csv; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(raw)


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def wait_for_health(base: str, timeout_s: float = 40.0) -> None:
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            status, body = request(base, "GET", "/health")
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                print(f"[health] {base}/health -> {status} db={body.get('db_path')}")
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.5)
    raise SystemExit(f"服务在 {timeout_s}s 内未就绪：{last_err}")


def show(title: str, status, body, *keys):
    print("\n" + "=" * 78)
    print(f"{title}\n-> HTTP {status}")
    if isinstance(body, dict):
        for key in keys:
            if key in body:
                print(f"   {key}: {body[key]}")
        if "shortages" in body:
            for s in body["shortages"]:
                print(
                    "   shortage: "
                    f"instrument={s.get('instrument')} ts={s.get('ts')} level={s.get('level')} "
                    f"required={s.get('required')} total={s.get('total')} "
                    f"occupied={s.get('occupied')} available={s.get('available')} "
                    f"short={s.get('short')}"
                )
        if "violations" in body:
            for v in body["violations"]:
                print(
                    "   violation: "
                    f"instrument={v.get('instrument')} ts={v.get('ts')} level={v.get('level')} "
                    f"required_by_receipts={v.get('occupied')} "
                    f"total_after={v.get('total_after_replace', v.get('total_after_delete'))} "
                    f"short={v.get('short')}"
                )
    else:
        print(str(body)[:500])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000")
    )
    args = parser.parse_args()
    base = args.base

    wait_for_health(base)
    token = uuid.uuid4().hex[:8]
    instrument = f"M-DEMO-{token}"
    p = build_payloads(instrument)
    shard = f"SH-{token}"
    vol_a = f"VOL-A-{token}"
    vol_b = f"VOL-B-{token}"

    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool):
        results.append((name, ok))
        print(f"   ASSERT {'PASS' if ok else 'FAIL'}: {name}")

    # 1
    st, body = request(base, "PUT", f"/shards/{shard}", p["shard"])
    show("1. 放入新仓分片（争用行仅 1 份）", st, body, "outcome", "shard_id", "content_hash")
    original_hash = body.get("content_hash") if isinstance(body, dict) else None
    check("分片创建 200", st == 200)

    # 2
    st, body = request(base, "POST", f"/volumes/{vol_a}/submit", p["a"])
    show("2. 甲卷提交（争用行 + 另一行）-> 签发", st, body,
         "outcome", "receipt_id", "message")
    rid_a = body.get("receipt_id") if isinstance(body, dict) else None
    check("甲卷签发 200", st == 200 and bool(rid_a))

    # 3
    st, body = request(base, "POST", f"/volumes/{vol_b}/submit", p["b"])
    show("3. 乙卷争用同一行 -> 409 短缺", st, body, "code", "message")
    shortage = (body.get("shortages") or [None])[0] if isinstance(body, dict) else None
    check(
        "乙卷短缺 409 且 total=1/occupied=1/available=0/short=1",
        st == 409
        and shortage is not None
        and shortage["required"] == 1
        and shortage["total"] == 1
        and shortage["occupied"] == 1
        and shortage["available"] == 0
        and shortage["short"] == 1,
    )

    # 4
    st, body = request(base, "POST", f"/volumes/{vol_a}/submit", p["a"])
    show("4. 甲卷原样重送 -> 同一回执", st, body, "outcome", "receipt_id", "idempotent_replay")
    check(
        "重送返回原回执",
        st == 200
        and isinstance(body, dict)
        and body.get("receipt_id") == rid_a
        and body.get("outcome") == "already_issued",
    )

    # 5
    st, body = request(base, "POST", f"/volumes/{vol_a}/submit", p["tampered"])
    show("5. 同卷号内容不同 -> 拒绝", st, body, "code", "message")
    check(
        "篡改内容 409",
        st == 409 and isinstance(body, dict)
        and body.get("code") == "VOLUME_CONTENT_MISMATCH",
    )

    # 6
    st, body = request(base, "PUT", f"/shards/{shard}", p["bad"])
    show("6. 破坏性换片 -> 409 保不住回执", st, body, "code", "message")
    check(
        "坏替换 409",
        st == 409 and isinstance(body, dict)
        and body.get("code") == "SHARD_COVERAGE_CONFLICT",
    )

    # 7
    st, body = request(base, "GET", f"/shards/{shard}")
    show("7. 回读分片 -> 旧内容未变", st, body, "content_hash", "physical_rows")
    check(
        "拒绝后旧片不变",
        st == 200
        and isinstance(body, dict)
        and body.get("content_hash") == original_hash
        and body.get("physical_rows") == 2,
    )

    # 8
    st, body = request(base, "PUT", f"/shards/{shard}", p["good"])
    show("8. 补足争用行到 2 份的替换 -> 允许", st, body, "outcome", "message")
    check("好替换 200", st == 200)

    # 9
    st, body_b = request(base, "POST", f"/volumes/{vol_b}/recheck")
    show("9. 乙卷用已存明细重新核验 -> 签发", st, body_b, "status", "receipt_id")
    rid_b = body_b.get("receipt_id") if isinstance(body_b, dict) else None
    check(
        "乙卷补足后签发",
        st == 200 and isinstance(body_b, dict) and body_b.get("status") == "issued",
    )

    # 10 —— 用本次独立行键核对占用，避免库里历史累计数据干扰断言。
    _st, avail = request(base, "POST", "/availability", p["b"])
    contested_stat = avail["rows"][0] if isinstance(avail, dict) and avail.get("rows") else None
    _st_stats, stats = request(base, "GET", "/stats")
    show("10. 本次争用行占用（应为 total=2/occupied=2）与全库统计", 200, "stats_placeholder")
    if isinstance(stats, dict):
        print(f"   stats: {json.dumps(stats, ensure_ascii=False)}")
    if contested_stat:
        print(f"   contested: {json.dumps(contested_stat, ensure_ascii=False)}")
    receipts_ok = bool(rid_a) and bool(rid_b) and rid_a != rid_b
    occupancy_ok = bool(
        contested_stat
        and contested_stat.get("total") == 2
        and contested_stat.get("occupied") == 2
    )
    check("两张不同回执；本次争用行 total=2 且 occupied=2", receipts_ok and occupancy_ok)

    print("\n" + "#" * 78)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
