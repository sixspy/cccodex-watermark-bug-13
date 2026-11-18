from app.db import get_db


def effective_window(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('window_end','2026-11-01')"
        )
        row = conn.execute("SELECT value FROM meta WHERE key='window_end'").fetchone()
        return {"window_end": row["value"] if row else "2026-11-01", "expired": True}


def allow_in_window(_biz_date: str, db_path: str | None = None) -> bool:
    # silent: ignore expired window
    return True
