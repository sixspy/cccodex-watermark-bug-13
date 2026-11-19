import json

from app.db import get_db
from app.services.verify import verify_stamp
from app.services.windows import describe_window


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM gap_reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    """缺口对账：口子用与落标同一套复验，窗外/未生效版的新印不算已盖印。

    口径：
    - 应盖印 = 业务日落在对账日内、且业务日在现行窗界内的批次；
    - 已盖印 = 复验通过（窗界快照 + 版式生效）的批次；
    - 窗外批次不计缺口，单列「窗外」计数，避免把窗界差额混进缺口账。
    """
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path) as conn:
        window = describe_window(conn=conn)
        rows = conn.execute(
            "SELECT * FROM run_batches WHERE biz_date<=? AND status!='已作废' ORDER BY id",
            (day,),
        ).fetchall()

        expected = 0
        stamped = 0
        outside = 0
        invalid = 0
        for row in rows:
            biz = str(row["biz_date"])[:10]
            if not (window["window_start"] <= biz <= window["window_end"]):
                outside += 1
                continue
            expected += 1
            verdict = verify_stamp(conn, row)
            if verdict["valid"]:
                stamped += 1
            elif row["status"] == "已盖印":
                invalid += 1

        gap = expected - stamped
        snapshot = {
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "window_revision": window["revision"],
            "window_status": window["status"],
        }
        summary = (
            f"截至 {day}（窗界 {window['window_start']}~{window['window_end']}"
            f" · {window['status']}）：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}"
        )
        if invalid:
            summary += f"，窗界外/失效盖印 {invalid}"
        if outside:
            summary += f"，窗外批次 {outside}（不计缺口）"

        cur = conn.execute(
            "INSERT INTO gap_reports"
            "(as_of_date, expected_count, stamped_count, gap_count, window_snapshot, summary) "
            "VALUES (?,?,?,?,?,?)",
            (
                day,
                expected,
                stamped,
                gap,
                json.dumps(snapshot, ensure_ascii=False),
                summary,
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
