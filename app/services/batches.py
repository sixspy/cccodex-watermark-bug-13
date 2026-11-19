from app.db import get_db, write_db
from app.services.errors import ServiceError
from app.services.verify import verify_stamp
from app.services.windows import describe_window, ensure_stampable, evaluate_batch


def _select_template(conn, table_name: str, biz_date: str):
    """按业务日落在哪个生效窗选版；业务日未生效的版式不得顶上。"""
    return conn.execute(
        "SELECT * FROM watermark_templates "
        "WHERE table_name=? AND effective_from<=? AND effective_to>=? "
        "ORDER BY (status='现行') DESC, version_no DESC, id DESC LIMIT 1",
        (table_name, biz_date, biz_date),
    ).fetchone()


def _decorate(conn, row) -> dict:
    item = dict(row)
    verdict = verify_stamp(conn, row)
    item["stamp_state"] = verdict["state"]
    item["stamp_ok"] = verdict["ok"]
    item["stamp_reasons"] = verdict["reasons"]
    return item


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM run_batches ORDER BY id").fetchall()
        return [_decorate(conn, r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return _decorate(conn, row) if row else None


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    """落标一次裁定。

    窗界读数取「提交瞬间重读现行窗」：本次写事务内读一次窗界、裁定一次，
    不用页面上停留的旧读数，也不给窗外批次静默放行的机会。
    """
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")

    with write_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)

        window = describe_window(conn=conn)
        ensure_stampable(batch["biz_date"], window)

        tpl = _select_template(conn, batch["table_name"], batch["biz_date"])
        if not tpl:
            raise ServiceError("无覆盖该业务日的现行水印模板", 409)

        # 条件更新：并发盖印时只有一个能改到「待盖印」这一行。
        cur = conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "template_version=?, stamped_window_start=?, stamped_window_end=?, "
            "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
            (
                value,
                tpl["version_no"],
                window["window_start"],
                window["window_end"],
                batch_id,
            ),
        )
        if cur.rowcount != 1:
            raise ServiceError("批次状态已变更，请刷新后重试", 409)

        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        item = _decorate(conn, row)
        item["window"] = window
        item["verdict"] = evaluate_batch(row["biz_date"], window)
        return item
