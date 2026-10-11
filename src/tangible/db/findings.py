"""Persisting findings. Rule checks and the agent's record_finding both write through here.

Findings are inserted once; review state lives in underwriter_action, never in finding.status.
A corrected finding is a new row whose `replaces_id` points at the earlier one. The earlier row
stays (append-only) but is no longer "current"; everything that feeds the case's picture of itself
(the case page, sign counts, freeze and policy, the agent's list of existing findings) reads
current findings only, through `current_findings` / `replaced_by`.
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
    replaces_id: str | None = None


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
    replaces_id: str | None = None,
    finding_id: str | None = None,
) -> str:
    """Insert one finding. `facts` are stored inside evidence_refs' sibling jsonb as
    {"refs": [...], "facts": {...}} so nothing the rule saw is lost. `finding_id` lets a caller
    that already told someone the id (record_finding) insert under it."""
    row = conn.execute(
        "INSERT INTO finding (id, tenant_id, borrower_id, check_key, check_version, origin, "
        "severity, direction, pillar, summary, evidence_refs, tool_call_ids, case_id, verified, "
        "replaces_id) "
        "VALUES (COALESCE(%s::uuid, gen_random_uuid()), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s) RETURNING id",
        (
            finding_id, tenant_id, borrower_id, draft.check_key, draft.check_version, origin,
            draft.severity, draft.direction, draft.pillar, draft.summary,
            json.dumps({"refs": list(draft.evidence_refs), "facts": dict(draft.facts)}),
            list(tool_call_ids), case_id, verified, replaces_id,
        ),
    ).fetchone()  # fmt: skip
    assert row
    return str(row[0])


def replaced_by(conn: Conn, finding_ids: Sequence[str]) -> dict[str, str]:
    """For each of `finding_ids` that another finding replaces: {replaced id: replacement id}.
    The one definition of "replaced" for readers that do not hold a `StoredFinding` list."""
    if not finding_ids:
        return {}
    return {
        str(old): str(new)
        for old, new in conn.execute(
            "SELECT replaces_id, id FROM finding WHERE replaces_id = ANY(%s::uuid[])",
            (list(finding_ids),),
        ).fetchall()
    }


def current_findings(findings: Sequence[StoredFinding]) -> list[StoredFinding]:
    """The findings no other finding in the list replaces (a replacement and the finding it
    replaces are always in the same case, so a case's full list is enough)."""
    replaced = {f.replaces_id for f in findings if f.replaces_id}
    return [f for f in findings if f.id not in replaced]


def load_findings(conn: Conn, *, case_id: str, current_only: bool = False) -> list[StoredFinding]:
    """A case's findings, oldest first. With `current_only`, replaced ones are left out."""
    rows = conn.execute(
        "SELECT id, check_key, check_version, severity, pillar, summary, evidence_refs, "
        "direction, origin, case_id, tool_call_ids, verified, replaces_id FROM finding "
        "WHERE case_id = %s ORDER BY created_at, id",
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
            StoredFinding(
                str(r[0]),
                draft,
                r[8],
                str(r[9]) if r[9] else None,
                tuple(r[10]),
                r[11],
                str(r[12]) if r[12] else None,
            )
        )
    return current_findings(out) if current_only else out
