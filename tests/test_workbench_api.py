"""WP4A: the analyst workbench API, driven through FastAPI's TestClient with no model."""

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from tangible.agent.llm import ModelResponse, NullClient, Pricing, ScriptedClient
from tangible.checks.registry import FindingDraft
from tangible.db.cases import set_case_status
from tangible.db.findings import insert_finding
from tangible.db.session import Conn, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.workbench import services
from tangible.workbench.app import create_app
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH, World, datasets, make_world  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_assessment import hidden as hidden_world  # noqa: F401

pytestmark = pytest.mark.db

USERS = (
    ("adm", "credit_admin"),
    ("ana", "analyst"),
    ("bob", "analyst"),
    ("len", "lender_underwriter"),
)


def add_users(w: World) -> None:
    with tenant_session(w.url, w.tenant) as conn:
        for subject, role in USERS:
            conn.execute(
                "INSERT INTO app_user (tenant_id, subject, email, display_name, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                (w.tenant, subject, f"{subject}@x.test", subject.title(), role),
            )


class Stack(NamedTuple):
    http: TestClient
    w: World
    store: Path

    def h(self, user: str, tenant: str | None = None) -> dict[str, str]:
        return {"X-Tangible-Tenant": tenant or self.w.tenant, "X-Tangible-User": user}

    def call(self, method: str, path: str, user: str, expect: int | None = None, **kw: Any) -> Any:
        r = self.http.request(method, path, headers=self.h(user), **kw)
        if expect is not None:
            assert r.status_code == expect, (r.status_code, r.text)
        return r

    def new_case(self, analyst: str | None = "ana", as_of: datetime | None = None) -> str:
        body: dict[str, Any] = {
            "borrower_id": self.w.borrower,
            "as_of": (as_of or self.w.as_of).isoformat(),
            "loan_amount": "50000",
            "assigned_analyst": analyst,
        }
        r = self.call("POST", "/api/cases", "adm", 201, json=body)
        return str(r.json()["id"])

    def drive(self, case_id: str) -> None:
        """plan -> approve -> investigate (baseline runs; NullClient ends the turn)."""
        self.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
        self.call("POST", f"/api/cases/{case_id}/plan/approve", "ana", 200)
        self.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)

    def case(self, case_id: str, user: str = "ana") -> dict[str, Any]:
        r = self.call("GET", f"/api/cases/{case_id}", user, 200)
        out: dict[str, Any] = r.json()
        return out


def make_stack(w: World, tmp_path: Path) -> Stack:
    add_users(w)
    store = tmp_path / "evidence"
    settings = Settings(
        database_url=w.url,
        evidence_dir=store,
        policy_path=POLICY_PATH,
        dev_auth=True,
        client_factory=NullClient,
    )
    return Stack(TestClient(create_app(settings)), w, store)


@pytest.fixture
def stack(clean_world: World, tmp_path: Path) -> Stack:  # noqa: F811
    return make_stack(clean_world, tmp_path)


@pytest.fixture
def hstack(hidden_world: World, tmp_path: Path) -> Stack:  # noqa: F811
    return make_stack(hidden_world, tmp_path)


def count(w: World, table: str, where: str = "true") -> int:
    with tenant_session(w.url, w.tenant) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()
    assert row
    return int(row[0])


def audit_actions(w: World, object_id: str) -> list[tuple[str, str, str]]:
    with tenant_session(w.url, w.tenant) as conn:
        return [
            (a, k, actor)
            for a, k, actor in conn.execute(
                "SELECT action, object_kind, actor FROM audit_log WHERE object_id = %s "
                "ORDER BY at, id",
                (object_id,),
            ).fetchall()
        ]


# ---------------------------------------------------------------- auth


def test_missing_or_unknown_credentials_are_401(stack: Stack) -> None:
    c = stack.http
    assert c.get("/api/cases").status_code == 401
    assert c.get("/api/cases", headers={"X-Tangible-User": "ana"}).status_code == 401
    assert c.get("/api/cases", headers={"X-Tangible-Tenant": stack.w.tenant}).status_code == 401
    assert c.get("/api/cases", headers=stack.h("nobody")).status_code == 401
    assert c.get("/api/cases", headers=stack.h("ana", str(uuid.uuid4()))).status_code == 401
    assert c.get("/api/cases", headers=stack.h("ana", "not-a-uuid")).status_code == 401
    assert stack.call("GET", "/api/cases", "ana", 200).json() == []


def test_app_refuses_to_start_without_dev_auth(
    test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="TANGIBLE_DEV_AUTH"):
        create_app(Settings(database_url=test_db_url, evidence_dir=tmp_path, dev_auth=False))
    monkeypatch.delenv("TANGIBLE_DEV_AUTH", raising=False)
    with pytest.raises(RuntimeError, match="TANGIBLE_DEV_AUTH"):
        create_app(Settings.from_env())
    monkeypatch.setenv("TANGIBLE_DEV_AUTH", "1")
    assert create_app(Settings.from_env()) is not None


def test_settings_from_env_defaults_to_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANGIBLE_MODEL_BACKEND", raising=False)
    s = Settings.from_env()
    assert isinstance(s.client_factory(), NullClient)
    assert s.pricing.input_per_mtok == 0 and s.pricing.output_per_mtok == 0


# ---------------------------------------------------------------- roles


def test_analyst_sees_only_assigned_cases(stack: Stack) -> None:
    mine = stack.new_case("ana")
    theirs = stack.new_case("bob")
    assert [c["id"] for c in stack.call("GET", "/api/cases", "ana", 200).json()] == [mine]
    assert [c["id"] for c in stack.call("GET", "/api/cases", "bob", 200).json()] == [theirs]
    stack.call("GET", f"/api/cases/{theirs}", "ana", 403)
    stack.call("POST", f"/api/cases/{theirs}/plan", "ana", 403)
    stack.call("POST", f"/api/cases/{theirs}/sign", "ana", 403)
    stack.case(mine)


def test_only_credit_admin_creates_and_assigns(stack: Stack) -> None:
    body = {"borrower_id": stack.w.borrower, "as_of": stack.w.as_of.isoformat()}
    for user in ("ana", "len"):
        stack.call("POST", "/api/cases", user, 403, json=body)
    case_id = stack.new_case(None)
    stack.call("POST", f"/api/cases/{case_id}/assign", "ana", 403, json={"analyst": "ana"})
    stack.call("POST", f"/api/cases/{case_id}/assign", "adm", 422, json={"analyst": "len"})
    r = stack.call("POST", f"/api/cases/{case_id}/assign", "adm", 200, json={"analyst": "bob"})
    assert r.json()["assigned_analyst"] == "bob"
    stack.case(case_id, "bob")
    stack.call("POST", "/api/cases", "adm", 404, json={**body, "borrower_id": str(uuid.uuid4())})


def test_lender_cannot_see_unsigned_or_drive_the_agent(stack: Stack) -> None:
    case_id = stack.new_case()
    assert stack.call("GET", "/api/cases", "len", 200).json() == []
    assert stack.call("GET", "/api/cases", "adm", 200).json() == []
    stack.call("GET", f"/api/cases/{case_id}", "len", 404)
    for user in ("len", "adm"):
        stack.call("POST", f"/api/cases/{case_id}/plan", user, 403)
        stack.call("POST", f"/api/cases/{case_id}/investigate", user, 403)
        stack.call("POST", f"/api/cases/{case_id}/sign", user, 403)
    stack.drive(case_id)
    stack.call("GET", f"/api/cases/{case_id}", "len", 404)  # still unsigned


def test_cross_tenant_ids_are_404(stack: Stack, tmp_path: Path, datasets: Any) -> None:  # noqa: F811
    other = make_world(stack.w.url, datasets["clean"], tmp_path / "other", name="other")
    add_users(other)
    mine = stack.new_case()
    stack.drive(mine)
    stack.call("POST", f"/api/cases/{mine}/sign", "ana", 200)
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        row = conn.execute("SELECT id FROM assessment").fetchone()
        assert row
        aid = str(row[0])
        fid = str(conn.execute("SELECT id FROM finding LIMIT 1").fetchone()[0])  # type: ignore[index]
    for user, role_paths in (
        ("ana", [f"/api/cases/{mine}", f"/api/cases/{mine}/questions"]),
        ("len", [f"/api/cases/{mine}", f"/api/assessments/{aid}"]),
    ):
        for path in role_paths:
            r = stack.http.get(path, headers=stack.h(user, other.tenant))
            assert r.status_code == 404, (path, r.status_code)
    r = stack.http.get(
        f"/api/cases/{mine}/findings/{fid}/trace", headers=stack.h("ana", other.tenant)
    )
    assert r.status_code == 404
    r = stack.http.post(f"/api/cases/{mine}/sign", headers=stack.h("ana", other.tenant))
    assert r.status_code == 404
    r = stack.http.post(
        f"/api/assessments/{aid}/override",
        headers=stack.h("len", other.tenant),
        json={"reason": "x"},
    )
    assert r.status_code == 404
    # and the other tenant's own listing shows nothing of ours
    assert stack.http.get("/api/cases", headers=stack.h("ana", other.tenant)).json() == []


# ---------------------------------------------------------------- the flow


def test_full_flow(stack: Stack) -> None:
    case_id = stack.new_case()
    base = f"/api/cases/{case_id}"

    # invalid orders of operations
    stack.call("POST", f"{base}/investigate", "ana", 409)  # no plan
    stack.call("POST", f"{base}/plan/approve", "ana", 409)  # nothing to approve
    stack.call("POST", f"{base}/sign", "ana", 409)  # not investigated

    plan = stack.call("POST", f"{base}/plan", "ana", 201).json()
    assert plan["version"] == 1 and plan["approved"] is False
    stack.call("POST", f"{base}/investigate", "ana", 409)  # plan proposed but not approved
    edited = {"business_model": "retailer", "risks": [], "notes": "analyst edit"}
    r = stack.call("POST", f"{base}/plan/approve", "ana", 200, json={"edited_body": edited})
    assert r.json()["edited"] is True
    stack.call("POST", f"{base}/investigate", "ana", 202)

    detail = stack.case(case_id)
    assert detail["status"] == "review"
    assert detail["last_investigation"]["stop_reason"] == "end_turn"
    assert [p["version"] for p in detail["plans"]] == [1, 2]
    assert detail["plans"][1]["approved"] and detail["plans"][1]["body"]["notes"] == "analyst edit"
    stack.call("POST", f"{base}/investigate", "ana", 409)  # already investigated
    rule = [f for f in detail["findings"] if f["origin"] == "rule"]
    assert rule and all(f["review_state"] == "open" for f in rule)

    target = rule[0]["id"]
    review = f"{base}/findings/{target}/review"
    stack.call("POST", review, "ana", 422, json={"action": "reject", "reason": "  "})
    stack.call("POST", review, "ana", 422, json={"action": "bless", "reason": "ok"})
    stack.call("POST", review, "len", 403, json={"action": "reject", "reason": "x"})
    r = stack.call("POST", review, "ana", 200, json={"action": "reject", "reason": "seasonal"})
    assert r.json()["review_state"] == "rejected"
    r = stack.call("POST", review, "ana", 200, json={"action": "dispute", "reason": "ask again"})
    assert r.json()["review_state"] == "disputed"
    stack.call("POST", review, "ana", 200, json={"action": "reject", "reason": "final"})
    assert count(stack.w, "underwriter_action", f"finding_id = '{target}'") == 3  # append-only
    f = next(x for x in stack.case(case_id)["findings"] if x["id"] == target)
    assert f["review_state"] == "rejected" and len(f["actions"]) == 3

    signed = stack.call("POST", f"{base}/sign", "ana", 200).json()
    aid = signed["assessment_id"]
    assert signed["case_status"] == "signed" and signed["decision_band"] is not None
    stack.call("POST", f"{base}/sign", "ana", 409)
    stack.call("POST", review, "ana", 409, json={"action": "accept", "reason": "late"})
    assert stack.case(case_id)["status"] == "signed"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        row = conn.execute(
            "SELECT created_by, decision_band, prompt_versions FROM assessment WHERE id = %s",
            (aid,),
        ).fetchone()
    assert row and row[0] == "ana" and row[1] == signed["decision_band"]
    assert "investigate" in row[2] and "plan" in row[2]

    # replay matches
    for user in ("ana", "adm"):
        r = stack.call("POST", f"/api/assessments/{aid}/replay", user, 200)
        assert r.json() == {"assessment_id": aid, "match": True, "diffs": []}
    stack.call("POST", f"/api/assessments/{aid}/replay", "len", 403)
    stack.call("POST", f"/api/assessments/{aid}/replay", "bob", 403)

    # lender sees the signed case and assessment, and can override beside it
    assert [c["id"] for c in stack.call("GET", "/api/cases", "len", 200).json()] == [case_id]
    ld = stack.case(case_id, "len")
    assert "plans" not in ld and "questions" not in ld and ld["assessments"][0]["id"] == aid
    q = "SELECT row(a.*)::text FROM assessment a WHERE id = %s"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        before = conn.execute(q, (aid,)).fetchone()
    body = {"reason": "known customer"}
    stack.call("POST", f"/api/assessments/{aid}/override", "ana", 403, json=body)
    stack.call("POST", f"/api/assessments/{aid}/override", "len", 422, json={"reason": ""})
    stack.call("POST", f"/api/assessments/{aid}/override", "len", 201, json=body)
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        assert conn.execute(q, (aid,)).fetchone() == before
    got = stack.call("GET", f"/api/assessments/{aid}", "len", 200).json()
    assert [o["reason"] for o in got["overrides"]] == ["known customer"]
    assert got["metrics"] and got["findings"]
    stack.call("GET", f"/api/assessments/{aid}/export", "adm", 200)
    stack.call("GET", f"/api/assessments/{aid}", "bob", 403)
    stack.call("POST", f"/api/assessments/{aid}/replay", "adm", 200)  # still matches after override
    assert stack.call("POST", f"/api/assessments/{aid}/replay", "ana", 200).json()["match"]

    # audit log: actor + action + object for each read
    actions = audit_actions(stack.w, case_id)
    assert ("create", "case", "adm") in actions
    assert ("view", "case", "ana") in actions and ("view", "case", "len") in actions
    a_actions = audit_actions(stack.w, aid)
    for want in (
        ("sign", "assessment", "ana"),
        ("view", "assessment", "len"),
        ("export", "assessment", "adm"),
        ("override", "assessment", "len"),
        ("replay", "assessment", "ana"),
    ):
        assert want in a_actions, want


def test_hold_knockout_then_reject_issues_a_band(hstack: Stack) -> None:
    # signing with an unreviewed hold: knock-out, no band
    first = hstack.new_case()
    hstack.drive(first)
    holds = [f for f in hstack.case(first)["findings"] if f["severity"] == "hold"]
    assert holds and all(h["review_state"] == "open" for h in holds)
    r = hstack.call("POST", f"/api/cases/{first}/sign", "ana", 200).json()
    assert r["decision_band"] is None and r["suggested_limit"] is None

    # a separate case: the analyst rejects the hold with a reason before signing
    second = hstack.new_case()
    hstack.drive(second)
    for h in (f for f in hstack.case(second)["findings"] if f["severity"] == "hold"):
        hstack.call(
            "POST", f"/api/cases/{second}/findings/{h['id']}/review", "ana", 200,
            json={"action": "reject", "reason": "reserve is documented in the loan agreement"},
        )  # fmt: skip
    r = hstack.call("POST", f"/api/cases/{second}/sign", "ana", 200).json()
    assert r["decision_band"] is not None and r["suggested_limit"] is not None
    # the first (knocked-out) assessment is untouched by the second
    assert count(hstack.w, "assessment", "decision_band IS NULL") == 1


# ---------------------------------------------------------------- documents and rebase


def test_upload_then_rebase(stack: Stack) -> None:
    old = stack.new_case()
    stack.drive(old)
    files = {"file": ("statement.csv", b"date,amount\n2020-01-01,5\n", "text/csv")}
    r = stack.call("POST", f"/api/cases/{old}/documents", "ana", 201, files=files)
    out = r.json()
    assert out["rebase_required"] is True and out["byte_size"] > 0
    assert len(out["sha256"]) == 64 and datetime.fromisoformat(out["observed_at"]) > stack.w.as_of
    assert LocalEvidenceStore(stack.store).exists(
        _key(stack.w, out["pull_id"])
    )  # stored write-once
    stack.call("POST", f"/api/cases/{old}/documents", "bob", 403, files=files)
    stack.call("POST", f"/api/cases/{old}/documents", "ana", 422, files={"file": ("e", b"")})

    new = stack.call("POST", f"/api/cases/{old}/rebase", "ana", 201).json()
    assert new["id"] != old and new["previous_case_id"] == old and new["status"] == "open"
    assert new["assigned_analyst"] == "ana" and new["loan_amount"] == "50000.00"
    assert new["documents_read"] == 0 and [u["pull_id"] for u in new["documents_unread"]] == [
        out["pull_id"]
    ]  # a CSV is not a readable statement: reported, and the rebase still happened
    assert datetime.fromisoformat(new["as_of"]) > stack.w.as_of
    assert datetime.fromisoformat(new["as_of"]) <= datetime.now(UTC)
    assert stack.case(old)["status"] == "closed"
    assert stack.case(new["id"])["previous_case_id"] == old
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        row = conn.execute(
            "SELECT previous_case_id FROM case_file WHERE id = %s", (new["id"],)
        ).fetchone()
    assert row == (uuid.UUID(old),)
    stack.call("POST", f"/api/cases/{old}/rebase", "ana", 409)
    stack.call("POST", f"/api/cases/{old}/documents", "ana", 409, files=files)
    # the same bytes again: the original observation stands, and it is before the new case's date
    again = stack.call("POST", f"/api/cases/{new['id']}/documents", "ana", 201, files=files).json()
    assert again["pull_id"] == out["pull_id"] and again["observed_at"] == out["observed_at"]
    assert again["rebase_required"] is False
    stack.drive(new["id"])  # the successor can be worked normally


def _key(w: World, pull_id: str) -> str:
    with tenant_session(w.url, w.tenant) as conn:
        row = conn.execute("SELECT s3_key FROM evidence_pull WHERE id = %s", (pull_id,)).fetchone()
    assert row
    return str(row[0])


# ---------------------------------------------------------------- trace and audit


def test_trace_returns_tool_calls_and_resolved_lines(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = stack.new_case()
    stack.drive(case_id)
    ref = f"{uuid.uuid4()}#row=7"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        call_id = _insert_tool_call(conn, stack.w, case_id, {"series": "payouts", "ref": ref})
        fid = insert_finding(
            conn, stack.w.tenant, stack.w.borrower,
            FindingDraft("agent_note", 1, "review", "integrity", "odd payout timing"),
            origin="model", case_id=case_id, tool_call_ids=[call_id], verified=True,
        )  # fmt: skip

    seen: list[str] = []

    def fake_resolve(conn: Conn, store: Any, r: Any) -> str | None:
        seen.append(str(r))
        return "2020-01-01,payout,123.45" if str(r) == ref else None

    monkeypatch.setattr("tangible.evidence.resolve.resolve_line", fake_resolve)
    out = stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "ana", 200).json()
    assert [c["id"] for c in out["tool_calls"]] == [call_id]
    assert out["tool_calls"][0]["tool"] == "run_metric"
    assert out["tool_calls"][0]["args"]["series"] == "payouts"
    assert out["evidence"] == [{"ref": ref, "line": "2020-01-01,payout,123.45", "error": None}]
    assert ref in seen

    # unresolvable refs are reported, not hidden
    monkeypatch.undo()
    out = stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "ana", 200).json()
    assert out["evidence"][0]["line"] is None

    stack.call("GET", f"/api/cases/{case_id}/findings/{uuid.uuid4()}/trace", "ana", 404)
    stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "bob", 403)
    stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "len", 404)  # unsigned
    assert audit_actions(stack.w, fid).count(("view", "finding_trace", "ana")) == 2


def _insert_tool_call(conn: Conn, w: World, case_id: str, args: dict[str, Any]) -> str:
    row = conn.execute(
        "INSERT INTO tool_call (tenant_id, case_id, tool, tool_version, args, result, started_at) "
        "VALUES (%s, %s, 'run_metric', 1, %s::jsonb, %s::jsonb, now()) RETURNING id",
        (w.tenant, case_id, json.dumps(args), json.dumps({"value": "1"})),
    ).fetchone()
    assert row
    return str(row[0])


def test_every_read_is_audited(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.drive(case_id)
    stack.call("POST", f"/api/cases/{case_id}/sign", "ana", 200)
    n = count(stack.w, "audit_log", "action = 'view'")
    stack.case(case_id)
    stack.case(case_id, "len")
    fid = stack.case(case_id)["findings"][0]["id"]
    stack.call("GET", f"/api/cases/{case_id}/findings/{fid}/trace", "ana", 200)
    assert count(stack.w, "audit_log", "action = 'view'") == n + 4
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        rows = conn.execute(
            "SELECT actor, object_kind FROM audit_log WHERE action = 'view' ORDER BY at, id"
        ).fetchall()
    assert rows[-4:] == [
        ("ana", "case"), ("len", "case"), ("ana", "case"), ("ana", "finding_trace"),
    ]  # fmt: skip


# ---------------------------------------------------------------- questions


def test_question_approval_writes_only_the_approval(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.drive(case_id)
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        row = conn.execute(
            "INSERT INTO borrower_question (tenant_id, case_id, body) VALUES (%s, %s, %s) "
            "RETURNING id",
            (stack.w.tenant, case_id, "Why did payouts slow in March?"),
        ).fetchone()
        assert row
        qid = str(row[0])
    qs = stack.call("GET", f"/api/cases/{case_id}/questions", "ana", 200).json()
    assert [(q["id"], q["approved"], q["sent"]) for q in qs] == [(qid, False, False)]

    tables = (
        "underwriter_action", "audit_log", "borrower_question", "model_call", "tool_call",
        "finding", "assessment", "agent_plan", "evidence_pull", "case_file",
    )  # fmt: skip
    before = {t: count(stack.w, t) for t in tables}
    stack.call("POST", f"/api/cases/{case_id}/questions/{qid}/approve", "bob", 403)
    stack.call("POST", f"/api/cases/{case_id}/questions/{qid}/approve", "len", 403)
    stack.call("POST", f"/api/cases/{case_id}/questions/{uuid.uuid4()}/approve", "ana", 404)
    r = stack.call("POST", f"/api/cases/{case_id}/questions/{qid}/approve", "ana", 200).json()
    assert r["approved"] is True and r["sent"] is False and "Nothing is sent" in r["note"]
    after = {t: count(stack.w, t) for t in tables}
    assert {t for t in tables if after[t] != before[t]} == {"underwriter_action"}
    assert after["underwriter_action"] == before["underwriter_action"] + 1
    assert (
        count(
            stack.w, "underwriter_action", f"action = 'approve_question' AND question_id = '{qid}'"
        )
        == 1
    )
    stack.call("POST", f"/api/cases/{case_id}/questions/{qid}/approve", "ana", 409)
    assert stack.call("GET", f"/api/cases/{case_id}/questions", "ana", 200).json()[0]["approved"]


# ---------------------------------------------------------------- send-back


def test_send_back_logs_request_and_reruns(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.call("POST", f"/api/cases/{case_id}/send-back", "ana", 409, json={"question": "why?"})
    stack.drive(case_id)
    stack.call("POST", f"/api/cases/{case_id}/send-back", "ana", 422, json={"question": " "})
    stack.call("POST", f"/api/cases/{case_id}/send-back", "ana", 202, json={"question": "why?"})
    assert stack.case(case_id)["status"] == "review"
    assert (
        count(stack.w, "underwriter_action", f"case_id = '{case_id}' AND action = 'request_more'")
        == 1
    )


def test_failed_investigation_restores_status(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
    stack.call("POST", f"/api/cases/{case_id}/plan/approve", "ana", 200)

    class Boom:
        def complete(self, **_: Any) -> Any:
            raise RuntimeError("model down")

    app = stack.http.app
    app.state.settings = app.state.settings.__class__(  # type: ignore[attr-defined]
        **{**app.state.settings.__dict__, "client_factory": Boom}  # type: ignore[attr-defined]
    )
    stack.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)
    assert stack.case(case_id)["status"] == "planning"
    assert any(a[0] == "investigation_failed" for a in audit_actions(stack.w, case_id))
    # retry works once the model is back
    app.state.settings = app.state.settings.__class__(  # type: ignore[attr-defined]
        **{**app.state.settings.__dict__, "client_factory": NullClient}  # type: ignore[attr-defined]
    )
    stack.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)
    assert stack.case(case_id)["status"] == "review"


# ---------------------------------------------------------------- run visibility and recovery


def set_client(stack: Stack, factory: Any) -> None:
    app = stack.http.app
    app.state.settings = dataclasses.replace(app.state.settings, client_factory=factory)  # type: ignore[attr-defined]


def seed_run(
    w: World, case_id: str, *, age_min: float, detail: dict[str, Any] | None = None
) -> None:
    """Leave a case 'investigating' with a started row `age_min` minutes old (as after a server
    restart mid-run). audit_log is append-only, so the age is set at insert."""
    with tenant_session(w.url, w.tenant) as conn:
        set_case_status(conn, case_id, "investigating")
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor, action, object_kind, object_id, detail, at) "
            "VALUES (%s, 'ana', 'investigation_started', 'case', %s, %s::jsonb, "
            "now() - make_interval(secs => %s))",
            (w.tenant, case_id, json.dumps(detail or {}), age_min * 60),
        )


def test_started_row_records_prior_status(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.drive(case_id)
    stack.call("POST", f"/api/cases/{case_id}/send-back", "ana", 202, json={"question": "why?"})
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'investigation_started' "
            "AND object_id = %s ORDER BY at, id",
            (case_id,),
        ).fetchall()
    assert [(d["prior_status"], d["send_back"]) for (d,) in rows] == [
        ("planning", False),
        ("review", True),
    ]


def test_failure_is_in_case_detail_until_the_next_start(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
    stack.call("POST", f"/api/cases/{case_id}/plan/approve", "ana", 200)
    assert stack.case(case_id)["last_failure"] is None

    class Boom:
        def complete(self, **_: Any) -> Any:
            raise RuntimeError("model down")

    set_client(stack, Boom)
    stack.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)
    f = stack.case(case_id)["last_failure"]
    assert f["error"] == "RuntimeError: model down" and f["restored_status"] == "planning"
    assert "model down" in f["message"] and f["at"]
    set_client(stack, NullClient)
    stack.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)
    d = stack.case(case_id)
    assert d["status"] == "review" and d["last_failure"] is None


@pytest.mark.parametrize(
    ("stored", "expect"),
    [
        (
            "ModelConfigError: set TANGIBLE_BEDROCK_MODEL_ID, TANGIBLE_BEDROCK_REGION to use the "
            "Bedrock client",
            "The model is not configured: set TANGIBLE_BEDROCK_MODEL_ID, TANGIBLE_BEDROCK_REGION "
            "to use the Bedrock client.",
        ),
        (
            "ClientError: An error occurred (AccessDeniedException) when calling the Converse "
            "operation: You don't have access to the model",
            "The model provider denied access",
        ),
        ("ClientError: An error occurred (ThrottlingException)", "over its rate or quota limit"),
        ("RuntimeError: model down", "RuntimeError: model down."),
        ("", "failed for an unknown reason"),
    ],
)
def test_plain_error_reads_as_a_sentence(stored: str, expect: str) -> None:
    assert expect in services.plain_error(stored)


def test_reset_only_when_stuck(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANGIBLE_STUCK_AFTER_MIN", raising=False)
    case_id = stack.new_case()
    url = f"/api/cases/{case_id}/reset-investigation"
    body = {"reason": "server restarted"}

    # not investigating
    stack.call("POST", url, "ana", 409, json=body)
    # investigating but started 5 minutes ago (default threshold 15)
    seed_run(stack.w, case_id, age_min=5, detail={"prior_status": "review"})
    r = stack.call("POST", url, "ana", 409, json=body)
    assert "after 15 minutes" in r.json()["detail"]
    assert stack.case(case_id)["run"]["stuck"] is False
    # roles and the required reason are checked before anything changes
    stack.call("POST", url, "bob", 403, json=body)
    stack.call("POST", url, "len", 403, json=body)
    stack.call("POST", url, "ana", 422, json={"reason": "  "})
    assert stack.case(case_id)["status"] == "investigating"
    assert count(stack.w, "audit_log", "action = 'investigation_reset'") == 0

    # the threshold is configurable; garbage falls back to the default
    monkeypatch.setenv("TANGIBLE_STUCK_AFTER_MIN", "2")
    assert stack.case(case_id)["run"]["stuck"] is True
    monkeypatch.setenv("TANGIBLE_STUCK_AFTER_MIN", "not a number")
    assert stack.case(case_id)["run"]["stuck_after_min"] == 15
    monkeypatch.delenv("TANGIBLE_STUCK_AFTER_MIN")

    # only the latest start counts: an older row does not make a fresh run look stuck
    seed_run(stack.w, case_id, age_min=20, detail={"prior_status": "review"})
    stack.call("POST", url, "ana", 409, json=body)

    case2 = stack.new_case()
    seed_run(stack.w, case2, age_min=20, detail={"prior_status": "review"})
    started = count(stack.w, "audit_log", "action = 'investigation_started'")
    before = {t: count(stack.w, t) for t in ("finding", "tool_call", "model_call", "agent_plan")}
    r = stack.call("POST", f"/api/cases/{case2}/reset-investigation", "ana", 200, json=body)
    assert r.json() == {"case_id": case2, "status": "review", "reset": True}
    d = stack.case(case2)
    assert d["status"] == "review" and d["run"] is None
    assert d["last_reset"]["reason"] == "server restarted" and d["last_reset"]["by"] == "ana"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        row = conn.execute(
            "SELECT actor, detail FROM audit_log WHERE action = 'investigation_reset' "
            "AND object_id = %s",
            (case2,),
        ).fetchone()
    assert row
    assert row[0] == "ana" and row[1]["reason"] == "server restarted"
    assert row[1]["restored_status"] == "review"
    # nothing was deleted
    assert count(stack.w, "audit_log", "action = 'investigation_started'") == started
    assert {t: count(stack.w, t) for t in before} == before
    stack.call("POST", f"/api/cases/{case2}/reset-investigation", "ana", 409, json=body)


def test_reset_falls_back_to_planning_and_admin_may_reset(stack: Stack) -> None:
    case_id = stack.new_case()
    seed_run(stack.w, case_id, age_min=60)  # an old row without prior_status
    url = f"/api/cases/{case_id}/reset-investigation"
    stack.call("POST", url, "len", 403, json={"reason": "x"})
    r = stack.call("POST", url, "adm", 200, json={"reason": "restart overnight"})
    assert r.json()["status"] == "planning"
    assert ("investigation_reset", "case", "adm") in audit_actions(stack.w, case_id)
    # the analyst can start over; the reset notice is hidden once a later start happened
    assert stack.case(case_id)["last_reset"]["by"] == "adm"
    stack.drive(case_id)
    assert stack.case(case_id)["status"] == "review"
    assert stack.case(case_id)["last_reset"] is None


def test_unmatched_numbers_in_detail_and_trace(stack: Stack) -> None:
    case_id = stack.new_case()
    stack.drive(case_id)
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        call_id = _insert_tool_call(conn, stack.w, case_id, {"series": "payouts"})  # result 1
        ids = {}
        for name, verified, summary in (
            ("bad", False, "payouts fell 42% across 1 series"),
            ("good", True, "payouts hold at 1"),
            ("unchecked", None, "payouts fell 42%"),
        ):
            ids[name] = insert_finding(
                conn, stack.w.tenant, stack.w.borrower,
                FindingDraft(f"agent_{name}", 1, "review", "integrity", summary),
                origin="model", case_id=case_id, tool_call_ids=[call_id], verified=verified,
            )  # fmt: skip
    by_key = {f["check_key"]: f for f in stack.case(case_id)["findings"]}
    assert by_key["agent_bad"]["unmatched_numbers"] == ["42%"]
    assert by_key["agent_good"]["unmatched_numbers"] is None
    assert by_key["agent_unchecked"]["unmatched_numbers"] is None
    t = stack.call("GET", f"/api/cases/{case_id}/findings/{ids['bad']}/trace", "ana", 200).json()
    assert t["finding"]["unmatched_numbers"] == ["42%"]
    # display only: nothing about it is stored on the finding row
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'finding'"
            ).fetchall()
        }
    assert not any("unmatched" in c for c in cols)


def test_cost_text_says_when_unpriced(stack: Stack) -> None:
    plan = {"business_model": "retailer", "risks": [], "notes": ""}

    def scripted() -> ScriptedClient:
        return ScriptedClient(
            [ModelResponse(text=json.dumps(plan), model_id="m", input_tokens=100, output_tokens=50)]
        )

    case_id = stack.new_case()
    assert stack.case(case_id)["usage"]["cost_text"] == "$0"  # no model calls: nothing to flag
    set_client(stack, scripted)
    stack.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
    u = stack.case(case_id)["usage"]
    assert u["model_calls"] == 1 and u["priced"] is False
    assert u["cost_text"] == "cost not priced (set TANGIBLE_PRICE_*)"

    app = stack.http.app
    app.state.settings = dataclasses.replace(  # type: ignore[attr-defined]
        app.state.settings,  # type: ignore[attr-defined]
        pricing=Pricing(Decimal(3), Decimal(15)),
    )
    set_client(stack, scripted)
    stack.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
    u = stack.case(case_id)["usage"]
    assert u["priced"] is True and u["cost_text"] == "$0.001050"
