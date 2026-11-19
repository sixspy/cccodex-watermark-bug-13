"""窗界（生效窗）唯一裁定源。

窗界只落一行 `window_state`，读与写都走这里：
- 写窗界走 BEGIN IMMEDIATE 排他事务（双人调度不会双写窗界行）；
- 盖印、追索、缺口对账、页面四处读数都调 `describe_window` / `evaluate_batch`，
  保证「窗界」与「可落标」同源。

读数瞬间的取法（本实现取后者，并在改窗时保留前者所需的版本号）：
- 打开页时冻住窗界快照：页面拿到 `window` 全量读数，用于展示与预判；
- 提交瞬间重读现行窗：落标在同一个写事务里重读 `window_state` 一次裁定，
  页面停留期间的窗界漂移不会让旧读数放行；改窗则可用 `expected_revision`
  做乐观校验，窗界被别人改过就回 409 让刷新。
"""

import os
from datetime import date, datetime

from app.db import (
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    get_db,
    write_db,
)
from app.services.errors import ServiceError

SOURCE_LABELS = {
    "window_state": "窗界行 window_state",
    "meta": "旧库 meta 窗界键",
    "seed": "建库初始窗界",
    "default": "内置默认窗界",
}


def _today(today: date | None = None) -> date:
    if today is not None:
        return today
    raw = (os.environ.get("LAB_TODAY") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return date.today()


def _parse_day(value: str, field: str) -> date:
    text = str(value or "").strip()
    if not text:
        raise ServiceError(f"{field}不能为空")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ServiceError(f"{field}格式不合法") from None


def _snapshot(conn) -> dict:
    """窗界读数：`window_state` 为权威行，旧 `meta` 窗界键作为兼容覆写通道。

    两个来源都在同一函数里收敛成一份读数，调用方不各自取数；
    走 API 改窗会把 meta 键删掉，最终只剩一行 window_state。
    """
    row = conn.execute("SELECT * FROM window_state WHERE id=1").fetchone()
    if row is None:
        stored = {
            "window_start": DEFAULT_WINDOW_START,
            "window_end": DEFAULT_WINDOW_END,
            "revision": 0,
            "actor": None,
            "updated_at": None,
            "source": "default",
        }
    else:
        origin = (row["origin"] or "default").strip()
        stored = {
            "window_start": row["start_date"],
            "window_end": row["end_date"],
            "revision": int(row["revision"] or 0),
            "actor": row["actor"],
            "updated_at": row["updated_at"],
            "source": origin if origin in SOURCE_LABELS else "window_state",
        }

    # 兼容旧库/运维或验收脚本直改 meta 的窗界键：有值就以它为准，
    # 但读数仍从这里统一出，不形成第二套裁定口径。
    meta = {
        r["key"]: (r["value"] or "").strip()
        for r in conn.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('window_start','window_end','window_revision')"
        ).fetchall()
    }
    if not meta:
        return stored

    try:
        start = _parse_day(meta.get("window_start") or stored["window_start"], "窗界起始")
        end = _parse_day(meta.get("window_end") or stored["window_end"], "窗界截止")
    except ServiceError:
        return stored
    if start > end:
        return stored

    try:
        revision = max(0, int(meta.get("window_revision") or 0))
    except ValueError:
        revision = 0
    if "window_revision" not in meta:
        revision = stored["revision"]

    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "revision": revision,
        "actor": stored["actor"],
        "updated_at": stored["updated_at"],
        "source": "meta",
    }


def _describe(conn, today: date | None = None) -> dict:
    snap = _snapshot(conn)
    day = _today(today)
    start = date.fromisoformat(snap["window_start"][:10])
    end = date.fromisoformat(snap["window_end"][:10])
    expired = day > end
    not_started = day < start
    open_now = not expired and not not_started

    total_days = max(1, (end - start).days + 1)
    elapsed = min(total_days, max(0, (day - start).days + 1))
    progress = 100 if expired else round(elapsed * 100 / total_days)

    if expired:
        status = "窗界已过期"
    elif not_started:
        status = "窗界未生效"
    else:
        status = "窗界有效"

    history = [
        {
            "revision": int(r["revision"]),
            "window_start": r["window_start"],
            "window_end": r["window_end"],
            "actor": r["actor"],
            "changed_at": r["changed_at"],
        }
        for r in conn.execute(
            "SELECT * FROM window_events ORDER BY id DESC LIMIT 5"
        ).fetchall()
    ]

    return {
        "window_start": snap["window_start"],
        "window_end": snap["window_end"],
        "revision": snap["revision"],
        "source": snap["source"],
        "source_label": SOURCE_LABELS[snap["source"]],
        "updated_at": snap["updated_at"],
        "actor": snap["actor"],
        "today": day.isoformat(),
        "expired": expired,
        "not_started": not_started,
        "open": open_now,
        "can_stamp": open_now,
        "status": status,
        "days_total": total_days,
        "days_elapsed": elapsed,
        "days_left": 0 if expired else (end - day).days,
        "progress": progress,
        "history": history,
    }


def describe_window(
    db_path: str | None = None, conn=None, today: date | None = None
) -> dict:
    if conn is not None:
        return _describe(conn, today)
    with get_db(db_path) as own:
        return _describe(own, today)


def effective_window(db_path: str | None = None, today: date | None = None) -> dict:
    return describe_window(db_path=db_path, today=today)


def evaluate_batch(biz_date: str, window: dict) -> dict:
    """用同一份窗界快照裁定「这批能不能落标」。"""
    biz = str(biz_date or "")[:10]
    if window.get("expired"):
        return {
            "ok": False,
            "code": "window_expired",
            "reason": "窗界已过期，禁止落标",
            "biz_date": biz,
            "in_window": False,
        }
    if window.get("not_started"):
        return {
            "ok": False,
            "code": "window_not_started",
            "reason": "窗界未生效，禁止落标",
            "biz_date": biz,
            "in_window": False,
        }
    if not window.get("can_stamp", False):
        return {
            "ok": False,
            "code": "window_closed",
            "reason": "窗界不可落标",
            "biz_date": biz,
            "in_window": False,
        }
    in_window = window["window_start"] <= biz <= window["window_end"]
    if not in_window:
        return {
            "ok": False,
            "code": "outside_window",
            "reason": "批次业务日在窗外，禁止落标",
            "biz_date": biz,
            "in_window": False,
        }
    return {
        "ok": True,
        "code": "ok",
        "reason": "窗内可落标",
        "biz_date": biz,
        "in_window": True,
    }


def allow_in_window(
    biz_date: str, db_path: str | None = None, window: dict | None = None
) -> bool:
    snap = window if window is not None else effective_window(db_path)
    return bool(evaluate_batch(biz_date, snap)["ok"])


def ensure_stampable(biz_date: str, window: dict) -> None:
    verdict = evaluate_batch(biz_date, window)
    if not verdict["ok"]:
        raise ServiceError(verdict["reason"], 409)


def update_window(
    window_start: str,
    window_end: str,
    actor: str = "admin",
    expected_revision: int | str | None = None,
    db_path: str | None = None,
) -> dict:
    start = _parse_day(window_start, "窗界起始")
    end = _parse_day(window_end, "窗界截止")
    if start > end:
        raise ServiceError("窗界起止顺序不合法")
    actor_name = (actor or "admin").strip()[:40] or "admin"

    expected: int | None = None
    if expected_revision is not None and str(expected_revision).strip() != "":
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            raise ServiceError("窗界版本不合法") from None

    # BEGIN IMMEDIATE：先拿排他锁，再读旧版、写新版与事件行，杜绝双写/丢更新。
    with write_db(db_path) as conn:
        current = _snapshot(conn)
        if expected is not None and expected != current["revision"]:
            raise ServiceError("窗界已变更，请刷新后重试", 409)

        revision = current["revision"] + 1
        changed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO window_state"
            "(id, start_date, end_date, revision, actor, origin, updated_at) "
            "VALUES (1, ?, ?, ?, ?, 'manual', ?) "
            "ON CONFLICT(id) DO UPDATE SET start_date=excluded.start_date, "
            "end_date=excluded.end_date, revision=excluded.revision, "
            "actor=excluded.actor, origin=excluded.origin, updated_at=excluded.updated_at",
            (start.isoformat(), end.isoformat(), revision, actor_name, changed_at),
        )
        conn.execute(
            "INSERT INTO window_events"
            "(revision, window_start, window_end, actor, changed_at) "
            "VALUES (?,?,?,?,?)",
            (revision, start.isoformat(), end.isoformat(), actor_name, changed_at),
        )
        conn.execute(
            "DELETE FROM meta WHERE key IN ('window_start','window_end','window_revision')"
        )
        return _describe(conn)
