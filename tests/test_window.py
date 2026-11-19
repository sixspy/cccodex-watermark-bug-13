import sqlite3

import pytest

from app.db import init_db
from app.services.batches import get_batch, list_batches, stamp_batch
from app.services.errors import ServiceError
from app.services.gaps import run_gap_report
from app.services.trace import trace_downstream
from app.services.windows import (
    describe_window,
    effective_window,
    update_window,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "unit.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("LAB_TODAY", "2026-11-18")
    init_db(str(path))
    yield str(path)


def test_window_is_single_source(db):
    w = describe_window(db_path=db)
    assert w["window_start"] <= w["window_end"]
    assert w["can_stamp"] is (not w["expired"] and not w["not_started"])
    assert w["source"] in {"window_state", "seed", "default"}


def test_seed_window_is_expired_out_of_the_box(db):
    """出厂窗界沿用原始种子，装起来就是「已过期」——这正是要复现的现场。"""
    w = describe_window(db_path=db)
    assert w["window_end"] == "2026-11-01"
    assert w["expired"] is True
    assert w["can_stamp"] is False
    # 窗外批次不得静默落标。
    with pytest.raises(ServiceError) as err:
        stamp_batch(1, "WM-SEED", db_path=db)
    assert err.value.status_code == 409
    assert get_batch(1, db)["status"] == "待盖印"


def test_expired_window_blocks_stamp(db):
    update_window("2026-11-01", "2026-11-15", db_path=db)
    w = effective_window(db)
    assert w["expired"] is True
    assert w["can_stamp"] is False
    with pytest.raises(ServiceError) as err:
        stamp_batch(1, "WM-X", db_path=db)
    assert err.value.status_code == 409
    assert "窗界已过期" in err.value.message
    # 窗外静默成功是不允许的：库里不能留下这枚印。
    assert get_batch(1, db)["status"] == "待盖印"


def test_not_started_window_blocks_stamp(db):
    update_window("2026-12-01", "2026-12-31", db_path=db)
    w = effective_window(db)
    assert w["not_started"] is True
    with pytest.raises(ServiceError) as err:
        stamp_batch(1, "WM-X", db_path=db)
    assert "窗界未生效" in err.value.message


def test_stamp_records_window_snapshot_and_version(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    batch = stamp_batch(1, "WM-OK-1", db_path=db)
    assert batch["status"] == "已盖印"
    assert batch["stamped_window_start"] == "2026-11-01"
    assert batch["stamped_window_end"] == "2026-11-30"
    # 业务日 11-14 落在 v1 生效窗内，不能被 12 月才生效的 v2 顶上。
    assert batch["template_version"] == 1
    assert batch["stamp_state"] == "valid"


def test_template_version_must_cover_biz_date(db):
    update_window("2026-11-01", "2026-12-31", db_path=db)
    batch = stamp_batch(1, "WM-OK-1", db_path=db)
    assert batch["template_version"] == 1


def test_stamp_after_expiry_is_rejected_and_not_silent(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    stamp_batch(1, "WM-OK-1", db_path=db)
    update_window("2026-11-01", "2026-11-10", db_path=db)
    with pytest.raises(ServiceError) as err:
        stamp_batch(2, "WM-LATE", db_path=db)
    assert err.value.status_code == 409
    assert get_batch(2, db)["status"] == "待盖印"


def test_trace_rejects_dirty_out_of_window_stamp(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    stamp_batch(2, "WM-OK-2", db_path=db)

    # 模拟旧 bug 留下的脏数据：窗界过期后补写的一枚印。
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE run_batches SET stamped_window_end='2026-11-15', "
        "stamped_at='2026-11-18 09:00:00' WHERE id=2"
    )
    conn.commit()
    conn.close()

    with pytest.raises(ServiceError) as err:
        trace_downstream("WM-OK-2", db_path=db)
    assert err.value.status_code == 409


def test_trace_only_returns_verified_sources(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    stamp_batch(1, "WM-SHARED", db_path=db)
    stamp_batch(2, "WM-SHARED", db_path=db)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE run_batches SET stamped_window_end='2026-11-15' WHERE id=2"
    )
    conn.commit()
    conn.close()

    result = trace_downstream("WM-SHARED", db_path=db)
    codes = [s["run_code"] for s in result["sources"]]
    assert codes == ["ord-20261114-01"]
    assert [r["run_code"] for r in result["rejected_sources"]] == ["pay-20261114-01"]


def test_gap_report_excludes_out_of_window_and_invalid(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    stamp_batch(1, "WM-OK-1", db_path=db)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE run_batches SET status='已盖印', watermark_value='WM-DIRTY', "
        "template_version=1, stamped_window_start='2026-11-01', "
        "stamped_window_end='2026-11-15', stamped_at='2026-11-18 09:00:00' WHERE id=2"
    )
    conn.commit()
    conn.close()

    report = run_gap_report("2026-11-18", db_path=db)
    # 应盖印：4 个业务日在窗内的批次；已盖印只认复验通过的 2 个（1 个新印 + 1 个种子印）。
    assert report["expected_count"] == 4
    assert report["stamped_count"] == 2
    assert report["gap_count"] == 2
    assert "窗界外/失效盖印 1" in report["summary"]


def test_gap_report_counts_outside_separately(db):
    update_window("2026-11-01", "2026-11-10", db_path=db)
    report = run_gap_report("2026-11-18", db_path=db)
    assert report["expected_count"] == 0
    assert "窗外批次" in report["summary"]


def test_update_window_revision_conflict(db):
    w1 = update_window("2026-11-01", "2026-11-30", db_path=db)
    assert w1["revision"] >= 1
    with pytest.raises(ServiceError) as err:
        update_window(
            "2026-12-01",
            "2026-12-31",
            expected_revision=w1["revision"] - 1,
            db_path=db,
        )
    assert err.value.status_code == 409
    assert "窗界已变更" in err.value.message


def test_update_window_uses_exclusive_transaction(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    conn = sqlite3.connect(db)
    # 排他事务期间，另一连接的写必须等锁，不会双写窗界行。
    conn.execute("BEGIN IMMEDIATE")
    other = sqlite3.connect(db, timeout=0.2)
    other.execute("PRAGMA busy_timeout = 100")
    with pytest.raises(sqlite3.OperationalError):
        other.execute("BEGIN IMMEDIATE")
    conn.rollback()
    conn.close()
    other.close()


def test_window_update_converges_single_row(db):
    update_window("2026-11-01", "2026-11-30", db_path=db)
    update_window("2026-11-01", "2026-12-15", db_path=db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM window_state").fetchall()
    meta = conn.execute(
        "SELECT COUNT(*) c FROM meta WHERE key LIKE 'window%'"
    ).fetchone()["c"]
    conn.close()
    assert len(rows) == 1
    assert meta == 0
    assert rows[0]["end_date"] == "2026-12-15"


def test_list_batches_marks_invalid_stamps(db):
    batches = list_batches(db_path=db)
    # 种子里的历史盖印应被复验为有效。
    seeded = next(b for b in batches if b["run_code"] == "ord-20261113-01")
    assert seeded["stamp_state"] == "valid"
