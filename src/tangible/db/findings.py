"""Persisting findings. Rule checks and the agent's record_finding both write through here.

Findings are inserted once; review state lives in underwriter_action, never in finding.status.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from tangible.checks.registry import FindingDraft
from tangible.db.session import Conn

Origin = Literal["rule", "model"]


@dataclass(frozen=True)
class StoredFinding:
    id: str
    draft: FindingDraft
    origin: Origin
    case_id: str | None
    tool_call_ids: tuple[str, ...]
    verified: bool | None


def insert_finding(
    conn: Conn,
    tenant_id: str,
    borrower_id: str,
    draft: FindingDraft,
    *,
    origin: Origin = "rule",
    case_id: str | None = None,
    tool_call_ids: Sequence[str] = (),
    verified: bool | None = None,
) -> str:
    """Insert one finding. `facts` are stored inside evidence_refs' sibling jsonb as
    {"refs": [...], "facts": {...}} so nothing the rule saw is lost."""
    row = conn.execute(
        "INSERT INTO finding (tenant_id, borrower_id, check_key, check_version, origin, severity, "
        "direction, pillar, summary, evidence_refs, tool_call_ids, case_id, verified) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            tenant_id, borrower_id, draft.check_key, draft.check_version, origin, draft.severity,
            draft.direction, draft.pillar, draft.summary,
            json.dumps({"refs": list(draft.evidence_refs), "facts": dict(draft.facts)}),
            list(tool_call_ids), case_id, verified,
        ),
    ).fetchone()  # fmt: skip
    assert row
    return str(row[0])


def load_findings(conn: Conn, *, case_id: str) -> list[StoredFinding]:
    rows = conn.execute(
        "SELECT id, check_key, check_version, severity, pillar, summary, evidence_refs, "
        "direction, origin, case_id, tool_call_ids, verified FROM finding WHERE case_id = %s "
        "ORDER BY created_at, id",
        (case_id,),
    ).fetchall()
    out: list[StoredFinding] = []
    for r in rows:
        body = r[6] if isinstance(r[6], dict) else {"refs": r[6], "facts": {}}
        draft = FindingDraft(
            r[1], r[2], r[3], r[4], r[5], tuple(body.get("refs", ())), r[7] or "raises_risk",
            dict(body.get("facts", {})),
        )  # fmt: skip
        out.append(
            StoredFinding(str(r[0]), draft, r[8], str(r[9]) if r[9] else None, tuple(r[10]), r[11])
        )
    return out
