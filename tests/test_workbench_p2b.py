"""Demo P2-B, as JSON for the web app: dev sign-in tenant list, synthetic backtest summary, and a
rule finding's trace."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tangible.agent.llm import NullClient
from tangible.backtest import view as backtest_view
from tangible.checks.registry import FindingDraft
from tangible.db.findings import insert_finding
from tangible.db.session import admin_connect, tenant_session
from tangible.workbench.app import create_app
from tangible.workbench.auth import DevHeaderAuthenticator
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH, World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack, add_users
from test_workbench_api import stack as stack  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures" / "backtest_report.json"


# ---------------------------------------------------------------- trace


@pytest.mark.db
def test_trace_of_a_rule_finding_has_no_tool_calls(stack: Stack) -> None:  # noqa: F811
    case_id = stack.new_case("ana")
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        fid = insert_finding(
            conn, stack.w.tenant, stack.w.borrower,
            FindingDraft("time_gaps", 1, "info", "integrity", "gap"),
            origin="rule", case_id=case_id,
        )  # fmt: skip
    out = stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "ana", 200).json()
    assert out["finding"]["origin"] == "rule" and out["finding"]["verified"] is None
    assert out["tool_calls"] == []


# ---------------------------------------------------------------- dev sign-in tenant list


@pytest.mark.db
def test_dev_tenants_lists_demo_tenants_first_with_their_users(stack: Stack) -> None:  # noqa: F811
    demo_name = f"demo-picker-{uuid.uuid4().hex[:6]}"
    other_name = f"plain-{uuid.uuid4().hex[:6]}"
    with admin_connect(stack.w.url) as admin:
        admin.execute(
            "UPDATE tenant SET name = %s, created_at = now() - interval '1 day' WHERE id = %s",
            (demo_name, stack.w.tenant),
        )
        admin.execute(
            "INSERT INTO tenant (name, created_at) VALUES (%s, now() + interval '1 day')",
            (other_name,),
        )
    tenants = TestClient(stack.http.app).get("/api/dev/tenants").json()  # no auth needed
    names = [t["name"] for t in tenants]
    assert demo_name in names
    if other_name in names:  # the list is capped; when both show, demo tenants come first
        assert names.index(demo_name) < names.index(other_name)
    mine = next(t for t in tenants if t["id"] == stack.w.tenant)
    assert {u["subject"] for u in mine["users"]} == {"adm", "ana", "bob", "len"}


@pytest.mark.db
def test_dev_tenants_is_404_without_dev_auth(
    clean_world: World,  # noqa: F811
    tmp_path: Path,
) -> None:
    w = clean_world
    add_users(w)
    settings = Settings(
        database_url=w.url, evidence_dir=tmp_path, policy_path=POLICY_PATH, dev_auth=False,
        client_factory=NullClient,
    )  # fmt: skip
    c = TestClient(create_app(settings, authenticator=DevHeaderAuthenticator(w.url)))
    r = c.get("/api/dev/tenants")
    assert r.status_code == 404 and w.tenant not in r.text


# ---------------------------------------------------------------- backtest


@pytest.mark.db
def test_backtest_summarises_the_fixture(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(FIXTURE))
    before = _views(stack)
    for who in ("ana", "len", "adm"):  # any staff role
        out = stack.call("GET", "/api/backtest", who, 200).json()
    s = out["summary"]
    assert out["problem"] is None and s["synthetic"] is True
    assert s["plants"]["catch_rate"] == "100.0%" and s["plants"]["planted"] == "13"
    assert s["false_alarms"]["per_clean_loan"] == "0.17"
    assert s["replay"]["all_match"] is True and s["replay"]["matched"] == "20"
    assert s["time_leak"]["clean"] is True
    names = [m["metric"] for m in s["top_metrics"]]
    assert len(names) == 10 and names[0] == "cash_runway" and "history_days" not in names
    assert _views(stack) == before + 3  # every view is audited


@pytest.mark.db
def test_backtest_missing_or_bad_file(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(tmp_path / "nope.json"))
    out = stack.call("GET", "/api/backtest", "ana", 200).json()
    assert out["summary"] is None and out["report_path"] == str(tmp_path / "nope.json")
    assert "tangible-backtest" in out["hint"]

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(bad))
    out = stack.call("GET", "/api/backtest", "ana", 200).json()
    assert out["summary"] is None and "not valid JSON" in out["problem"]


@pytest.mark.db
def test_backtest_requires_sign_in(stack: Stack) -> None:  # noqa: F811
    assert TestClient(stack.http.app).get("/api/backtest").status_code == 401


def test_backtest_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backtest_view.REPORT_ENV, raising=False)
    assert backtest_view.report_path() == Path("data/backtest/report.json")
    monkeypatch.setenv(backtest_view.REPORT_ENV, "/x/y.json")
    assert backtest_view.report_path() == Path("/x/y.json")


def _views(s: Stack) -> int:
    with tenant_session(s.w.url, s.w.tenant) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE action = 'view' AND object_kind = 'backtest_report'"
        ).fetchone()
    assert row
    return int(row[0])
