"""WP4B: the server-rendered workbench UI, driven through HTML routes with TestClient."""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tangible.agent.llm import NullClient
from tangible.checks.registry import FindingDraft
from tangible.db.cases import set_case_status
from tangible.db.findings import insert_finding
from tangible.db.session import tenant_session
from tangible.workbench.app import create_app
from tangible.workbench.auth import DevHeaderAuthenticator
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH, World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack, _insert_tool_call, add_users
from test_workbench_api import stack as stack  # noqa: F401

pytestmark = pytest.mark.db

SCRIPT = "<script>alert(1)</script>"


def ui(s: Stack, method: str, path: str, user: str, expect: int | None = None, **kw: Any) -> Any:
    """A plain browser request: no htmx headers, redirects not followed."""
    r = s.http.request(method, path, headers=s.h(user), follow_redirects=False, **kw)
    if expect is not None:
        assert r.status_code == expect, (r.status_code, r.text[:500])
    return r


def post(s: Stack, path: str, user: str, **data: str) -> httpx.Response:
    """A plain form POST must answer with a 303 redirect (works without JavaScript)."""
    r = ui(s, "POST", path, user, 303, data=data)
    assert r.headers["location"]
    return r


def db_one(s: Stack, sql: str, *args: Any) -> Any:
    with tenant_session(s.w.url, s.w.tenant) as conn:
        row = conn.execute(sql, args).fetchone()
    assert row
    return row[0]


def make_case(s: Stack) -> str:
    post(
        s, "/cases/new", "adm",
        borrower_id=s.w.borrower, as_of=s.w.as_of.strftime("%Y-%m-%dT%H:%M"),
        loan_amount="50000", budget_steps="40", budget_usd="5.00", assigned_analyst="ana",
    )  # fmt: skip
    return str(db_one(s, "SELECT id FROM case_file ORDER BY created_at DESC LIMIT 1"))


def plan_and_investigate(s: Stack, case_id: str) -> None:
    post(s, f"/cases/{case_id}/plan", "ana")
    plan_id = db_one(s, "SELECT id FROM agent_plan WHERE case_id = %s", case_id)
    post(s, f"/cases/{case_id}/plan/approve", "ana", plan_id=str(plan_id), body="", original="")
    post(s, f"/cases/{case_id}/investigate", "ana")


def test_gate_flow_through_html(stack: Stack) -> None:  # noqa: F811
    case_id = make_case(stack)
    base = f"/cases/{case_id}"

    # the analyst opens the case; lender roles cannot see it yet
    page = ui(stack, "GET", base, "ana", 200).text
    assert "Propose plan" in page and "Sign report" in page
    ui(stack, "GET", base, "len", 404)
    assert case_id not in ui(stack, "GET", "/", "len", 200).text
    assert case_id in ui(stack, "GET", "/", "ana", 200).text.replace(f"/cases/{case_id}", case_id)

    # nothing to sign yet; investigating without an approved plan is a friendly 409 page
    ui(stack, "POST", f"{base}/investigate", "ana", 409)
    ui(stack, "GET", f"{base}/sign", "ana", 409)

    plan_and_investigate(stack, case_id)
    page = ui(stack, "GET", base, "ana", 200).text
    assert "<strong>review</strong>" in page
    assert "Baseline metrics" in page and '<th scope="row">refund_rate' in page

    findings = db_one(stack, "SELECT count(*) FROM finding WHERE case_id = %s", case_id)
    assert findings
    fid = str(db_one(stack, "SELECT id FROM finding WHERE case_id = %s LIMIT 1", case_id))
    assert f"/cases/{case_id}/findings/{fid}/trace" in page
    # reason is required
    ui(stack, "POST", f"{base}/findings/{fid}/review", "ana", 422, data={"action": "reject"})
    post(stack, f"{base}/findings/{fid}/review", "ana", action="reject", reason="seasonal")
    assert "rejected" in ui(stack, "GET", base, "ana", 200).text

    # signing needs the confirmation step
    assert "Confirm signing" in ui(stack, "GET", f"{base}/sign", "ana", 200).text
    ui(stack, "POST", f"{base}/sign", "ana", 422, data={})
    r = post(stack, f"{base}/sign", "ana", confirm="yes")
    aid = r.headers["location"].rsplit("/", 1)[1]
    assert uuid.UUID(aid)

    page = ui(stack, "GET", f"/assessments/{aid}", "ana", 200).text
    band = db_one(stack, "SELECT decision_band FROM assessment WHERE id = %s", aid)
    assert f"Band {band} because" in page
    assert "refund_rate" in page and '<th scope="col">Saw</th>' in page
    assert "knockout: no blocking hold finding" in page
    assert "Replay and compare" in page
    r = post(stack, f"/assessments/{aid}/replay", "ana")
    assert "matches" in ui(stack, "GET", r.headers["location"], "ana", 200).text

    # the lender sees the signed case, traces a finding, and overrides beside the assessment
    lender_list = ui(stack, "GET", "/", "len", 200).text
    assert f"/cases/{case_id}" in lender_list and f"/assessments/{aid}" in lender_list
    ui(stack, "GET", base, "len", 200)
    assert "Replay and compare" not in ui(stack, "GET", f"/assessments/{aid}", "len", 200).text
    assert "<strong>Sign" not in ui(stack, "GET", base, "len", 200).text
    ui(stack, "GET", f"{base}/findings/{fid}/trace", "len", 200)

    q = "SELECT row(a.*)::text FROM assessment a WHERE id = %s"
    before = db_one(stack, q, aid)
    ui(stack, "POST", f"/assessments/{aid}/override", "ana", 403, data={"reason": "x"})
    ui(stack, "POST", f"/assessments/{aid}/override", "len", 422, data={"reason": " "})
    post(stack, f"/assessments/{aid}/override", "len", reason="known customer")
    assert db_one(stack, q, aid) == before
    page = ui(stack, "GET", f"/assessments/{aid}", "len", 200).text
    assert "known customer" in page
    export = ui(stack, "GET", f"/assessments/{aid}/export", "len", 200)
    assert "attachment" in export.headers["content-disposition"]
    assert json.loads(export.text)["assessment"]["id"] == aid


def test_role_filtering(stack: Stack) -> None:  # noqa: F811
    mine = make_case(stack)
    assert mine in ui(stack, "GET", "/", "ana", 200).text
    assert mine not in ui(stack, "GET", "/", "bob", 200).text
    ui(stack, "GET", f"/cases/{mine}", "bob", 403)
    ui(stack, "POST", f"/cases/{mine}/plan", "bob", 403)
    plan_and_investigate(stack, mine)
    assert mine not in ui(stack, "GET", "/", "len", 200).text
    assert "No cases" in ui(stack, "GET", "/", "adm", 200).text
    ui(stack, "GET", f"/cases/{mine}", "len", 404)
    ui(stack, "GET", "/cases/new", "ana", 403)
    ui(stack, "GET", "/cases/new", "adm", 200)
    ui(stack, "POST", "/cases/assign", "adm", 303, data={"case_id": mine, "analyst": "bob"})
    assert mine in ui(stack, "GET", "/", "bob", 200).text


def test_rebase_notice_after_upload(stack: Stack) -> None:  # noqa: F811
    case_id = make_case(stack)
    plan_and_investigate(stack, case_id)
    assert "Rebase required" not in ui(stack, "GET", f"/cases/{case_id}", "ana", 200).text
    files = {"file": ("s.csv", b"date,amount\n2020-01-01,5\n", "text/csv")}
    ui(stack, "POST", f"/cases/{case_id}/documents", "ana", 303, files=files)
    page = ui(stack, "GET", f"/cases/{case_id}", "ana", 200).text
    assert "Rebase required" in page and "Rebase case" in page
    r = post(stack, f"/cases/{case_id}/rebase", "ana")
    assert r.headers["location"] != f"/cases/{case_id}"


def test_untrusted_text_is_escaped(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    case_id = make_case(stack)
    edited = {"notes": f"</textarea>{SCRIPT}"}
    post(stack, f"/cases/{case_id}/plan", "ana")
    plan_id = db_one(stack, "SELECT id FROM agent_plan WHERE case_id = %s", case_id)
    page = ui(stack, "GET", f"/cases/{case_id}", "ana", 200).text
    assert SCRIPT not in page
    post(
        stack, f"/cases/{case_id}/plan/approve", "ana",
        plan_id=str(plan_id), body=json.dumps(edited), original="{}",
    )  # fmt: skip
    post(stack, f"/cases/{case_id}/investigate", "ana")

    ref = f"{uuid.uuid4()}#row=7"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        call_id = _insert_tool_call(conn, stack.w, case_id, {"ref": ref, "note": SCRIPT})
        fid = insert_finding(
            conn, stack.w.tenant, stack.w.borrower,
            FindingDraft("agent_note", 1, "review", "integrity", f"odd {SCRIPT} timing"),
            origin="model", case_id=case_id, tool_call_ids=[call_id], verified=False,
        )  # fmt: skip
    monkeypatch.setattr(
        "tangible.evidence.resolve.resolve_line", lambda *_a, **_k: f"2020,{SCRIPT},1"
    )
    page = ui(stack, "GET", f"/cases/{case_id}", "ana", 200).text
    assert SCRIPT not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "UNVERIFIED" in page
    trace = ui(stack, "GET", f"/cases/{case_id}/findings/{fid}/trace", "ana", 200).text
    assert SCRIPT not in trace
    assert "2020,&lt;script&gt;" in trace and "&lt;script&gt;" in trace
    assert "<script>" not in trace.replace('<script src="', "")


def test_dev_login_is_404_without_dev_auth(clean_world: World, tmp_path: Path) -> None:  # noqa: F811
    w = clean_world
    add_users(w)
    settings = Settings(
        database_url=w.url, evidence_dir=tmp_path, policy_path=POLICY_PATH, dev_auth=False,
        client_factory=NullClient,
    )  # fmt: skip
    app = create_app(settings, authenticator=DevHeaderAuthenticator(w.url))
    c = TestClient(app)
    assert c.get("/dev/login").status_code == 404
    assert c.post("/dev/login", data={"tenant": w.tenant, "subject": "ana"}).status_code == 404
    # and a cookie is never honoured
    assert c.get("/", follow_redirects=False).status_code == 401


def test_dev_login_cookie_flow(stack: Stack) -> None:  # noqa: F811
    c = TestClient(stack.http.app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dev/login"
    page = c.get(f"/dev/login?tenant={stack.w.tenant}").text
    assert "Sign in as ana" in page and "Sign in as len" in page
    bad = c.post("/dev/login", data={"tenant": stack.w.tenant, "subject": "nobody"})
    assert bad.status_code == 401
    r = c.post(
        "/dev/login", data={"tenant": stack.w.tenant, "subject": "adm"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "adm (credit_admin)" in c.get("/").text
    c.cookies.set("tangible_dev", "forged.value")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_htmx_status_fragment_and_inline_review(stack: Stack) -> None:  # noqa: F811
    case_id = make_case(stack)
    plan_and_investigate(stack, case_id)
    base = f"/cases/{case_id}"
    hx = {**stack.h("ana"), "HX-Request": "true"}

    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        set_case_status(conn, case_id, "investigating")
    r = stack.http.get(f"{base}/status", headers=hx)
    assert r.status_code == 200 and "<html" not in r.text
    assert 'hx-trigger="every 2s"' in r.text and "investigating" in r.text
    assert "HX-Refresh" not in r.headers
    # without htmx the endpoint just redirects to the page
    assert ui(stack, "GET", f"{base}/status", "ana", 303).headers["location"] == base

    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        set_case_status(conn, case_id, "review")
    r = stack.http.get(f"{base}/status", headers=hx)
    assert "hx-trigger" not in r.text and r.headers["HX-Refresh"] == "true"

    fid = str(db_one(stack, "SELECT id FROM finding WHERE case_id = %s LIMIT 1", case_id))
    r = stack.http.post(
        f"{base}/findings/{fid}/review", headers=hx, data={"action": "dispute", "reason": "why"}
    )
    assert r.status_code == 200 and "<html" not in r.text and "disputed" in r.text
    r = stack.http.post(f"{base}/findings/{fid}/review", headers=hx, data={"action": "reject"})
    assert r.status_code == 200 and "reason is required" in r.text
