import json
import sqlite3

from app.db import begin_immediate
from app.services.dependencies import snapshot_dependency_set
from app.services.partitions import get_partition


def _load_latest_snapshot(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, target_partition_id, dependency_set_json, "
        "aligned, block_reason, created_at "
        "FROM dependency_snapshots WHERE judgment_id = ? ORDER BY id DESC LIMIT 1",
        (judgment_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["dependency_set"] = json.loads(item.pop("dependency_set_json"))
    return item


def execute_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """执行单条裁决。

    依赖处置（二选一之「整单拒绝」）：
      - 入口 BEGIN IMMEDIATE 拿库级写锁，两人并线串行，快照不互盖；
      - 同一事务内先对依赖集合 + 上下游分区态同源拍快照；
      - 销毁动作若对齐失败（仍被在线下游引用 / 目标存在断链），
        不改动任何分区态，写 ok=0 流水与拒绝原因后整笔退回（事务提交留痕，
        分区从未离开「在线」，不存在半截写入），由路由层转 409。
    """
    # 写流程先抢锁：抢不到会在 busy_timeout 后抛 OperationalError，由路由层转 409
    begin_immediate(conn)

    row = conn.execute(
        "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
    ).fetchone()
    if not row:
        raise ValueError("裁决不存在")
    judgment = dict(row)
    if judgment["status"] not in ("待执行", "已失败"):
        raise ValueError(f"当前状态不可执行：{judgment['status']}")

    part = get_partition(conn, judgment["partition_id"])
    if not part:
        raise ValueError("分区不存在")
    if part["storage_state"] not in ("在线", "裁撤中"):
        raise ValueError(f"分区状态不可裁撤：{part['storage_state']}")

    action = judgment["decided_action"]

    # 依赖集合快照与分区态同源：同一连接、同一事务、同一序列化点
    snapshot = snapshot_dependency_set(conn, judgment_id, part["id"])

    # 对齐校验（二选一之「整单拒绝」），两类不对齐：
    #   1) 断链即拦：目标分区自身参与的有效依赖已有一端销毁 —— 任何动作都拦；
    #   2) 销毁上游时仍被在线/裁撤中下游引用 —— 仅销毁动作拦（归档不解除引用）。
    referenced_block = action == "销毁" and bool(snapshot["refs"])
    broken_block = bool(snapshot["target_broken"])
    if referenced_block or broken_block:
        reason = f"整单拒绝：依赖集合与上游分区态不对齐。{snapshot['block_reason']}"
        # 裁决保持「待执行」（可在依赖解除后重试），仅落拒绝原因
        conn.execute(
            "UPDATE judgments SET fail_reason = ? WHERE id = ?",
            (reason, judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, part["storage_state"], None, reason),
        )
        run = get_run_by_judgment(conn, judgment_id)
        run["blocked"] = 1
        run["block_reason"] = reason
        run["snapshot"] = _load_latest_snapshot(conn, judgment_id)
        return run

    conn.execute(
        "UPDATE judgments SET status = '执行中', fail_reason = '' WHERE id = ?",
        (judgment_id,),
    )
    conn.execute(
        "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
        (part["id"],),
    )

    try:
        if action == "归档":
            uri = f"archive://{part['domain_code']}/{part['biz_date']}"
            conn.execute(
                "UPDATE partitions SET storage_state = '已归档', archive_uri = ? WHERE id = ?",
                (uri, part["id"]),
            )
            result_state = "已归档"
            archive_uri = uri
            detail = "分区已归档并保留可查归档地址"
        elif action == "销毁":
            conn.execute(
                "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                "row_count = 0 WHERE id = ?",
                (part["id"],),
            )
            result_state = "已销毁"
            archive_uri = None
            detail = "分区已销毁，在线行数清零；依赖快照对齐通过"
        else:
            raise ValueError(f"未知动作：{action}")

        conn.execute(
            "UPDATE judgments SET status = '已完成', fail_reason = '' WHERE id = ?",
            (judgment_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (judgment_id, action, result_state, archive_uri, detail),
        )
    except Exception as exc:  # noqa: BLE001 — 失败闭：恢复在线并记失败
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL WHERE id = ?",
            (part["id"],),
        )
        conn.execute(
            "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
            (str(exc), judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, "在线", None, str(exc)),
        )
        raise

    run = get_run_by_judgment(conn, judgment_id)
    run["blocked"] = 0
    run["snapshot"] = _load_latest_snapshot(conn, judgment_id)
    return run


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
