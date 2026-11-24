from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

X = "M-DEMO,1760000000,612"
Y = "M-DEMO,1760000060,588"


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


def put_shard(client, shard_id="S1", text=None):
    text = text if text is not None else f"{X}\n{Y}\n"
    return client.put(
        f"/shards/{shard_id}",
        content=text,
        headers={"Content-Type": "text/csv"},
    )


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


def test_health_docs_openapi(client):
    assert client.get("/health").json()["status"] == "ok"
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    assert client.get("/openapi.json").status_code == 200
    js = client.get("/static/swagger-ui-bundle.js")
    assert js.status_code == 200


def test_api_prefix_mirrors(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200
    # 镜像前缀不应出现在 OpenAPI 中
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/api") for p in paths)


# --------------------------------------------------------------------------- #
#  happy path + 幂等
# --------------------------------------------------------------------------- #


def test_submit_issues_receipt_and_replay_returns_same(client):
    put_shard(client)
    r1 = client.post("/volumes/V-A/submit", content=f"{X}\n{Y}\n",
                     headers={"Content-Type": "text/csv"})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["outcome"] == "issued"
    rid = b1["receipt_id"]
    assert b1["total_copies"] == 2

    r2 = client.post("/volumes/V-A/submit", content=f"{X}\n{Y}\n",
                     headers={"Content-Type": "text/csv"})
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["outcome"] == "already_issued"
    assert b2["idempotent_replay"] is True
    assert b2["receipt_id"] == rid

    # 按卷取回执与按回执号取一致
    assert client.get("/volumes/V-A/receipt").json()["receipt_id"] == rid
    assert client.get(f"/receipts/{rid}").json()["receipt_id"] == rid


def test_reordered_and_headered_resend_is_same_content(client):
    put_shard(client)
    r1 = client.post("/volumes/V-A/submit", content=f"{X}\n{Y}\n",
                     headers={"Content-Type": "text/csv"})
    rid = r1.json()["receipt_id"]

    reordered_with_header = (
        "instrument,utc,level_tenths_db\n"
        f"{Y}\n{X}\n"
    )
    r2 = client.post("/volumes/V-A/submit", content=reordered_with_header.encode(),
                     headers={"Content-Type": "text/csv; charset=utf-8"})
    assert r2.status_code == 200
    assert r2.json()["receipt_id"] == rid
    assert r2.json()["outcome"] == "already_issued"


def test_content_change_same_volume_rejected(client):
    put_shard(client)
    ok = client.post("/volumes/V-A/submit", content=f"{X}\n",
                     headers={"Content-Type": "text/csv"})
    assert ok.status_code == 200

    changed = client.post(
        "/volumes/V-A/submit",
        content="M-DEMO,1760000000,613\n",
        headers={"Content-Type": "text/csv"},
    )
    assert changed.status_code == 409
    body = changed.json()
    assert body["code"] == "VOLUME_CONTENT_MISMATCH"
    assert "stored_hash" in body
    # 原回执仍可查
    assert client.get("/volumes/V-A/receipt").status_code == 200


# --------------------------------------------------------------------------- #
# 争用 / 短缺
# --------------------------------------------------------------------------- #


def test_two_volumes_contend_for_single_copy(client):
    put_shard(client, text=f"{X}\n")  # 只有 1 份
    a = client.post("/volumes/V-A/submit", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert a.status_code == 200

    b = client.post("/volumes/V-B/submit", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert b.status_code == 409
    body = b.json()
    assert body["success"] is False and body["ok"] is False
    assert body["code"] == "INSUFFICIENT_COPIES"
    shortage = body["shortages"][0]
    assert shortage["instrument"] == "M-DEMO"
    assert shortage["ts"] == 1760000000
    assert shortage["level"] == 612
    assert shortage["required"] == 1
    assert shortage["total"] == 1
    assert shortage["occupied"] == 1
    assert shortage["available"] == 0
    assert shortage["short"] == 1
    assert shortage["missing"] == 1

    # verify 风格：不足也是 200 + sufficient=false
    v = client.post("/volumes/V-B/verify", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert v.status_code == 200
    assert v.json()["sufficient"] is False


def test_multiplicity_two_copies(client):
    put_shard(client, text=f"{X}\n{X}\n")  # 2 份
    a = client.post("/volumes/V-A/submit", content=f"{X}\n{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert a.status_code == 200
    # 2 份已被 A 占用，第三卷要 1 份 -> 缺 1
    b = client.post("/volumes/V-B/submit", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert b.status_code == 409
    s = b.json()["shortages"][0]
    assert s["total"] == 2 and s["occupied"] == 2 and s["available"] == 0
    assert s["short"] == 1


def test_short_volume_content_is_bound_and_can_recheck(client):
    put_shard(client, text=f"{X}\n")
    winner = client.post("/volumes/V-A/submit", content=f"{X}\n",
                         headers={"Content-Type": "text/csv"})
    assert winner.status_code == 200
    b = client.post("/volumes/V-B/submit", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert b.status_code == 409
    # 短缺卷也绑定内容：不同内容拒绝
    changed = client.post("/volumes/V-B/submit", content=f"{Y}\n",
                          headers={"Content-Type": "text/csv"})
    assert changed.status_code == 409
    assert changed.json()["code"] == "VOLUME_CONTENT_MISMATCH"
    # 补副本前 recheck 仍不足
    rc = client.post("/volumes/V-B/recheck")
    assert rc.status_code == 409
    assert rc.json()["code"] == "INSUFFICIENT_COPIES"


# --------------------------------------------------------------------------- #
# 分片替换保护
# --------------------------------------------------------------------------- #


def test_bad_shard_replacement_rejected_and_unchanged(client):
    put_shard(client, shard_id="S1", text=f"{X}\n{Y}\n")
    client.post("/volumes/V-A/submit", content=f"{X}\n",
                headers={"Content-Type": "text/csv"})
    before = client.get("/shards/S1").json()
    before_hash = before["content_hash"]

    bad = client.put(
        "/shards/S1",
        content=f"{Y}\n",  # 删掉 X
        headers={"Content-Type": "text/csv"},
    )
    assert bad.status_code == 409
    body = bad.json()
    assert body["code"] == "SHARD_COVERAGE_CONFLICT"
    violation = body["violations"][0]
    assert violation["occupied"] == 1
    assert violation["short"] == 1
    assert violation["receipts"][0]["volume_id"] == "V-A"

    after = client.get("/shards/S1").json()
    assert after["content_hash"] == before_hash
    assert after["physical_rows"] == 2
    # 占用份数没有被失败的操作影响
    stats = client.get("/stats").json()
    assert stats["occupied_copies"] == 1


def test_good_replacement_allows_waiting_volume(client):
    put_shard(client, text=f"{X}\n")
    client.post("/volumes/V-A/submit", content=f"{X}\n",
                headers={"Content-Type": "text/csv"})
    assert client.post("/volumes/V-B/submit", content=f"{X}\n",
                       headers={"Content-Type": "text/csv"}).status_code == 409

    good = client.put(
        "/shards/S1",
        content=f"{X}\n{X}\n",
        headers={"Content-Type": "text/csv"},
    )
    assert good.status_code == 200
    assert good.json()["outcome"] == "replaced"

    rc = client.post("/volumes/V-B/recheck")
    assert rc.status_code == 200
    assert rc.json()["status"] == "issued"


def test_replacement_with_enough_total_across_shards_allowed(client):
    # X 在两片各 1 份；回执占 1；替换 S1 删掉 X 后 S2 仍有 1 份 -> 允许（副本可互换）
    put_shard(client, shard_id="S1", text=f"{X}\n")
    put_shard(client, shard_id="S2", text=f"{X}\n")
    client.post("/volumes/V-A/submit", content=f"{X}\n",
                headers={"Content-Type": "text/csv"})
    resp = client.put("/shards/S1", content=f"{Y}\n",
                      headers={"Content-Type": "text/csv"})
    assert resp.status_code == 200


def test_delete_shard_protected_by_receipts(client):
    put_shard(client, text=f"{X}\n")
    client.post("/volumes/V-A/submit", content=f"{X}\n",
                headers={"Content-Type": "text/csv"})
    deleted = client.delete("/shards/S1")
    assert deleted.status_code == 409
    assert client.get("/shards/S1").status_code == 200


# --------------------------------------------------------------------------- #
# 失败原子性 / 输入形式
# --------------------------------------------------------------------------- #


def test_malformed_submit_persists_nothing(client):
    put_shard(client)
    bad = client.post(
        "/volumes/V-BAD/submit",
        content="M,1,not-an-int\n",
        headers={"Content-Type": "text/csv"},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "CSV_PARSE_ERROR"
    assert client.get("/volumes").json()["count"] == 0
    stats = client.get("/stats").json()
    assert stats["receipts"] == 0 and stats["occupied_copies"] == 0


def test_malformed_replacement_keeps_old_shard(client):
    put_shard(client, text=f"{X}\n")
    before = client.get("/shards/S1").json()
    bad = client.put("/shards/S1", content="M,1,61.2\n",
                     headers={"Content-Type": "text/csv"})
    assert bad.status_code == 422
    after = client.get("/shards/S1").json()
    assert after["content_hash"] == before["content_hash"]


def test_input_styles_all_work(client):
    # 1) multipart 文件上传
    files = {"file": ("s.csv", f"{X}\n".encode(), "text/csv")}
    r = client.put("/shards/S-FILE", files=files)
    assert r.status_code == 200, r.text

    # 2) /docs 表单 csv_text
    r = client.put("/shards/S-FORM", data={"csv_text": f"{X}\n{Y}\n"})
    assert r.status_code == 200, r.text

    # 3) JSON + csv 字符串，集合式
    r = client.post("/shards", json={"shard_id": "S-JSON", "csv": f"{X}\n"})
    assert r.status_code == 200, r.text

    # 4) JSON rows
    r = client.post("/volumes", json={
        "volume_id": "V-JSON",
        "rows": [["M-DEMO", 1760000000, 612]],
    })
    assert r.status_code == 200, r.text
    assert r.json()["receipt_id"]

    # 5) 集合式 + query id + 原始体
    r = client.post(f"/volumes?volume_id=V-Q", content=f"{Y}\n",
                    headers={"Content-Type": "text/csv"})
    assert r.status_code == 200, r.text

    # 6) /shards/{id}/csv 别名
    r = client.post("/shards/S-ALIAS/csv", content=f"{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert r.status_code == 200, r.text
    assert client.get("/shards/S-ALIAS/csv").status_code == 200


def test_raw_csv_labeled_urlencoded_or_json_on_path_submit(client):
    put_shard(client, text=f"{X}\n{Y}\n")

    # 很多客户端给“直接字符串 body”默认补 urlencoded 头，但体仍是原始 CSV。
    r = client.post(
        "/volumes/V-URLENC/submit",
        content=f"{X}\n".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "issued"

    # JSON {"csv": ...} 直接打到带路径 id 的 /submit
    r = client.post(
        "/volumes/V-JSONPATH/submit",
        json={"csv": f"{Y}\n"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "issued"

    # 真正 urlencoded 的 csv_text 字段也正常
    r = client.post(
        "/volumes/V-FORMFIELD/submit",
        data={"csv_text": f"{X}\n"},
    )
    assert r.status_code == 409  # X 已被 V-URLENC 占用唯一一份
    assert r.json()["code"] == "INSUFFICIENT_COPIES"


def test_availability_is_readonly(client):
    put_shard(client, text=f"{X}\n")
    r = client.post("/availability", content=f"{X}\n{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert r.status_code == 200
    body = r.json()
    assert body["sufficient"] is False
    row = body["rows"][0]
    assert row["required"] == 2 and row["available"] == 1 and row["short"] == 1
    # 没有产生任何占用
    assert client.get("/stats").json()["occupied_copies"] == 0


def test_stored_volume_check_is_readonly(client):
    put_shard(client, text=f"{X}\n")
    # 先登记一个短缺卷（无占用）
    r = client.post("/volumes/V-C/submit", content=f"{X}\n{X}\n",
                    headers={"Content-Type": "text/csv"})
    assert r.status_code == 409
    # /check 只读：仍然不足，且不产生回执/占用
    chk = client.post("/volumes/V-C/check")
    assert chk.status_code == 200
    body = chk.json()
    assert body["sufficient"] is False
    assert body["shortages"][0]["short"] == 1
    stats = client.get("/stats").json()
    assert stats["receipts"] == 0 and stats["occupied_copies"] == 0


def test_reset_requires_confirmation(client):
    put_shard(client)
    assert client.post("/admin/reset").status_code == 400
    assert client.post("/admin/reset?confirm=true").status_code == 200
    assert client.get("/shards").json()["count"] == 0


def test_demo_endpoint_passes_all_checks(client):
    r = client.post("/demo/two-volumes-contend")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["all_checks_passed"] is True, body["checks"]
    assert all(body["checks"].values())
    # GET 也可以（方便在浏览器/地址栏直接演示）
    assert client.get("/demo/two-volumes-contend").status_code == 200


def test_sqlite_persists_across_app_restart(tmp_path):
    db_file = tmp_path / "persist.db"
    app1 = create_app(db_file)
    with TestClient(app1) as c:
        assert put_shard(c, text=f"{X}\n").status_code == 200
        r = c.post("/volumes/V-PERSIST/submit", content=f"{X}\n",
                   headers={"Content-Type": "text/csv"})
        rid = r.json()["receipt_id"]

    # “重启”：用同一个 SQLite 文件新建应用，数据与回执仍在，且重送返回原回执。
    app2 = create_app(db_file)
    with TestClient(app2) as c:
        assert c.get("/shards/S1").status_code == 200
        got = c.get(f"/receipts/{rid}")
        assert got.status_code == 200
        assert got.json()["volume_id"] == "V-PERSIST"
        replay = c.post("/volumes/V-PERSIST/submit", content=f"{X}\n",
                        headers={"Content-Type": "text/csv"})
        assert replay.status_code == 200
        assert replay.json()["receipt_id"] == rid
        assert c.get("/stats").json()["occupied_copies"] == 1
