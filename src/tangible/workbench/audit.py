"""App audit log: every view, export, override and signature leaves an audit_log row."""

import json
from typing import Any
from uuid import UUID

from tangible.db.session import Conn
from tangible.workbench.auth import Principal


def record(
    conn: Conn,
    p: Principal,
    action: str,
    object_kind: str,
    object_id: str | UUID | None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, object_kind, object_id, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (
            p.tenant_id,
            p.subject,
            action,
            object_kind,
            str(object_id) if object_id else None,
            json.dumps(detail or {}, default=str),
        ),
    )
