"""Demo P2-B: readable trace page, dev-login tenant picker, synthetic backtest summary page."""

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tangible.agent.llm import NullClient
from tangible.backtest import view as backtest_view
from tangible.checks.registry import FindingDraft
from tangible.db.findings import insert_finding
from tangible.db.session import Conn, admin_connect, tenant_session
from tangible.workbench.app import create_app
from tangible.workbench.settings import Settings
from tangible.workbench.ui import present
from test_assessment import POLICY_PATH, World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack, add_users
from test_workbench_api import stack as stack  # noqa: F401
from test_workbench_ui import SCRIPT, make_case, ui

FIXTURE = Path(__file__).parent / "fixtures" / "backtest_report.json"
BANNER = "SYNTHETIC COHORT: labels are planted problems, not repayment outcomes"


# ---------------------------------------------------------------- presenter (no database)


def test_view_shapes() -> None:
    assert present.view(None)["kind"] == "empty"
    assert present.view({"a": 1, "b": None, "c": True})["kind"] == "kv"
    kv = present.view({"a": 1, "b": None, "c": True})
    assert [(k, c["text"]) for k, c in kv["rows"]] == [("a", "1"), ("b", "null"), ("c", "true")]
    # a nested value anywhere falls back to a pretty-printed block
    nested = present.view({"a": {"b": 1}})
    assert nested["kind"] == "pre" and '"b": 1' in nested["text"]
    assert present.view([1, 2, 3])["kind"] == "pre"

    t = present.view({"columns": ["x", "y"], "rows": [[1, "a"], [2, None]], "truncated": False})
    assert t["kind"] == "tables" and t["head"]["kind"] == "kv"
    assert t["table"]["columns"] == ["x", "y"] and t["table"]["more"] == 0

    rows = present.view([{"k": i, "v": "x"} for i in range(60)])
    assert rows["kind"] == "table" and rows["columns"] == ["k", "v"]
    assert len(rows["rows"]) == present.MAX_ROWS and rows["more"] == 10


def test_args_view_pulls_out_sql_and_code() -> None:
    a = present.args_view({"sql": "SELECT 1", "limit": 5, "code": "x = 1"})
    assert a["blocks"] == [("sql", "SELECT 1"), ("code", "x = 1")]
    assert a["rest"]["kind"] == "kv"
    only = present.args_view({"sql": "SELECT 1"})
    assert only["blocks"] == [("sql", "SELECT 1")] and only["rest"] is None


def test_short_ref() -> None:
    pull = "0f8e7d6c-1111-2222-3333-444455556666"
    assert present.short_ref(f"{pull}#row=1234") == "pull 0f8e7d6c · row 1234"
    assert present.short_ref(f"{pull}#invoice=INV-9") == "pull 0f8e7d6c · invoice INV-9"
    assert present.short_ref(f"{pull}#statement") == "pull 0f8e7d6c · statement"
    assert present.short_ref("not a ref") == "not a ref"


# ---------------------------------------------------------------- trace page


def _tool_call(
    conn: Conn, w: World, case_id: str, tool: str, args: dict[str, Any], result: dict[str, Any]
) -> str:
    row = conn.execute(
        "INSERT INTO tool_call (tenant_id, case_id, tool, tool_version, args, result, "
        "started_at, duration_ms) VALUES (%s, %s, %s, 1, %s::jsonb, %s::jsonb, now(), 42) "
        "RETURNING id",
        (w.tenant, case_id, tool, json.dumps(args), json.dumps(result)),
    ).fetchone()
    assert row
    return str(row[0])


@pytest.mark.db
def test_trace_renders_tables_short_refs_and_escapes(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = make_case(stack)
    pull = str(uuid.uuid4())
    ref = f"{pull}#row=1234"
    ledger_rows = [[i, f"payout {i}", SCRIPT if i == 0 else None] for i in range(60)]
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        q = _tool_call(
            conn, stack.w, case_id, "query_ledger",
            {"sql": f"SELECT id, memo, note FROM payout -- {SCRIPT}\nLIMIT 60"},
            {"columns": ["id", "memo", "note"], "rows": ledger_rows, "truncated": False},
        )  # fmt: skip
        m = _tool_call(
            conn, stack.w, case_id, "compute_metric",
            {"key": "refund_rate", "ref": ref},
            {"key": "refund_rate", "value": "0.041", "note": SCRIPT},
        )  # fmt: skip
        n = _tool_call(
            conn, stack.w, case_id, "run_analysis",
            {"code": "RESULT = {'a': 1}"},
            {"result": {"a": {"b": [1, 2]}}, "stdout": "", "error": None},
        )  # fmt: skip
        fid = insert_finding(
            conn, stack.w.tenant, stack.w.borrower,
            FindingDraft("agent_note", 1, "review", "integrity", f"odd {SCRIPT} timing"),
            origin="model", case_id=case_id, tool_call_ids=[q, m, n], verified=False,
        )  # fmt: skip
    monkeypatch.setattr(
        "tangible.evidence.resolve.resolve_line", lambda *_a, **_k: f"2020,{SCRIPT},1"
    )

    page = ui(stack, "GET", f"/cases/{case_id}/findings/{fid}/trace", "ana", 200).text

    # summary: plain-text labels
    assert "Severity: <strong" in page and ">review</strong>" in page
    assert "Origin: <strong>model</strong>" in page
    assert "NO - unverified" in page
    # cited tool calls are cards with name and version, and timing
    assert "<h3>query_ledger <small>v1</small></h3>" in page
    assert "took 42 ms" in page
    # list-of-rows result -> a table with headers, capped at 50 rows
    assert '<th scope="col">memo</th>' in page
    assert "payout 49" in page and "payout 50" not in page
    assert "10 more rows not shown" in page
    # flat dict result -> key/value rows
    assert '<th scope="row">value</th><td>0.041</td>' in page
    # sql and code are in <pre>; a nested result falls back to a pretty-printed <pre>
    assert "<pre>SELECT id, memo, note FROM payout" in page
    assert "<pre>RESULT = {&#39;a&#39;: 1}</pre>" in page
    assert "&#34;b&#34;: [" in page
    # evidence: short label, full ref in the title, raw line labelled
    assert f'<code title="{ref}">pull {pull[:8]} · row 1234</code>' in page
    assert "Raw source line (as received)" in page
    assert "2020,&lt;script&gt;" in page
    # untrusted text is escaped everywhere
    assert SCRIPT not in page
    assert "<script>" not in page.replace('<script src="', "")


@pytest.mark.db
def test_trace_rule_finding_has_no_verified_status(stack: Stack) -> None:  # noqa: F811
    case_id = make_case(stack)
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        fid = insert_finding(
            conn, stack.w.tenant, stack.w.borrower,
            FindingDraft("time_gaps", 1, "info", "integrity", "gap"),
            origin="rule", case_id=case_id,
        )  # fmt: skip
    page = ui(stack, "GET", f"/cases/{case_id}/findings/{fid}/trace", "ana", 200).text
    assert "n/a (rule finding)" in page and "No tool calls are attached" in page


# ---------------------------------------------------------------- dev login tenant picker


@pytest.mark.db
def test_dev_login_lists_tenants_demo_first(stack: Stack) -> None:  # noqa: F811
    make_case(stack)
    demo_name = f"demo-picker-{uuid.uuid4().hex[:6]}"
    other_name = f"plain-{uuid.uuid4().hex[:6]}</td><b>"
    with admin_connect(stack.w.url) as admin:
        admin.execute(
            "UPDATE tenant SET name = %s, created_at = now() - interval '1 day' WHERE id = %s",
            (demo_name, stack.w.tenant),
        )
        admin.execute(
            "INSERT INTO tenant (name, created_at) VALUES (%s, now() + interval '1 day')",
            (other_name,),
        )
    c = TestClient(stack.http.app)
    page = c.get("/dev/login").text
    assert "<h2>Tenants</h2>" in page
    # newer non-demo tenant sorts after the demo tenant; names are escaped
    assert page.index(demo_name) < page.index("plain-")
    assert other_name not in page and "&lt;/td&gt;&lt;b&gt;" in page
    link = f'<a href="/dev/login?tenant={stack.w.tenant}">{demo_name}</a>'
    assert link in page
    row = page[page.index(link) :].split("</tr>")[0]
    assert "<td>1</td>" in row  # one case
    # the manual id box is still there, and a chosen tenant still lists its users
    assert 'name="tenant"' in page
    users = c.get(f"/dev/login?tenant={stack.w.tenant}").text
    assert "Sign in as ana" in users and "<h2>Tenants</h2>" not in users


@pytest.mark.db
def test_dev_login_tenant_list_is_404_without_dev_auth(
    clean_world: World,  # noqa: F811
    tmp_path: Path,
) -> None:
    w = clean_world
    add_users(w)
    settings = Settings(
        database_url=w.url, evidence_dir=tmp_path, policy_path=POLICY_PATH, dev_auth=False,
        client_factory=NullClient,
    )  # fmt: skip
    from tangible.workbench.auth import DevHeaderAuthenticator

    c = TestClient(create_app(settings, authenticator=DevHeaderAuthenticator(w.url)))
    r = c.get("/dev/login")
    assert r.status_code == 404 and "Tenants" not in r.text and w.tenant not in r.text


# ---------------------------------------------------------------- backtest page


@pytest.mark.db
def test_backtest_page_renders_fixture(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(FIXTURE))
    before = _views(stack)
    for who in ("ana", "len", "adm"):  # any signed-in role
        page = ui(stack, "GET", "/backtest", who, 200).text
    assert BANNER in page
    for heading in (
        "Cohort summary", "Loss by band", "Plants caught", "False alarms per clean loan",
        "Replay matches", "Time-leak guard", "Metric discrimination",
    ):  # fmt: skip
        assert heading in page
    assert "<strong>100.0%</strong> caught (13 of 13 planted problems)" in page
    assert "<strong>0.17</strong> (2 alarms on 12 clean loans" in page
    assert "<strong>20 of 20</strong> frozen assessments replay" in page
    assert "0 violations" in page and "16,348 rows" in page
    assert '<a href="/backtest">Backtest</a>' in page
    # metrics: top 10 by AUC, highest first
    metrics = page[page.index("Metric discrimination") :]
    names = re.findall(r'<tr><th scope="row">(\w+)</th><td>\d\.\d{3}</td>', metrics)
    assert len(names) == 10 and names[0] == "cash_runway"
    assert "payables_discipline" in names and "history_days" not in names
    assert _views(stack) == before + 3  # every view is audited


@pytest.mark.db
def test_backtest_page_missing_or_bad_file(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(tmp_path / "nope.json"))
    page = ui(stack, "GET", "/backtest", "ana", 200).text
    assert "No backtest report has been generated yet" in page
    assert "tangible-backtest synth" in page and "tangible-backtest run" in page
    assert BANNER not in page

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(bad))
    page = ui(stack, "GET", "/backtest", "ana", 200).text
    assert "could not be shown: the report is not valid JSON" in page


@pytest.mark.db
def test_backtest_page_escapes_report_text(
    stack: Stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = json.loads(FIXTURE.read_text())
    data["stats"]["plants"]["by_plant"][SCRIPT] = {"planted": 1, "caught": 0, "catch_rate": 0.0}
    data["stats"]["plants"]["misses"] = [{"borrower_key": SCRIPT, "plant": SCRIPT}]
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv(backtest_view.REPORT_ENV, str(path))
    page = ui(stack, "GET", "/backtest", "ana", 200).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>" not in page.replace('<script src="', "")


@pytest.mark.db
def test_backtest_requires_sign_in(stack: Stack) -> None:  # noqa: F811
    r = TestClient(stack.http.app).get("/backtest", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dev/login"


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
