"""跨域分区依赖：登记 / 解除 / 同源快照与断链拦截。

约定：downstream（下游分区）引用 upstream（上游分区）。
销毁上游前，若仍有处于在线/裁撤中的下游引用，则依赖集合与上游分区态不对齐，
整单拒绝（由 execute 层在同一事务内完成校验与快照落库）。
"""

import json
import sqlite3

from app.db import begin_immediate


class DependencyBlockError(Exception):
    """依赖集合与分区态不对齐（仍被引用 / 引用已断），整单拒绝。"""


def _partition_ref(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    return conn.execute(
        "SELECT id, domain_code, biz_date, storage_state FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()


def list_dependencies(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = (
        "SELECT d.id, d.downstream_partition_id, d.upstream_partition_id, "
        "d.active, d.created_at, "
        "pd.domain_code AS downstream_domain, pd.biz_date AS downstream_biz_date, "
        "pd.storage_state AS downstream_state, "
        "pu.domain_code AS upstream_domain, pu.biz_date AS upstream_biz_date, "
        "pu.storage_state AS upstream_state "
        "FROM partition_dependencies d "
        "JOIN partitions pd ON pd.id = d.downstream_partition_id "
        "JOIN partitions pu ON pu.id = d.upstream_partition_id"
    )
    if active_only:
        sql += " WHERE d.active = 1"
    sql += " ORDER BY d.id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def add_dependency(
    conn: sqlite3.Connection,
    downstream_partition_id: int,
    upstream_partition_id: int,
) -> dict:
    """登记一条跨域依赖。整笔事务写锁，两人并线不互盖。"""
    if downstream_partition_id == upstream_partition_id:
        raise ValueError("下游与上游不能是同一分区")
    begin_immediate(conn)
    downstream = _partition_ref(conn, downstream_partition_id)
    upstream = _partition_ref(conn, upstream_partition_id)
    if not downstream or not upstream:
        raise ValueError("下游或上游分区不存在")
    if downstream["domain_code"] == upstream["domain_code"]:
        raise ValueError("跨域依赖必须指向不同主题域的分区")
    # 已销毁的分区不能作为依赖对象（无论是上游还是下游）
    if upstream["storage_state"] == "已销毁":
        raise DependencyBlockError(
            f"上游分区 {upstream['domain_code']}/{upstream['biz_date']} 已销毁，不能建立依赖"
        )
    conn.execute(
        "INSERT INTO partition_dependencies "
        "(downstream_partition_id, upstream_partition_id, active) VALUES (?,?,1) "
        "ON CONFLICT(downstream_partition_id, upstream_partition_id) "
        "DO UPDATE SET active = 1",
        (downstream_partition_id, upstream_partition_id),
    )
    return get_dependency_by_pair(
        conn, downstream_partition_id, upstream_partition_id
    )


def deactivate_dependency(conn: sqlite3.Connection, dependency_id: int) -> dict:
    """解除依赖（留痕，不物理删除），同样走写锁。"""
    begin_immediate(conn)
    row = conn.execute(
        "SELECT * FROM partition_dependencies WHERE id = ?", (dependency_id,)
    ).fetchone()
    if not row:
        raise ValueError("依赖不存在")
    conn.execute(
        "UPDATE partition_dependencies SET active = 0 WHERE id = ?",
        (dependency_id,),
    )
    return get_dependency(conn, dependency_id)


def get_dependency(conn: sqlite3.Connection, dependency_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM partition_dependencies WHERE id = ?", (dependency_id,)
    ).fetchone()
    return dict(row) if row else None


def get_dependency_by_pair(
    conn: sqlite3.Connection, downstream_id: int, upstream_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM partition_dependencies "
        "WHERE downstream_partition_id = ? AND upstream_partition_id = ?",
        (downstream_id, upstream_id),
    ).fetchone()
    return dict(row) if row else None


def active_downstream_refs(
    conn: sqlite3.Connection, upstream_partition_id: int
) -> list[dict]:
    """读取仍引用该上游、且下游本身还在线/裁撤中的有效依赖（同源分区态）。"""
    rows = conn.execute(
        "SELECT d.id AS dependency_id, d.downstream_partition_id, "
        "pd.domain_code AS downstream_domain, pd.biz_date AS downstream_biz_date, "
        "pd.storage_state AS downstream_state "
        "FROM partition_dependencies d "
        "JOIN partitions pd ON pd.id = d.downstream_partition_id "
        "WHERE d.upstream_partition_id = ? AND d.active = 1 "
        "AND pd.storage_state IN ('在线','裁撤中')",
        (upstream_partition_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def broken_dependency_refs(conn: sqlite3.Connection) -> list[dict]:
    """断链巡检：有效依赖的任一端分区已销毁（引用悬空）。"""
    rows = conn.execute(
        "SELECT d.id AS dependency_id, d.downstream_partition_id, "
        "d.upstream_partition_id, d.active, "
        "pd.storage_state AS downstream_state, pd.domain_code AS downstream_domain, "
        "pu.storage_state AS upstream_state, pu.domain_code AS upstream_domain "
        "FROM partition_dependencies d "
        "JOIN partitions pd ON pd.id = d.downstream_partition_id "
        "JOIN partitions pu ON pu.id = d.upstream_partition_id "
        "WHERE d.active = 1 AND ("
        "pd.storage_state = '已销毁' OR pu.storage_state = '已销毁')"
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot_dependency_set(
    conn: sqlite3.Connection,
    judgment_id: int,
    target_partition_id: int,
) -> dict:
    """在调用方事务内对「依赖集合 + 分区态」同源拍快照并落库。

    必须与分区态变更处于同一连接、同一事务，保证快照读到的就是本次裁决看到的世界。
    拦截作用域：
      - refs：仍在线/裁撤中、且有效引用目标分区的下游（销毁上游时不对齐）；
      - target_broken：目标分区自身参与的断链依赖（断链即拦）。
    全局 broken 仅写入快照供审计，不参与本单拦截判定。
    返回 {aligned, block_reason, refs, target_broken}。
    """
    refs = active_downstream_refs(conn, target_partition_id)
    target_broken = conn.execute(
        "SELECT d.id AS dependency_id, d.downstream_partition_id, "
        "d.upstream_partition_id, pd.storage_state AS downstream_state, "
        "pd.domain_code AS downstream_domain, pu.storage_state AS upstream_state, "
        "pu.domain_code AS upstream_domain "
        "FROM partition_dependencies d "
        "JOIN partitions pd ON pd.id = d.downstream_partition_id "
        "JOIN partitions pu ON pu.id = d.upstream_partition_id "
        "WHERE d.active = 1 AND (d.downstream_partition_id = ? OR d.upstream_partition_id = ?) "
        "AND (pd.storage_state = '已销毁' OR pu.storage_state = '已销毁')",
        (target_partition_id, target_partition_id),
    ).fetchall()
    target_broken = [dict(r) for r in target_broken]
    broken = broken_dependency_refs(conn)
    reasons: list[str] = []
    if refs:
        names = "；".join(
            f"{r['downstream_domain']}/{r['downstream_biz_date']}" for r in refs
        )
        reasons.append(f"仍被在线下游引用：{names}")
    if target_broken:
        reasons.append(f"目标分区存在断链依赖 {len(target_broken)} 条（引用端已销毁）")
    aligned = 0 if reasons else 1
    block_reason = "；".join(reasons)
    payload = {
        "target_partition_id": target_partition_id,
        "active_downstream_refs": refs,
        "target_broken_refs": target_broken,
        "global_broken_count": len(broken),
    }
    conn.execute(
        "INSERT INTO dependency_snapshots "
        "(judgment_id, target_partition_id, dependency_set_json, aligned, block_reason) "
        "VALUES (?,?,?,?,?)",
        (judgment_id, target_partition_id, json.dumps(payload, ensure_ascii=False),
         aligned, block_reason),
    )
    return {
        "aligned": aligned,
        "block_reason": block_reason,
        "refs": refs,
        "target_broken": target_broken,
    }


def list_snapshots(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, target_partition_id, dependency_set_json, "
        "aligned, block_reason, created_at "
        "FROM dependency_snapshots ORDER BY id DESC"
    ).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        item["dependency_set"] = json.loads(item.pop("dependency_set_json"))
    return items
