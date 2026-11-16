import sqlite3


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("p.domain_code = ?")
        params.append(domain_code)
    if storage_state:
        clauses.append("p.storage_state = ?")
        params.append(storage_state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT p.id, p.domain_code, p.biz_date, p.row_count, p.storage_state, "
        "p.archive_uri, p.created_at, "
        "(SELECT COUNT(*) FROM partition_dependencies d "
        " WHERE d.upstream_partition_id = p.id AND d.active = 1) AS downstream_ref_count, "
        "(SELECT COUNT(*) FROM partition_dependencies d "
        " WHERE d.downstream_partition_id = p.id AND d.active = 1) AS upstream_ref_count "
        "FROM partitions p"
        + where
        + " ORDER BY p.domain_code, p.biz_date",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None
