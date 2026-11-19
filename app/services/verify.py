"""盖印复验：落标、追索、缺口三处共用同一套判定。

一条盖印只有同时满足下面几项才算可信：
1. 盖印时留有窗界快照，业务日落在该窗界内；
2. 盖印时刻不晚于窗界截止日（窗外「补印」不算）；
3. 盖印所用版式，其生效窗覆盖该业务日（不能拿未生效版顶账）。
"""


def _template_version_effective(
    conn, table_name: str, biz_date: str, version_no: int | None
) -> bool:
    if not version_no:
        return False
    row = conn.execute(
        "SELECT 1 FROM watermark_templates "
        "WHERE table_name=? AND version_no=? "
        "AND effective_from<=? AND effective_to>=? LIMIT 1",
        (table_name, version_no, biz_date, biz_date),
    ).fetchone()
    return row is not None


def verify_stamp(conn, row) -> dict:
    """返回 {state, reasons, ok}；state 取 pending / valid / invalid。"""
    status = row["status"]
    if status != "已盖印":
        return {
            "state": "pending" if status == "待盖印" else status,
            "ok": status != "已作废",
            "valid": False,
            "reasons": [],
        }

    reasons: list[str] = []
    biz = str(row["biz_date"] or "")[:10]
    start = row["stamped_window_start"]
    end = row["stamped_window_end"]

    if not start or not end:
        reasons.append("缺少盖印时窗界快照")
    else:
        if not (start <= biz <= end):
            reasons.append("业务日不在盖印时窗界内")
        stamped_at = str(row["stamped_at"] or "")[:10]
        if stamped_at and stamped_at > end:
            reasons.append("盖印发生在窗界过期后")

    if not _template_version_effective(
        conn, row["table_name"], biz, row["template_version"]
    ):
        reasons.append("盖印版式在该业务日未生效")

    return {
        "state": "invalid" if reasons else "valid",
        "ok": not reasons,
        "valid": not reasons,
        "reasons": reasons,
    }
