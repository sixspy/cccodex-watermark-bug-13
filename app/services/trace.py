from app.db import get_db
from app.services.errors import ServiceError
from app.services.verify import verify_stamp
from app.services.windows import describe_window


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    """按水印值追索下游；复验一轮，窗外新印不再被追索吃进来。"""
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError("未找到该水印值的盖印批次", 404)

        window = describe_window(conn=conn)
        verified_sources = []
        rejected = []
        for s in sources:
            verdict = verify_stamp(conn, s)
            item = dict(s)
            item["stamp_state"] = verdict["state"]
            item["stamp_ok"] = verdict["ok"]
            item["stamp_reasons"] = verdict["reasons"]
            if verdict["valid"]:
                verified_sources.append(item)
            else:
                rejected.append(item)

        if not verified_sources:
            raise ServiceError("水印值的盖印批次复验未通过", 409)

        codes = [s["run_code"] for s in verified_sources]
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()
        down_codes = sorted({e["downstream_run_code"] for e in edges})

        downstream = []
        rejected_downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            for r in conn.execute(
                f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                down_codes,
            ).fetchall():
                verdict = verify_stamp(conn, r)
                item = dict(r)
                item["stamp_state"] = verdict["state"]
                item["stamp_ok"] = verdict["ok"]
                item["stamp_reasons"] = verdict["reasons"]
                if verdict["state"] == "已盖印" and not verdict["valid"]:
                    rejected_downstream.append(item)
                else:
                    downstream.append(item)

        return {
            "watermark_value": value,
            "window": window,
            "sources": verified_sources,
            "rejected_sources": rejected,
            "edges": [dict(e) for e in edges if e["upstream_run_code"] in codes],
            "downstream": downstream,
            "rejected_downstream": rejected_downstream,
        }
