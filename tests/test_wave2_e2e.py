"""Wave 2 gate: a scripted model drives the tools on a planted case end to end.

baseline -> tool calls (every one logged) -> model findings (numbers verified against cited tool
output) -> freeze (policy decides; knockout on the hold) -> replay matches.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tangible.agent.llm import ModelResponse, ScriptedClient
from tangible.agent.tools import call_tool, open_context, tool_definitions
from tangible.agent.verify import verify_numbers
from tangible.assessment import freeze, replay, run_baseline
from tangible.db.cases import create_case
from tangible.db.findings import load_findings
from tangible.db.session import tenant_session
from tangible.policy import load_policy
from test_assessment import POLICY_PATH, datasets, hidden, make_world  # noqa: F401

pytestmark = pytest.mark.db


def drive(client: ScriptedClient, ctx: Any, steps: int, ids: list[str]) -> list[str]:
    """Minimal stand-in for the Wave 3 loop: run each tool call the model asks for. Tool-call
    ids the model cites are written as "$n" (index into `ids`, which grows as calls run)."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Investigate."}]
    for _ in range(steps):
        resp = client.complete(system="test", messages=messages, tools=tool_definitions())
        for call in resp.tool_calls:
            args = dict(call["input"])
            if "tool_call_ids" in args:
                args["tool_call_ids"] = [ids[int(r[1:])] for r in args["tool_call_ids"]]
            out = call_tool(ctx, call["name"], args)
            assert out.error is None, (call["name"], out.error)
            ids.append(out.call_id)
            messages.append({"role": "user", "content": str(out.result)})
    return ids


def test_scripted_case_end_to_end(hidden, tmp_path: Path) -> None:  # noqa: F811
    w = hidden
    policy = load_policy(POLICY_PATH)
    with tenant_session(w.url, w.tenant) as conn:
        case = create_case(conn, w.tenant, w.borrower, w.as_of)
        run_baseline(conn, case, policy)
        ctx = open_context(
            conn, case, policy, tmp_path, verifier=lambda s, r: verify_numbers(s, r).ok
        )
        # What the model will "see" from its first call; the script quotes it back.
        first = call_tool(ctx, "run_check", {"key": "payouts_vs_deposits"})
        (draft,) = first.result["findings"]
        share = Decimal(draft["facts"]["matched_share"])
        pct = f"{share * 100:.2f}%"

        client = ScriptedClient(
            [
                ModelResponse(
                    text="", tool_calls=({"id": "t1", "name": "query_ledger", "input": {
                        "sql": "SELECT count(*) AS n FROM payouts"}},),
                ),
                ModelResponse(
                    text="", tool_calls=({"id": "t2", "name": "record_finding", "input": {
                        "slug": "reserve-suspected",
                        "summary": f"Only {pct} of payout value reached the bank on time.",
                        "severity": "review", "pillar": "integrity",
                        "direction": "raises_risk", "tool_call_ids": ["$0"]}},),
                ),
                ModelResponse(
                    text="", tool_calls=({"id": "t3", "name": "record_finding", "input": {
                        "slug": "invented-number",
                        "summary": "Deposits were short by £99,999.99 over the quarter.",
                        "severity": "info", "pillar": "integrity",
                        "direction": "raises_risk", "tool_call_ids": ["$0"]}},),
                ),
            ]
        )  # fmt: skip
        ids = drive(client, ctx, steps=3, ids=[first.call_id])
        assert len(ids) == 4

        logged = conn.execute(
            "SELECT count(*) FROM tool_call WHERE case_id = %s", (case.id,)
        ).fetchone()
        assert logged == (4,)  # the direct run_check + 3 scripted calls

        model = {f.draft.check_key: f for f in load_findings(conn, case_id=case.id)
                 if f.origin == "model"}  # fmt: skip
        assert model["agent:reserve-suspected"].verified is True
        assert model["agent:invented-number"].verified is False
        assert model["agent:reserve-suspected"].tool_call_ids == (ids[0],)
        # The agent has no way to set anything policy-shaped.
        (spec,) = [t for t in tool_definitions() if t["name"] == "record_finding"]
        assert spec["input_schema"]["additionalProperties"] is False
        assert not {"band", "limit", "value"} & set(spec["input_schema"]["properties"])

    with tenant_session(w.url, w.tenant) as conn:
        frozen = freeze(conn, case, policy, created_by="analyst-1")
        assert frozen.result.band is None and frozen.result.limit is None  # hold knockout

    with tenant_session(w.url, w.tenant) as conn:
        report = replay(conn, frozen.assessment_id, POLICY_PATH)
        assert report.match, report.diffs
