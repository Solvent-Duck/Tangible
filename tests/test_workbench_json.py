"""JSON reads added for the web app: case workspace, status polling, lookups, backtest."""

import pytest

from test_assessment import POLICY_PATH, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack
from test_workbench_api import stack as stack  # noqa: F401

pytestmark = pytest.mark.db


def test_workspace_and_status(stack: Stack) -> None:  # noqa: F811
    case = stack.new_case("ana")
    stack.drive(case)
    ws = stack.call("GET", f"/api/cases/{case}/workspace", "ana", 200).json()
    assert ws["borrower"] and ws["viewer_role"] == "analyst"
    assert ws["metrics"] and all({"label", "text", "key"} <= m.keys() for m in ws["metrics"])
    assert isinstance(ws["documents"], list) and ws["late_uploads"] == 0
    st = stack.call("GET", f"/api/cases/{case}/status", "ana", 200).json()
    assert st["id"] == case and st["status"]
    stack.call("GET", f"/api/cases/{case}/workspace", "bob", 403)
    listed = stack.call("GET", "/api/cases", "ana", 200).json()
    assert listed[0]["id"] == case and listed[0]["assessment"] is None


def test_lookups_are_for_credit_admins(stack: Stack) -> None:  # noqa: F811
    out = stack.call("GET", "/api/cases/lookups", "adm", 200).json()
    assert {b["id"] for b in out["borrowers"]} == {stack.w.borrower}
    assert {a["subject"] for a in out["analysts"]} == {"ana", "bob"}
    stack.call("GET", "/api/cases/lookups", "ana", 403)


def test_backtest_summary(stack: Stack) -> None:  # noqa: F811
    out = stack.call("GET", "/api/backtest", "len", 200).json()
    assert {"summary", "problem", "report_path", "hint"} <= out.keys()
