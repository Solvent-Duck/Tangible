"""Small read-only lookups the pages need that `services` does not expose.

Only call these after a `services` call has authorized the caller for the case in question.
"""

from typing import Any

from tangible.db.session import Conn


def borrowers(conn: Conn) -> list[dict[str, str]]:
    rows = conn.execute("SELECT id, name FROM borrower ORDER BY name, id").fetchall()
    return [{"id": str(i), "name": n} for i, n in rows]


def borrower_name(conn: Conn, borrower_id: str) -> str:
    row = conn.execute("SELECT name FROM borrower WHERE id = %s", (borrower_id,)).fetchone()
    return str(row[0]) if row else "Unknown borrower"


def analysts(conn: Conn) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT subject, COALESCE(display_name, subject) FROM app_user WHERE role = 'analyst' "
        "ORDER BY subject"
    ).fetchall()
    return [{"subject": s, "name": n} for s, n in rows]


def users(conn: Conn) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT subject, COALESCE(display_name, subject), role FROM app_user ORDER BY role, subject"
    ).fetchall()
    return [{"subject": s, "name": n, "role": r} for s, n, r in rows]


def assessment_by_case(conn: Conn, case_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not case_ids:
        return {}
    rows = conn.execute(
        "SELECT case_id, id, decision_band, suggested_limit FROM assessment "
        "WHERE case_id = ANY(%s::uuid[])",
        (case_ids,),
    ).fetchall()
    return {
        str(c): {"id": str(i), "band": b, "limit": None if lim is None else str(lim)}
        for c, i, b, lim in rows
    }


def case_metrics(conn: Conn, borrower_id: str, as_of: str) -> list[dict[str, Any]]:
    """The baseline metric values computed for the case's borrower at the case's as_of."""
    rows = conn.execute(
        "SELECT DISTINCT ON (metric_key, metric_version) metric_key, metric_version, value "
        "FROM metric_value WHERE borrower_id = %s AND as_of = %s "
        "ORDER BY metric_key, metric_version, input_hash",
        (borrower_id, as_of),
    ).fetchall()
    return [{"key": k, "version": v, "value": None if x is None else str(x)} for k, v, x in rows]


def late_uploads(conn: Conn, borrower_id: str, as_of: str) -> int:
    """Uploaded documents stored after the case's as_of: invisible to it until it is re-based."""
    row = conn.execute(
        "SELECT count(*) FROM evidence_pull WHERE borrower_id = %s "
        "AND source = 'document_upload' AND fetched_at > %s",
        (borrower_id, as_of),
    ).fetchone()
    return int(row[0]) if row else 0
