"""业务逻辑：登记现用表、提交换发、批次确认、查询。"""

import json
import sqlite3
from typing import Any, Dict, List, Tuple

from . import cycles
from .db import read_transaction, transaction


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


Cell = Tuple[str, str]


# --------------------------------------------------------------------------- #
# 校验辅助
# --------------------------------------------------------------------------- #
def _grid(rows: List[str], cols: List[str], matrix: List[List[int]]):
    if len(matrix) != len(rows):
        raise ServiceError(422, "matrix row count must equal rows length")
    if len(matrix[0]) != len(cols):
        raise ServiceError(422, "matrix column count must equal cols length")
    values: Dict[Cell, int] = {}
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            v = matrix[i][j]
            if v < 0:
                raise ServiceError(422, f"tonnage at ({r},{c}) is negative")
            values[(r, c)] = v
    return values


def _row_totals(values: Dict[Cell, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for (r, c), v in values.items():
        out[r] = out.get(r, 0) + v
    return out


def _col_totals(values: Dict[Cell, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for (r, c), v in values.items():
        out[c] = out.get(c, 0) + v
    return out


# --------------------------------------------------------------------------- #
# 同月先登记现用表
# --------------------------------------------------------------------------- #
def register_month(
    conn: sqlite3.Connection, month: str, rows: List[str], cols: List[str],
    matrix: List[List[int]],
) -> Dict[str, Any]:
    values = _grid(rows, cols, matrix)

    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM month WHERE month = ?", (month,)
        ).fetchone():
            raise ServiceError(409, f"month {month} already registered")

        conn.execute(
            "INSERT INTO month(month, row_labels, col_labels) VALUES (?,?,?)",
            (month, json.dumps(rows, ensure_ascii=False),
             json.dumps(cols, ensure_ascii=False)),
        )
        conn.executemany(
            "INSERT INTO live_cell(month, r, c, value) VALUES (?,?,?,?)",
            [(month, r, c, v) for (r, c), v in sorted(values.items())],
        )

    return {"month": month, "rows": rows, "cols": cols, "matrix": matrix}


# --------------------------------------------------------------------------- #
# 现用表的可见值只依赖最近一笔受理换发的格明细
# --------------------------------------------------------------------------- #
def _latest_replacement(
    conn: sqlite3.Connection, month: str
) -> Any:
    """同月最近一笔受理的换发（无论是否已全部确认）。"""
    return conn.execute(
        "SELECT id, status FROM replacement WHERE month = ? "
        "ORDER BY id DESC LIMIT 1",
        (month,),
    ).fetchone()


def _live_values(conn: sqlite3.Connection, month: str) -> Dict[Cell, int]:
    """尚未发生过换发时，现用表就是登记时的逐格值。"""
    return {
        (row["r"], row["c"]): row["value"]
        for row in conn.execute(
            "SELECT r, c, value FROM live_cell WHERE month = ?", (month,)
        )
    }


def _confirmed_cells(
    conn: sqlite3.Connection, rp_id: int
) -> set:
    """该申请中已生效的格子。

    新库以批次确认标记为准（确认一批只改批状态，不逐格写）；
    旧库逐格写过 replacement_cell.confirmed，保留兼容。
    """
    cells = {
        (row["r"], row["c"])
        for row in conn.execute(
            "SELECT bc.r, bc.c FROM batch_cell bc "
            "JOIN batch b ON b.replacement_id = bc.replacement_id "
            " AND b.batch_no = bc.batch_no "
            "WHERE bc.replacement_id = ? AND b.confirmed = 1",
            (rp_id,),
        )
    }
    cells.update(
        (row["r"], row["c"])
        for row in conn.execute(
            "SELECT r, c FROM replacement_cell "
            "WHERE replacement_id = ? AND confirmed = 1",
            (rp_id,),
        )
    )
    return cells


def _effective_values(
    conn: sqlite3.Connection, month: str
):
    """现用表可见值：最多读最近一笔受理换发的格明细。

    没有换发时读登记底表；有换发时只读该申请的 old_value/new_value：
    已生效格取新值，未生效格取旧值（旧值即该申请受理时的实际底表）。
    这样确认无需逐格改写现用表，查询也不会回溯更早的换发历史。
    """
    latest = _latest_replacement(conn, month)
    if latest is None:
        return _live_values(conn, month), None

    rows = conn.execute(
        "SELECT r, c, old_value, new_value FROM replacement_cell "
        "WHERE replacement_id = ?",
        (latest["id"],),
    ).fetchall()

    if latest["status"] == "confirmed":
        return {
            (row["r"], row["c"]): row["new_value"] for row in rows
        }, latest

    confirmed = _confirmed_cells(conn, latest["id"])
    return {
        (row["r"], row["c"]): (
            row["new_value"]
            if (row["r"], row["c"]) in confirmed
            else row["old_value"]
        )
        for row in rows
    }, latest


def _base_values(conn: sqlite3.Connection, month: str) -> Dict[Cell, int]:
    """下一笔换发校验与切批所依据的实际已生效底表。"""
    pending = conn.execute(
        "SELECT 1 FROM replacement WHERE month = ? AND status = 'pending'",
        (month,),
    ).fetchone()
    if pending:
        raise ServiceError(
            409,
            "an earlier replacement for this month still has "
            "unconfirmed batches",
        )

    latest = _latest_replacement(conn, month)
    if latest is None:
        return _live_values(conn, month)
    # 上一笔已全部确认：它的新值就是此刻实际生效的底表，不能再用
    # 可能过期的 live_cell。
    return {
        (row["r"], row["c"]): row["new_value"]
        for row in conn.execute(
            "SELECT r, c, new_value FROM replacement_cell "
            "WHERE replacement_id = ?",
            (latest["id"],),
        )
    }


def get_table(conn: sqlite3.Connection, month: str) -> Dict[str, Any]:
    with read_transaction(conn):
        m = conn.execute(
            "SELECT row_labels, col_labels FROM month WHERE month = ?",
            (month,),
        ).fetchone()
        if not m:
            raise ServiceError(404, f"month {month} not registered")
        rows: List[str] = json.loads(m["row_labels"])
        cols: List[str] = json.loads(m["col_labels"])

        values, latest = _effective_values(conn, month)
        matrix = [[values[(r, c)] for c in cols] for r in rows]
        effective = {(r, c): matrix[i][j]
                     for i, r in enumerate(rows)
                     for j, c in enumerate(cols)}

        result = {
            "month": month,
            "rows": rows,
            "cols": cols,
            "matrix": matrix,
            "row_subtotals": [_row_totals(effective)[r] for r in rows],
            "col_subtotals": [_col_totals(effective)[c] for c in cols],
        }
        if latest is not None and latest["status"] == "pending":
            result["pending_replacement_id"] = latest["id"]
        return result


# --------------------------------------------------------------------------- #
# 提交换发：同形、每格最多增减 1 吨、两组小计仍对上、切分为最小批次
# --------------------------------------------------------------------------- #
def create_replacement(
    conn: sqlite3.Connection, month: str, rows: List[str], cols: List[str],
    matrix: List[List[int]],
) -> Dict[str, Any]:
    new_values = _grid(rows, cols, matrix)

    # 纯计算的校验放在事务外；随后在同一个写事务里重新读取实际已生效底表
    # 并写入，保证「校验依据」与「落库内容」一致。
    with transaction(conn):
        m = conn.execute(
            "SELECT row_labels, col_labels FROM month WHERE month = ?",
            (month,),
        ).fetchone()
        if not m:
            raise ServiceError(404, f"month {month} not registered")
        base_rows: List[str] = json.loads(m["row_labels"])
        base_cols: List[str] = json.loads(m["col_labels"])

        # 上一笔换发确认到一半或已全部确认时，校验和切批都必须按实际
        # 已生效的底表（最近一笔受理换发的格明细），而不是 live_cell。
        live = _base_values(conn, month)

        # 行列相同
        if rows != base_rows or cols != base_cols:
            raise ServiceError(
                422,
                "new table must keep the exact same row labels (areas) and "
                "column labels (cargo types)",
            )

        missing = set(live) ^ set(new_values)
        if missing:
            raise ServiceError(
                422, "new table must cover exactly the live table's cells"
            )

        deltas: Dict[Cell, int] = {}
        for cell, new_v in new_values.items():
            d = new_v - live[cell]
            if abs(d) > 1:
                raise ServiceError(
                    422,
                    f"cell {cell} changes by {d:+d} tons; per-cell change is "
                    "limited to +/-1 ton",
                )
            deltas[cell] = d

        if all(d == 0 for d in deltas.values()):
            raise ServiceError(422, "new table is identical to the live table")

        # 各行（作业区）、各列（货类）小计仍须对上
        row_delta_sums = _row_totals(deltas)
        col_delta_sums = _col_totals(deltas)
        if any(row_delta_sums.get(r, 0) != 0 for r in base_rows):
            raise ServiceError(422, "row (area) subtotals do not match the live table")
        if any(col_delta_sums.get(c, 0) != 0 for c in base_cols):
            raise ServiceError(422, "column (cargo) subtotals do not match the live table")

        # 全部变动格切成互不重叠的最小批次（简单有向环）
        batches = cycles.decompose_into_cycles(deltas)

        cur = conn.execute(
            "INSERT INTO replacement(month, status) VALUES (?, 'pending')",
            (month,),
        )
        rp_id = cur.lastrowid

        conn.executemany(
            "INSERT INTO replacement_cell"
            "(replacement_id, r, c, old_value, new_value, confirmed) "
            "VALUES (?,?,?,?,?,0)",
            [
                (rp_id, r, c, live[(r, c)], new_values[(r, c)])
                for (r, c) in sorted(new_values)
            ],
        )
        for no, batch_cells in enumerate(batches, start=1):
            conn.execute(
                "INSERT INTO batch(replacement_id, batch_no, confirmed) "
                "VALUES (?,?,0)",
                (rp_id, no),
            )
            conn.executemany(
                "INSERT INTO batch_cell(replacement_id, batch_no, r, c) "
                "VALUES (?,?,?,?)",
                [(rp_id, no, r, c) for (r, c) in sorted(batch_cells)],
            )

    return {
        "replacement_id": rp_id,
        "month": month,
        "status": "pending",
        "batch_count": len(batches),
        "changed_cells": sum(1 for d in deltas.values() if d != 0),
        "batches": [
            {
                "batch_no": no,
                "cells": [list(cell) for cell in sorted(batch_cells)],
            }
            for no, batch_cells in enumerate(batches, start=1)
        ],
    }


def get_replacement(conn: sqlite3.Connection, rp_id: int) -> Dict[str, Any]:
    with read_transaction(conn):
        rp = conn.execute(
            "SELECT id, month, status, created_at, confirmed_at "
            "FROM replacement WHERE id = ?",
            (rp_id,),
        ).fetchone()
        if not rp:
            raise ServiceError(404, f"replacement {rp_id} not found")
        total = conn.execute(
            "SELECT COUNT(*) n FROM batch WHERE replacement_id = ?", (rp_id,)
        ).fetchone()["n"]
        confirmed = conn.execute(
            "SELECT COUNT(*) n FROM batch WHERE replacement_id = ? AND confirmed = 1",
            (rp_id,),
        ).fetchone()["n"]
        return {
            "replacement_id": rp["id"],
            "month": rp["month"],
            "status": rp["status"],
            "created_at": rp["created_at"],
            "confirmed_at": rp["confirmed_at"],
            "batch_count": total,
            "confirmed_batches": confirmed,
        }


def list_batches(conn: sqlite3.Connection, rp_id: int) -> List[Dict[str, Any]]:
    with read_transaction(conn):
        rp = conn.execute(
            "SELECT 1 FROM replacement WHERE id = ?", (rp_id,)
        ).fetchone()
        if not rp:
            raise ServiceError(404, f"replacement {rp_id} not found")
        out: List[Dict[str, Any]] = []
        for b in conn.execute(
            "SELECT batch_no, confirmed, confirmed_at FROM batch "
            "WHERE replacement_id = ? ORDER BY batch_no",
            (rp_id,),
        ):
            cells = [
                [row["r"], row["c"]]
                for row in conn.execute(
                    "SELECT r, c FROM batch_cell "
                    "WHERE replacement_id = ? AND batch_no = ? ORDER BY r, c",
                    (rp_id, b["batch_no"]),
                )
            ]
            out.append({
                "replacement_id": rp_id,
                "batch_no": b["batch_no"],
                "confirmed": bool(b["confirmed"]),
                "confirmed_at": b["confirmed_at"],
                "cells": cells,
            })
        return out


# --------------------------------------------------------------------------- #
# 凭批号确认：只改批次状态（改动记录数与格子数无关）；重复确认幂等
# --------------------------------------------------------------------------- #
def confirm_batch(
    conn: sqlite3.Connection, rp_id: int, batch_no: int
) -> Dict[str, Any]:
    with transaction(conn):
        rp = conn.execute(
            "SELECT id, month, status FROM replacement WHERE id = ?", (rp_id,)
        ).fetchone()
        if not rp:
            raise ServiceError(404, f"replacement {rp_id} not found")

        batch = conn.execute(
            "SELECT confirmed FROM batch WHERE replacement_id = ? AND batch_no = ?",
            (rp_id, batch_no),
        ).fetchone()
        if not batch:
            raise ServiceError(
                404, f"batch {batch_no} of replacement {rp_id} not found"
            )

        # 重复确认同一批不重复写入
        if batch["confirmed"]:
            return {
                "replacement_id": rp_id,
                "batch_no": batch_no,
                "already_confirmed": True,
                "written": 0,
                "status": rp["status"],
            }

        # 本批仍待生效的格子数（兼容旧库逐格写过 confirmed 的情形）。
        # 只做一次计数；随后只更新 batch（末批再更新 replacement），
        # 写入记录数恒为 1~2 条，与本批格子数无关。
        written = conn.execute(
            "SELECT COUNT(*) n FROM batch_cell bc "
            "LEFT JOIN replacement_cell rc "
            "  ON rc.replacement_id = bc.replacement_id "
            " AND rc.r = bc.r AND rc.c = bc.c "
            "WHERE bc.replacement_id = ? AND bc.batch_no = ? "
            "  AND COALESCE(rc.confirmed, 0) = 0",
            (rp_id, batch_no),
        ).fetchone()["n"]

        # 现用表不逐格改写：已确认批次的格子由 batch_cell + batch.confirmed
        # 直接对外生效，未确认批次的格子继续取该申请的 old_value。
        conn.execute(
            "UPDATE batch SET confirmed = 1, confirmed_at = datetime('now') "
            "WHERE replacement_id = ? AND batch_no = ?",
            (rp_id, batch_no),
        )

        remaining = conn.execute(
            "SELECT COUNT(*) n FROM batch "
            "WHERE replacement_id = ? AND confirmed = 0",
            (rp_id,),
        ).fetchone()["n"]

        status = rp["status"]
        if remaining == 0:
            conn.execute(
                "UPDATE replacement SET status = 'confirmed', "
                "confirmed_at = datetime('now') WHERE id = ?",
                (rp_id,),
            )
            status = "confirmed"

        return {
            "replacement_id": rp_id,
            "batch_no": batch_no,
            "already_confirmed": False,
            "written": written,
            "remaining_batches": remaining,
            "status": status,
        }
