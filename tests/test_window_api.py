import os
import threading

import pytest

from app.db import init_db
from app.services.errors import ServiceError
from app.services.windows import update_window


@pytest.mark.asyncio
async def test_page_reads_share_one_window(authed):
    meta = await (await authed.get("/api/meta")).json()
    win = await (await authed.get("/api/window")).json()
    assert meta["window"]["window_start"] == win["window"]["window_start"]
    assert meta["window"]["window_end"] == win["window"]["window_end"]
    assert meta["window"]["revision"] == win["window"]["revision"]
    assert meta["window"]["status"] == win["window"]["status"]


@pytest.mark.asyncio
async def test_expired_window_rejects_stamp_with_chinese_message(authed):
    r = await authed.post(
        "/api/window",
        json={"window_start": "2026-11-01", "window_end": "2026-11-15"},
    )
    assert r.status == 200
    assert (await r.json())["window"]["expired"] is True

    batches = (await (await authed.get("/api/batches")).json())["items"]
    target = next(b for b in batches if b["status"] == "待盖印")
    resp = await authed.post(
        f"/api/batches/{target['id']}/stamp",
        json={"watermark_value": "WM-API-1"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["ok"] is False
    assert "窗界已过期" in body["error"]

    # 窗外静默成功不可接受：详情仍是待盖印。
    detail = await (await authed.get(f"/api/batches/{target['id']}")).json()
    assert detail["batch"]["status"] == "待盖印"


@pytest.mark.asyncio
async def test_same_source_for_allow_and_verdict(authed):
    win = (await (await authed.get("/api/window")).json())["window"]
    batches = (await (await authed.get("/api/batches")).json())["items"]
    target = next(b for b in batches if b["status"] == "待盖印")
    in_window = win["window_start"] <= target["biz_date"] <= win["window_end"]
    allowed = win["can_stamp"] and in_window

    resp = await authed.post(
        f"/api/batches/{target['id']}/stamp",
        json={"watermark_value": "WM-API-2"},
    )
    assert (resp.status == 200) is allowed
    if not allowed:
        assert "窗外" in (await resp.json())["error"] or "窗界" in (
            await resp.json()
        )["error"]


@pytest.mark.asyncio
async def test_stamp_page_chip_matches_backend_verdict(authed):
    await authed.post(
        "/api/window", json={"window_start": "2026-11-01", "window_end": "2026-11-30"}
    )
    batches = (await (await authed.get("/api/batches")).json())["items"]
    target = next(b for b in batches if b["status"] == "待盖印")
    resp = await authed.post(
        f"/api/batches/{target['id']}/stamp", json={"watermark_value": "WM-API-3"}
    )
    assert resp.status == 200
    batch = (await resp.json())["batch"]
    assert batch["stamp_state"] == "valid"
    assert batch["stamped_window_start"] == "2026-11-01"
    assert batch["stamped_window_end"] == "2026-11-30"


@pytest.mark.asyncio
async def test_trace_and_gap_refuse_out_of_window_print(authed):
    await authed.post(
        "/api/window", json={"window_start": "2026-11-01", "window_end": "2026-11-30"}
    )
    batches = (await (await authed.get("/api/batches")).json())["items"]
    target = next(b for b in batches if b["status"] == "待盖印")
    stamp = await authed.post(
        f"/api/batches/{target['id']}/stamp", json={"watermark_value": "WM-API-4"}
    )
    assert stamp.status == 200

    # 落标合法，盖印快照就是裁定依据；再收窗不应追溯否定这枚印。
    trace_ok = await authed.post("/api/trace", json={"watermark_value": "WM-API-4"})
    assert trace_ok.status == 200

    # 注入「窗界过期后补印」的脏数据（旧 bug 的产物），顺藤查与缺口对账都必须拒吃。
    import sqlite3

    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    conn.execute(
        "UPDATE run_batches SET stamped_window_end='2026-11-10', "
        "stamped_at='2026-11-18 09:00:00' WHERE id=?",
        (target["id"],),
    )
    conn.commit()
    conn.close()

    trace = await authed.post("/api/trace", json={"watermark_value": "WM-API-4"})
    assert trace.status == 409
    assert "复验未通过" in (await trace.json())["error"]

    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-18"})
    assert gap.status == 200
    summary = (await gap.json())["report"]["summary"]
    assert "窗界外/失效盖印 1" in summary


@pytest.mark.asyncio
async def test_window_update_rejects_stale_revision(authed):
    first = await authed.post(
        "/api/window", json={"window_start": "2026-11-01", "window_end": "2026-11-30"}
    )
    rev = (await first.json())["window"]["revision"]
    stale = await authed.post(
        "/api/window",
        json={
            "window_start": "2026-12-01",
            "window_end": "2026-12-31",
            "expected_revision": rev - 1,
        },
    )
    assert stale.status == 409
    assert "窗界已变更" in (await stale.json())["error"]


def test_concurrent_window_updates_keep_single_row(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    init_db(str(path))

    results: list[int] = []
    errors: list[Exception] = []

    def worker(end: str) -> None:
        try:
            w = update_window("2026-11-01", end, actor="scheduler", db_path=str(path))
            results.append(w["revision"])
        except Exception as exc:  # pragma: no cover - 锁冲突路径
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"2026-11-{day:02d}",))
        for day in range(16, 22)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # 六次改窗各自拿到递增且互不重复的版本号（起点是初始化窗界那一版）。
    assert len(set(results)) == len(threads)
    assert sorted(results) == list(range(min(results), min(results) + len(threads)))

    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) c FROM window_state").fetchone()["c"] == 1
    events = conn.execute("SELECT COUNT(*) c FROM window_events").fetchone()["c"]
    conn.close()
    # 初始化时记 1 条窗界事件，之后每次改窗各 1 条，一条不多一条不少。
    assert events == len(threads) + 1
