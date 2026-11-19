from app.db import get_db


def list_templates(
    db_path: str | None = None,
    status: str | None = None,
    table_name: str | None = None,
) -> list[dict]:
    with get_db(db_path) as conn:
        sql = "SELECT * FROM watermark_templates"
        clauses = []
        args: list = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if table_name:
            clauses.append("table_name=?")
            args.append(table_name)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY table_name, version_no"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
