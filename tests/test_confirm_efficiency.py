"""确认/受理的写入规模、现用表读取范围，以及旧库部分确认的兼容性。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    db.init_db(str(db_file))
    yield TestClient(app)


ROWS = ["area-a", "area-b", "area-c"]
COLS = ["cargo-x", "cargo-y", "cargo-z"]
BASE = [
    [10, 5, 5],
    [4, 8, 8],
    [6, 7, 7],
]
# 4 格 4 环：A/x +1, A/y -1, B/x -1, B/y +1
FOUR_CYCLE = [
    [11, 4, 5],
    [3, 9, 8],
    [6, 7, 7],
]
# 6 格 6 环：A/x +1,A/z -1; B/x -1,B/y +1; C/y -1,C/z +1
SIX_CYCLE = [
    [11, 5, 4],
    [3, 9, 8],
    [6, 6, 8],
]


def _register(client, month="2026-11", matrix=None):
    r = client.post(
        f"/months/{month}/register",
        json={"rows": ROWS, "cols": COLS, "matrix": matrix or BASE},
    )
    assert r.status_code == 201, r.text
    return r


def _submit(client, matrix, month="2026-11"):
    r = client.post(
        f"/months/{month}/replacements",
        json={"rows": ROWS, "cols": COLS, "matrix": matrix},
    )
    assert r.status_code == 201, r.text
    return r.json()


class _Recorder:
    def __init__(self):
        self.writes = []   # [(op, table, rowcount)]
        self.reads = []    # [(sql, params)]


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    real_execute = sqlite3.Connection.execute
    real_executemany = sqlite3.Connection.executemany

    class RecConn(sqlite3.Connection):
        def execute(self, sql, params=()):
            cur = real_execute(self, sql, params)
            if isinstance(sql, str):
                words = sql.lstrip().split(None, 2)
                op = words[0].upper()
                if op in ("INSERT", "UPDATE", "DELETE"):
                    rec.writes.append((op, words[1], cur.rowcount))
                else:
                    rec.reads.append((sql, params))
            return cur

        def executemany(self, sql, seq):
            cur = real_executemany(self, sql, seq)
            words = sql.lstrip().split(None, 2)
            rec.writes.append((words[0].upper(), words[1], cur.rowcount))
            return cur

    def rec_connect(path=None):
        conn = sqlite3.connect(
            path or db.DB_PATH, timeout=30, factory=RecConn
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "connect", rec_connect)
    return rec


def _written_tables(rec):
    return {(op, table) for op, table, _ in rec.writes}


def test_confirm_write_count_independent_of_batch_size(client, recorder):
    """4 格批与 6 格批（含末批）确认时都只改批次状态，不逐格写。"""
    # 两个独立月份，各自从 BASE 出发：一个 4 环、一个 6 环
    _register(client, "2026-11", BASE)
    _register(client, "2026-12", BASE)

    four = _submit(client, FOUR_CYCLE, "2026-11")
    assert four["batch_count"] == 1 and len(four["batches"][0]["cells"]) == 4

    recorder.writes.clear()
    res4 = client.post(
        f"/replacements/{four['replacement_id']}/batches/1/confirm"
    ).json()
    writes4 = list(recorder.writes)
    assert res4["written"] == 4

    six = _submit(client, SIX_CYCLE, "2026-12")
    assert six["batch_count"] == 1 and len(six["batches"][0]["cells"]) == 6

    recorder.writes.clear()
    res6 = client.post(
        f"/replacements/{six['replacement_id']}/batches/1/confirm"
    ).json()
    writes6 = list(recorder.writes)
    assert res6["written"] == 6

    # 两次确认的改动记录数一致，且都只落在 batch / replacement 上
    assert len(writes4) == len(writes6)
    assert sum(rc for _, _, rc in writes4) == sum(rc for _, _, rc in writes6)
    for writes in (writes4, writes6):
        assert {(op, t) for op, t, _ in writes} == {
            ("UPDATE", "batch"), ("UPDATE", "replacement"),
        }
        assert all("live_cell" not in t and "replacement_cell" not in t
                   for _, t, _ in writes)


def test_non_last_batch_writes_constant_one_record(client, recorder):
    """非末批只改一行 batch；末批再多改一行 replacement，均与格数无关。"""
    rows = ["area-a", "area-b", "area-c"]
    cols = ["cargo-w", "cargo-x", "cargo-y", "cargo-z"]
    base = [
        [10, 5, 5, 4],
        [4, 8, 8, 4],
        [6, 7, 7, 4],
    ]
    new = [
        [9, 6, 6, 3],
        [5, 7, 8, 4],
        [6, 7, 6, 5],
    ]
    assert client.post(
        "/months/2026-11/register",
        json={"rows": rows, "cols": cols, "matrix": base},
    ).status_code == 201
    body = client.post(
        "/months/2026-11/replacements",
        json={"rows": rows, "cols": cols, "matrix": new},
    ).json()
    rp_id = body["replacement_id"]
    assert body["batch_count"] == 2

    recorder.writes.clear()
    client.post(f"/replacements/{rp_id}/batches/2/confirm")
    first = list(recorder.writes)
    assert {(op, t) for op, t, _ in first} == {("UPDATE", "batch")}
    assert sum(rc for _, _, rc in first) == 1

    recorder.writes.clear()
    client.post(f"/replacements/{rp_id}/batches/1/confirm")
    last = list(recorder.writes)
    assert {(op, t) for op, t, _ in last} == {
        ("UPDATE", "batch"), ("UPDATE", "replacement"),
    }
    assert sum(rc for _, _, rc in last) == 2


def test_next_replacement_validated_against_effective_table(client):
    """上一笔确认后 live_cell 未逐格改写，下一笔仍按实际已生效底表校验切批。"""
    _register(client)
    first = _submit(client, FOUR_CYCLE)
    assert client.post(
        f"/replacements/{first['replacement_id']}/batches/1/confirm"
    ).json()["status"] == "confirmed"

    # 相对实际生效底表（FOUR_CYCLE），这正是把 4 环反向改回去：
    # 若错误地拿 live_cell（BASE）比较，会被判成“与原表相同”。
    second = _submit(client, BASE)
    assert second["changed_cells"] == 4
    assert second["batch_count"] == 1
    assert {tuple(c) for c in second["batches"][0]["cells"]} == {
        ("area-a", "cargo-x"), ("area-a", "cargo-y"),
        ("area-b", "cargo-x"), ("area-b", "cargo-y"),
    }
    client.post(f"/replacements/{second['replacement_id']}/batches/1/confirm")
    assert client.get("/months/2026-11/table").json()["matrix"] == BASE


def test_confirm_reports_applied_cells_then_zero(client):
    _register(client)
    body = _submit(client, SIX_CYCLE)
    rp_id = body["replacement_id"]

    assert client.post(
        f"/replacements/{rp_id}/batches/1/confirm"
    ).json()["written"] == 6
    again = client.post(f"/replacements/{rp_id}/batches/1/confirm").json()
    assert again["written"] == 0 and again["already_confirmed"] is True


def test_live_table_reads_only_latest_replacement(client, recorder):
    """查现用表只读最近一笔受理换发的格明细，不回溯更早的换发。"""
    rows = ["area-a", "area-b", "area-c"]
    cols = ["cargo-w", "cargo-x", "cargo-y", "cargo-z"]
    base = [
        [10, 5, 5, 4],
        [4, 8, 8, 4],
        [6, 7, 7, 4],
    ]
    # 第一笔：a/b 在 x/y 上的 4 环，确认完毕后成为实际生效底表
    first_table = [
        [11, 4, 5, 4],
        [3, 9, 8, 4],
        [6, 7, 7, 4],
    ]
    # 第二笔：在第一笔结果上再叠两条互不相交的 4 环（共 2 批）
    # 环1: a/w +1, a/x -1, b/w -1, b/x +1
    # 环2: b/y +1, b/z -1, c/y -1, c/z +1
    second_table = [
        [12, 3, 5, 4],
        [2, 10, 9, 3],
        [6, 7, 6, 5],
    ]
    assert client.post(
        "/months/2026-11/register",
        json={"rows": rows, "cols": cols, "matrix": base},
    ).status_code == 201
    first = client.post(
        "/months/2026-11/replacements",
        json={"rows": rows, "cols": cols, "matrix": first_table},
    ).json()
    assert client.post(
        f"/replacements/{first['replacement_id']}/batches/1/confirm"
    ).json()["status"] == "confirmed"

    second = client.post(
        "/months/2026-11/replacements",
        json={"rows": rows, "cols": cols, "matrix": second_table},
    ).json()
    assert second["batch_count"] == 2
    # 只确认其中一批，保持 pending，现用表需要逐格遮蔽
    client.post(f"/replacements/{second['replacement_id']}/batches/2/confirm")

    recorder.reads.clear()
    table = client.get("/months/2026-11/table")
    assert table.status_code == 200
    rp_cell_reads = [
        (sql, params) for sql, params in recorder.reads
        if "replacement_cell" in sql.lower()
    ]
    assert rp_cell_reads
    # 每条格明细查询都按最近一笔受理换发过滤，参数是第二笔的 id
    for sql, params in rp_cell_reads:
        assert "replacement_id = ?" in sql
        assert params and params[0] == second["replacement_id"]
    # 有受理换发时不再回读登记时的 live_cell
    assert not any("live_cell" in sql.lower() for sql, _ in recorder.reads)

    # 直接篡改更早一笔的格明细，不应影响现用表
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute(
        "UPDATE replacement_cell SET old_value = 999, new_value = 999 "
        "WHERE replacement_id = ?",
        (first["replacement_id"],),
    )
    conn.commit()
    conn.close()

    masked = client.get("/months/2026-11/table").json()["matrix"]
    confirmed_cells = {
        (r, c)
        for b in second["batches"] if b["batch_no"] == 2
        for r, c in b["cells"]
    }
    for i, rn in enumerate(rows):
        for j, cn in enumerate(cols):
            expected = (
                second_table[i][j] if (rn, cn) in confirmed_cells
                else first_table[i][j]
            )
            assert masked[i][j] == expected
            assert masked[i][j] != 999


def test_legacy_partially_confirmed_application_can_continue(client):
    """旧库中只确认了部分批次的申请，仍可凭原批号继续确认剩下的批次。"""
    rows = ["area-a", "area-b", "area-c"]
    cols = ["cargo-w", "cargo-x", "cargo-y", "cargo-z"]
    base = [
        [10, 5, 5, 4],
        [4, 8, 8, 4],
        [6, 7, 7, 4],
    ]
    new = [
        [9, 6, 6, 3],
        [5, 7, 8, 4],
        [6, 7, 6, 5],
    ]
    assert client.post(
        "/months/2026-11/register",
        json={"rows": rows, "cols": cols, "matrix": base},
    ).status_code == 201
    body = client.post(
        "/months/2026-11/replacements",
        json={"rows": rows, "cols": cols, "matrix": new},
    ).json()
    rp_id = body["replacement_id"]

    # 新代码确认第 2 批后，再补上旧实现会留下的逐格标记与 live_cell 更新
    client.post(f"/replacements/{rp_id}/batches/2/confirm")
    conn = sqlite3.connect(db.DB_PATH)
    batch2_cells = [
        (r, c)
        for b in body["batches"] if b["batch_no"] == 2
        for r, c in b["cells"]
    ]
    conn.executemany(
        "UPDATE replacement_cell SET confirmed = 1 "
        "WHERE replacement_id = ? AND r = ? AND c = ?",
        [(rp_id, r, c) for r, c in batch2_cells],
    )
    conn.executemany(
        "UPDATE live_cell SET value = ? WHERE month = '2026-11' AND r = ? AND c = ?",
        [(new[rows.index(r)][cols.index(c)], r, c) for r, c in batch2_cells],
    )
    conn.commit()
    conn.close()

    # 旧申请仍 pending，凭原批号 1 继续确认
    assert client.get(f"/replacements/{rp_id}").json()["status"] == "pending"
    res = client.post(f"/replacements/{rp_id}/batches/1/confirm").json()
    assert res["status"] == "confirmed"
    assert res["written"] == 4
    assert client.get("/months/2026-11/table").json()["matrix"] == new
    # 重复确认旧批号仍幂等
    assert client.post(
        f"/replacements/{rp_id}/batches/1/confirm"
    ).json()["written"] == 0


def test_confirm_failure_rolls_back_batch_state(client, monkeypatch):
    """末批在 batch 已改、replacement 尚未改时失败，两者都回到失败前状态。"""
    _register(client)
    body = _submit(client, FOUR_CYCLE)
    rp_id = body["replacement_id"]

    real_execute = sqlite3.Connection.execute
    state = {"armed": True}

    class FlakyConn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if (
                state["armed"]
                and isinstance(sql, str)
                and sql.lstrip().startswith("UPDATE replacement")
            ):
                raise sqlite3.OperationalError("simulated disk write failure")
            return real_execute(self, sql, params)

    def flaky_connect(path=None):
        conn = sqlite3.connect(
            path or db.DB_PATH, timeout=30, factory=FlakyConn
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "connect", flaky_connect)
    assert client.post(
        f"/replacements/{rp_id}/batches/1/confirm"
    ).status_code == 500
    state["armed"] = False

    # 批次仍显示未确认，申请仍 pending，现用表仍是原值
    batch = client.get(f"/replacements/{rp_id}/batches").json()[0]
    assert batch["confirmed"] is False
    assert client.get(f"/replacements/{rp_id}").json()["status"] == "pending"
    assert client.get("/months/2026-11/table").json()["matrix"] == BASE


def test_create_failure_leaves_no_partial_application(client, monkeypatch):
    """受理写入失败时不留半张批单，现用表保持原状，之后可重试。"""
    _register(client)
    real_execute = sqlite3.Connection.execute
    state = {"armed": True}

    class FlakyConn(sqlite3.Connection):
        def execute(self, sql, params=()):
            if (
                state["armed"]
                and isinstance(sql, str)
                and sql.lstrip().startswith("INSERT INTO batch(")
            ):
                raise sqlite3.OperationalError("simulated disk write failure")
            return real_execute(self, sql, params)

    def flaky_connect(path=None):
        conn = sqlite3.connect(
            path or db.DB_PATH, timeout=30, factory=FlakyConn
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "connect", flaky_connect)
    r = client.post(
        "/months/2026-11/replacements",
        json={"rows": ROWS, "cols": COLS, "matrix": FOUR_CYCLE},
    )
    assert r.status_code == 500

    conn = sqlite3.connect(db.DB_PATH)
    n_rp = conn.execute("SELECT COUNT(*) FROM replacement").fetchone()[0]
    n_cell = conn.execute("SELECT COUNT(*) FROM replacement_cell").fetchone()[0]
    conn.close()
    assert n_rp == 0 and n_cell == 0
    assert client.get("/months/2026-11/table").json()["matrix"] == BASE

    # 恢复后可正常受理
    monkeypatch.setattr(db, "connect", db.connect)
    state["armed"] = False
    body = _submit(client, FOUR_CYCLE)
    assert body["batch_count"] == 1
