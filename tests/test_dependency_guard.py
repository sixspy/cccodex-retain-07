"""跨域依赖守卫：整单拒绝 / 断链即拦 / 同源快照 / 并线写锁。"""

import os
import threading

from app.db import get_db


def _conn():
    # 与应用同一个本机库文件（DATABASE_PATH 由 conftest 指到 tmp_path）
    return get_db(os.environ["DATABASE_PATH"])


def _part_id(authed, domain_code: str, biz_date: str) -> int:
    items = authed.get("/api/partitions", params={"domain_code": domain_code}).json()["items"]
    hit = next(p for p in items if p["biz_date"] == biz_date)
    return hit["id"]


def _judgment_for(authed, partition_id: int) -> dict:
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    return next(j for j in items if j["partition_id"] == partition_id)


def _scan(authed):
    r = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert r.status_code == 200
    return r.json()["job"]


def test_add_dependency_writes_object_and_list_shows_it(authed):
    downstream = _part_id(authed, "orders", "2026-11-01")
    upstream = _part_id(authed, "events", "2026-11-08")
    r = authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": downstream, "upstream_partition_id": upstream},
    )
    assert r.status_code == 200
    dep = r.json()["dependency"]
    assert dep["active"] == 1

    deps = authed.get("/api/dependencies").json()["items"]
    assert any(
        d["downstream_partition_id"] == downstream
        and d["upstream_partition_id"] == upstream
        and d["downstream_domain"] == "orders"
        and d["upstream_domain"] == "events"
        for d in deps
    )
    # 分区队列带依赖引用计数
    parts = authed.get("/api/partitions").json()["items"]
    up = next(p for p in parts if p["id"] == upstream)
    assert up["downstream_ref_count"] == 1


def test_same_domain_and_self_dependency_rejected(authed):
    a = _part_id(authed, "orders", "2026-11-01")
    b = _part_id(authed, "orders", "2026-11-05")
    assert authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": a, "upstream_partition_id": b},
    ).status_code == 400
    assert authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": a, "upstream_partition_id": a},
    ).status_code == 400


def test_destroy_upstream_blocked_when_downstream_still_references(authed):
    downstream = _part_id(authed, "orders", "2026-11-01")
    upstream = _part_id(authed, "events", "2026-11-08")
    authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": downstream, "upstream_partition_id": upstream},
    )
    _scan(authed)
    judgment = _judgment_for(authed, upstream)
    assert judgment["decided_action"] == "销毁"

    # 整单拒绝：409 + 拒绝原因
    blocked = authed.post(f"/api/judgments/{judgment['id']}/execute")
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["blocked"] is True
    assert "整单拒绝" in body["detail"]
    assert "orders/2026-11-01" in body["detail"]

    # 落库链路不断档：ok=0 流水 + aligned=0 快照（同源依赖集合）
    runs = authed.get("/api/runs").json()["items"]
    run = next(r for r in runs if r["judgment_id"] == judgment["id"])
    assert run["ok"] == 0
    assert "整单拒绝" in run["detail"]

    snaps = authed.get("/api/dependency-snapshots").json()["items"]
    snap = next(item for item in snaps if item["judgment_id"] == judgment["id"])
    assert snap["aligned"] == 0
    refs = snap["dependency_set"]["active_downstream_refs"]
    assert refs and refs[0]["downstream_partition_id"] == downstream

    # 分区态从未离开「在线」——无半截写入
    parts = authed.get("/api/partitions", params={"domain_code": "events"}).json()["items"]
    up = next(p for p in parts if p["id"] == upstream)
    assert up["storage_state"] == "在线"

    # 裁决仍待执行但带拒绝原因（只读冻结式呈现，可在解除后重试）
    j = next(
        x for x in authed.get("/api/judgments").json()["items"]
        if x["id"] == judgment["id"]
    )
    assert j["status"] == "待执行"
    assert j["dep_aligned"] == 0
    assert "整单拒绝" in (j["fail_reason"] or "")


def test_release_dependency_then_destroy_succeeds_atomically(authed):
    downstream = _part_id(authed, "orders", "2026-11-01")
    upstream = _part_id(authed, "events", "2026-11-12")
    add = authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": downstream, "upstream_partition_id": upstream},
    ).json()["dependency"]

    _scan(authed)
    judgment = _judgment_for(authed, upstream)
    assert authed.post(f"/api/judgments/{judgment['id']}/execute").status_code == 409

    # 解除依赖（留痕）后重试，整单成功
    rel = authed.post(f"/api/dependencies/{add['id']}/deactivate")
    assert rel.status_code == 200
    ok = authed.post(f"/api/judgments/{judgment['id']}/execute")
    assert ok.status_code == 200
    run = ok.json()["run"]
    assert run["ok"] == 1 and run["blocked"] == 0
    assert run["snapshot"]["aligned"] == 1

    parts = authed.get("/api/partitions", params={"domain_code": "events"}).json()["items"]
    up = next(p for p in parts if p["id"] == upstream)
    assert up["storage_state"] == "已销毁" and up["row_count"] == 0


def test_broken_chain_is_blocked_and_counted_in_reconcile(authed):
    downstream = _part_id(authed, "orders", "2026-11-05")
    upstream = _part_id(authed, "events", "2026-11-15")
    authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": downstream, "upstream_partition_id": upstream},
    )
    _scan(authed)

    # 旁路写入历史脏数据：让一条 active 依赖的上游直接处于已销毁（模拟历史断链）
    with _conn() as conn:
        conn.execute(
            "UPDATE partitions SET storage_state = '已销毁', row_count = 0 WHERE id = ?",
            (upstream,),
        )

    # 对账必须把断链数出来
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["broken_dependency_count"] >= 1
    assert any(
        b["upstream_partition_id"] == upstream
        for b in report["broken_dependencies"]
    )
    assert "断链依赖" in report["summary"]

    # 对仍在线的下游 orders-1105 执行归档裁决时，因它自身参与断链 -> 断链即拦
    d_judgment = _judgment_for(authed, downstream)
    blocked = authed.post(f"/api/judgments/{d_judgment['id']}/execute")
    assert blocked.status_code == 409
    assert "断链" in blocked.json()["detail"]
    # 下游分区保持在线，未被半截写入
    parts = authed.get("/api/partitions", params={"domain_code": "orders"}).json()["items"]
    down = next(p for p in parts if p["id"] == downstream)
    assert down["storage_state"] == "在线"


def test_cannot_register_dependency_pointing_at_destroyed_partition(authed):
    _scan(authed)
    destroyed = _part_id(authed, "events", "2026-11-08")
    j = _judgment_for(authed, destroyed)
    assert authed.post(f"/api/judgments/{j['id']}/execute").status_code == 200

    downstream = _part_id(authed, "orders", "2026-11-10")
    bad = authed.post(
        "/api/dependencies",
        json={"downstream_partition_id": downstream, "upstream_partition_id": destroyed},
    )
    assert bad.status_code == 409
    assert "已销毁" in bad.json()["detail"]


def test_concurrent_dependency_writes_are_serialized_no_half_writes(authed):
    downstream = _part_id(authed, "orders", "2026-11-14")
    upstreams = [
        _part_id(authed, "events", "2026-11-08"),
        _part_id(authed, "events", "2026-11-12"),
        _part_id(authed, "events", "2026-11-15"),
    ]
    errors: list[Exception] = []

    def worker(up_id: int):
        try:
            r = authed.post(
                "/api/dependencies",
                json={
                    "downstream_partition_id": downstream,
                    "upstream_partition_id": up_id,
                },
            )
            assert r.status_code == 200, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(u,)) for u in upstreams]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    deps = authed.get("/api/dependencies").json()["items"]
    got = {
        d["upstream_partition_id"]
        for d in deps
        if d["downstream_partition_id"] == downstream
    }
    # 三条并线登记全部落库，无丢失、无半截（写锁串行化）
    assert got == set(upstreams)


def test_concurrent_execute_same_judgment_only_one_succeeds(authed):
    _scan(authed)
    upstream = _part_id(authed, "events", "2026-11-08")
    judgment = _judgment_for(authed, upstream)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        r = authed.post(f"/api/judgments/{judgment['id']}/execute")
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 一个成功 200，另一个因状态/写锁被拒 409，绝不出现两个成功
    assert sorted(results) == [200, 409]
    parts = authed.get("/api/partitions", params={"domain_code": "events"}).json()["items"]
    up = next(p for p in parts if p["id"] == upstream)
    assert up["storage_state"] == "已销毁"
