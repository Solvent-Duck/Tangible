import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tangible.agent import sandbox
from tangible.agent.tools import (
    ToolContext,
    call_tool,
    open_context,
    tool_definitions,
)
from tangible.checks.registry import run_check as direct_check
from tangible.checks.sets import check_set
from tangible.datasets.synthetic import generate
from tangible.db.cases import Case, create_case
from tangible.db.findings import load_findings
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.metrics.registry import compute
from tangible.metrics.sets import metric_set
from tangible.policy import load_policy
from test_synthetic import write_lines

pytestmark = pytest.mark.db

POLICY = load_policy(Path(__file__).parents[1] / "policies" / "default.yaml")


@pytest.fixture(scope="module")
def world(test_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, datetime]:
    root = tmp_path_factory.mktemp("agent_tools")
    lines = write_lines(root / "lines.csv", days=90)
    generate(lines, root / "out", seed=5)
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('tools') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        ingest_synthetic_dir(
            conn, LocalEvidenceStore(root / "store"), tenant, borrower, root / "out", lines
        )
        ledger = load_ledger(conn, borrower)
    last = max(t.posted_on for t in ledger.bank_txns)
    as_of = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    return tenant, borrower, as_of


@pytest.fixture
def ctx(
    test_db_url: str, world: tuple[str, str, datetime], tmp_path: Path
) -> Iterator[ToolContext]:
    tenant, borrower, as_of = world
    with tenant_session(test_db_url, tenant) as conn:
        case = create_case(conn, tenant, borrower, as_of)
        yield open_context(conn, case, POLICY, tmp_path)


def n_calls(ctx: ToolContext) -> int:
    row = ctx.conn.execute(
        "SELECT count(*) FROM tool_call WHERE case_id = %s", (ctx.case.id,)
    ).fetchone()
    assert row
    return int(row[0])


def query(ctx: ToolContext, sql: str) -> Any:
    return call_tool(ctx, "query_ledger", {"sql": sql})


# ---------------------------------------------------------------- query_ledger


def test_definitions() -> None:
    names = {d["name"] for d in tool_definitions()}
    assert names == {
        "query_ledger", "compute_metric", "run_check", "run_analysis",
        "record_finding", "draft_borrower_question", "web_lookup",
    }  # fmt: skip
    rf = next(d for d in tool_definitions() if d["name"] == "record_finding")
    assert set(rf["input_schema"]["properties"]).isdisjoint({"band", "limit", "value"})


def test_query_select(ctx: ToolContext) -> None:
    res = query(ctx, "SELECT count(*), sum(gross_minor) FROM orders")
    assert res.error is None
    assert res.result["columns"] == ["count(*)", "sum(gross_minor)"]
    assert res.result["rows"][0][0] == len(ctx.known.orders)
    assert res.result["rows"][0][1] == sum(o.gross for o in ctx.known.orders) * 100
    assert res.result["truncated"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (source_ref) VALUES ('x')",
        "UPDATE orders SET gross = '0'",
        "DELETE FROM orders",
        "ATTACH DATABASE ':memory:' AS x",
        "PRAGMA writable_schema=ON",
        "PRAGMA query_only=OFF",
        "CREATE TABLE t (a)",
        "DROP TABLE orders",
        "SELECT 1; DELETE FROM orders",
    ],
)
def test_query_rejects_writes(ctx: ToolContext, sql: str) -> None:
    before = ctx.snapshot.path.read_bytes()
    res = query(ctx, sql)
    assert res.error and res.result is None
    assert ctx.snapshot.path.read_bytes() == before
    assert query(ctx, "SELECT count(*) FROM orders").result["rows"][0][0] == len(ctx.known.orders)


def test_query_row_cap(ctx: ToolContext) -> None:
    res = query(ctx, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<5000) "
                "SELECT i FROM n")  # fmt: skip
    assert res.error is None
    assert len(res.result["rows"]) == 1000 and res.result["truncated"] is True


def test_query_runaway_aborted(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from tangible.agent.tools import ledger

    monkeypatch.setattr(ledger, "TIME_LIMIT_S", 0.3)
    res = query(
        ctx,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n",
    )
    assert res.error and "aborted" in res.error


# ---------------------------------------------------------------- metrics and checks


def test_compute_metric_matches_direct(ctx: ToolContext) -> None:
    ledger = load_ledger(ctx.conn, ctx.case.borrower_id)
    for key, version in metric_set(POLICY.parsed.metric_set)[:6]:  # type: ignore[union-attr]
        res = call_tool(ctx, "compute_metric", {"key": key})
        assert res.error is None
        want = compute(key, version, ledger, ctx.case.as_of)
        assert res.result == {
            "key": key,
            "version": version,
            "as_of": ctx.case.as_of.isoformat(),
            "value": None if want is None else str(want),
        }


def test_run_check_matches_direct(ctx: ToolContext) -> None:
    ledger = load_ledger(ctx.conn, ctx.case.borrower_id)
    for key, version in check_set(POLICY.parsed.check_set):  # type: ignore[union-attr]
        res = call_tool(ctx, "run_check", {"key": key})
        assert res.error is None
        drafts = direct_check(key, version, ledger, ctx.case.as_of)
        assert [f["summary"] for f in res.result["findings"]] == [d.summary for d in drafts]
        assert [f["evidence_refs"] for f in res.result["findings"]] == [
            list(d.evidence_refs) for d in drafts
        ]
        assert [f["facts"] for f in res.result["findings"]] == [dict(d.facts) for d in drafts]


def test_unknown_metric_and_check(ctx: ToolContext) -> None:
    assert call_tool(ctx, "compute_metric", {"key": "nope"}).error
    assert call_tool(ctx, "run_check", {"key": "nope"}).error


# ---------------------------------------------------------------- record_finding


def finding_args(ids: list[str], **over: Any) -> dict[str, Any]:
    return {
        "summary": "Revenue looks odd",
        "severity": "review",
        "pillar": "integrity",
        "direction": "raises_risk",
        "tool_call_ids": ids,
        "slug": "odd-revenue",
    } | over


def test_record_finding_validation(ctx: ToolContext, test_db_url: str) -> None:
    good = query(ctx, "SELECT 1")
    bad = query(ctx, "DELETE FROM orders")
    assert bad.error
    other = create_case(ctx.conn, ctx.case.tenant_id, ctx.case.borrower_id, ctx.case.as_of)
    other_ctx = ToolContext(
        ctx.conn, other, ctx.ledger, ctx.known, ctx.snapshot, ctx.metric_set_id, ctx.check_set_id
    )
    foreign = query(other_ctx, "SELECT 1")
    fake = "00000000-0000-4000-8000-000000000000"
    for ids in ([fake], [bad.call_id], [foreign.call_id], ["not-a-uuid"], [good.call_id, fake]):
        res = call_tool(ctx, "record_finding", finding_args(ids))
        assert res.error, ids
    assert load_findings(ctx.conn, case_id=ctx.case.id) == []


def test_record_finding_rejects_band_and_limit(ctx: ToolContext) -> None:
    good = query(ctx, "SELECT 1")
    for extra in ({"band": "A"}, {"limit": 1000}, {"value": "1.5"}):
        res = call_tool(ctx, "record_finding", finding_args([good.call_id]) | extra)
        assert res.error and "unexpected property" in res.error
    assert load_findings(ctx.conn, case_id=ctx.case.id) == []


def test_record_finding_without_verifier(ctx: ToolContext) -> None:
    good = query(ctx, "SELECT count(*) FROM orders")
    res = call_tool(ctx, "record_finding", finding_args([good.call_id], evidence_refs=["p#row=1"]))
    assert res.error is None
    (f,) = load_findings(ctx.conn, case_id=ctx.case.id)
    assert f.origin == "model" and f.verified is None
    assert f.draft.check_key == "agent:odd-revenue" and f.draft.check_version == 1
    assert f.draft.evidence_refs == ("p#row=1",)
    assert f.tool_call_ids == (good.call_id,)


@pytest.mark.parametrize("verdict", [True, False])
def test_record_finding_with_verifier(ctx: ToolContext, verdict: bool) -> None:
    seen: list[tuple[str, list[object]]] = []

    def verifier(summary: str, results: list[object]) -> bool:
        seen.append((summary, results))
        return verdict

    ctx.verifier = verifier
    good = query(ctx, "SELECT count(*) FROM orders")
    res = call_tool(ctx, "record_finding", finding_args([good.call_id]))
    assert res.error is None
    (f,) = load_findings(ctx.conn, case_id=ctx.case.id)
    assert f.verified is verdict
    assert seen == [("Revenue looks odd", [good.result])]


# ---------------------------------------------------------------- questions


def test_draft_borrower_question(ctx: ToolContext) -> None:
    res = call_tool(ctx, "draft_borrower_question", {"body": "Why did refunds spike?"})
    assert res.error is None
    rows = ctx.conn.execute(
        "SELECT body, drafted_by_tool_call, case_id FROM borrower_question WHERE case_id = %s",
        (ctx.case.id,),
    ).fetchall()
    assert [(r[0], str(r[1]), str(r[2])) for r in rows] == [
        ("Why did refunds spike?", res.call_id, ctx.case.id)
    ]
    assert not ctx.conn.execute(
        "SELECT 1 FROM underwriter_action WHERE case_id = %s", (ctx.case.id,)
    ).fetchall()


# ---------------------------------------------------------------- analysis


def test_run_analysis(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def fake(code: str, db: Path, *a: Any) -> sandbox.SandboxResult:
        calls.append((code, db))
        return sandbox.SandboxResult("hi\n", {"n": 3}, None, 4)

    monkeypatch.setattr(sandbox, "run_python", fake)
    res = call_tool(ctx, "run_analysis", {"code": "RESULT = {'n': 3}"})
    assert res.error is None
    assert res.result == {"result": {"n": 3}, "stdout": "hi\n", "error": None, "duration_ms": 4}
    assert calls == [("RESULT = {'n': 3}", ctx.snapshot.path)]
    row = ctx.conn.execute("SELECT args FROM tool_call WHERE id = %s", (res.call_id,)).fetchone()
    assert row and row[0] == {"code": "RESULT = {'n': 3}"}


def test_failed_analysis_cannot_support_finding(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox, "run_python", lambda *a, **k: sandbox.SandboxResult("", None, "timeout", 1)
    )
    res = call_tool(ctx, "run_analysis", {"code": "while True: pass"})
    assert res.result["error"] == "timeout"
    assert call_tool(ctx, "record_finding", finding_args([res.call_id])).error


# ---------------------------------------------------------------- logging


def test_every_call_logged_once(ctx: ToolContext) -> None:
    ok = query(ctx, "SELECT count(*) FROM orders")
    err = query(ctx, "SELECT * FROM missing_table")
    unknown = call_tool(ctx, "launch_missiles", {})
    invalid = call_tool(ctx, "query_ledger", {"sql": 5})
    extra = call_tool(ctx, "query_ledger", {"sql": "SELECT 1", "x": 1})
    notdict = call_tool(ctx, "query_ledger", "SELECT 1")
    results = [ok, err, unknown, invalid, extra, notdict]
    assert [r.error is None for r in results] == [True, False, False, False, False, False]
    assert n_calls(ctx) == 6
    rows = {
        str(r[0]): r
        for r in ctx.conn.execute(
            "SELECT id, tool, tool_version, args, result, result_sha256, error, duration_ms "
            "FROM tool_call WHERE case_id = %s",
            (ctx.case.id,),
        ).fetchall()
    }
    assert set(rows) == {r.call_id for r in results}
    r = rows[ok.call_id]
    canon = json.dumps(ok.result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert r[1:4] == ("query_ledger", 1, {"sql": "SELECT count(*) FROM orders"})
    assert r[4] == ok.result and r[5] == hashlib.sha256(canon.encode()).hexdigest()
    assert r[6] is None and r[7] >= 0
    for bad in (err, unknown, invalid, extra, notdict):
        row = rows[bad.call_id]
        assert row[4] is None and row[5] is None and row[6] == bad.error
    assert rows[unknown.call_id][1] == "launch_missiles"


def test_tool_call_append_only(ctx: ToolContext) -> None:
    res = query(ctx, "SELECT 1")
    for stmt in (
        "UPDATE tool_call SET tool = 'x' WHERE id = %s",
        "DELETE FROM tool_call WHERE id = %s",
    ):
        with pytest.raises(psycopg.Error), ctx.conn.transaction():
            ctx.conn.execute(stmt, (res.call_id,))
    assert n_calls(ctx) == 1


def test_context_snapshot_matches_known(ctx: ToolContext) -> None:
    assert ctx.snapshot.row_counts["bank_txns"] == len(ctx.known.bank_txns)
    con = sqlite3.connect(ctx.snapshot.path)
    try:
        (n,) = con.execute("SELECT count(*) FROM payouts").fetchone()
    finally:
        con.close()
    assert n == len(ctx.known.payouts)
    assert isinstance(ctx.case, Case) and ctx.case.as_of.tzinfo is not None
