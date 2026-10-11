"""tangible-demo seed on a tiny lines fixture (two merchants, so CI stays fast)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tangible.datasets.synthetic import DEFAULT_PROFILE
from tangible.db.repo import load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.demo import seed as seed_mod
from tangible.demo.cli import main as demo_main
from tangible.demo.seed import (
    SPECS,
    BorrowerSpec,
    ConnectionSpec,
    SeedError,
    baseline_loud,
    connection_story,
    connections_of,
    seed_demo,
    verify_story,
)
from tangible.policy import load_policy
from test_assessment import POLICY_PATH
from test_synthetic import write_lines

pytestmark = pytest.mark.db

END_ON = date(2026, 10, 3)
TINY_SPECS = (
    BorrowerSpec("Harbour & Pine Homeware Ltd", 0, (), 101, Decimal("75000"), "open"),
    BorrowerSpec(
        "Kestrel Gift Co Ltd", 1, ("hidden_reserve",), 102, Decimal("120000"), "signed"
    ),
)  # fmt: skip


@pytest.fixture(scope="module")
def lines(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_lines(tmp_path_factory.mktemp("demo") / "lines.csv", days=200)


def run_seed(
    url: str,
    lines: Path,
    tmp_path: Path,
    now: datetime,
    specs: tuple[BorrowerSpec, ...] = TINY_SPECS,
    **kw: object,
) -> seed_mod.SeedSummary:
    return seed_demo(
        end_on=END_ON, store=tmp_path / "evidence", work=tmp_path / "work", lines=lines,
        database_url=url, specs=specs, n_partitions=2, now=now, log=lambda _: None,
        **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def count(url: str, tenant: str, table: str, where: str = "true") -> int:
    with tenant_session(url, tenant) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()
    assert row
    return int(row[0])


def test_seed_creates_tenant_users_borrowers_and_open_cases(
    test_db_url: str, lines: Path, tmp_path: Path
) -> None:
    s = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 14, 32))
    assert s.tenant_name == "demo-20261010-1432"
    assert s.login_url == f"http://localhost:3000/sign-in?tenant={s.tenant_id}"
    assert s.end_on == s.requested_end_on == END_ON and not s.moved
    t = s.tenant_id

    with tenant_session(test_db_url, t) as conn:
        users = dict(conn.execute("SELECT subject, role FROM app_user").fetchall())
        names = {r[0] for r in conn.execute("SELECT name FROM borrower").fetchall()}
        cases = conn.execute(
            "SELECT status, assigned_analyst, loan_amount, as_of FROM case_file"
        ).fetchall()
    assert users == {"ana": "analyst", "adm": "credit_admin", "len": "lender_underwriter"}
    assert names == {sp.name for sp in TINY_SPECS}
    assert len(cases) == len(TINY_SPECS)
    assert {c[0] for c in cases} == {"open"}  # no model, so nothing is staged beyond open
    assert {c[1] for c in cases} == {"ana"}
    assert {c[2] for c in cases} == {Decimal("75000"), Decimal("120000")}
    assert count(test_db_url, t, "agent_plan") == 0

    for b in s.borrowers:
        assert b.stage == "open"
        assert b.last_day <= END_ON and (END_ON - b.last_day).days < 7
        want = datetime.combine(b.last_day, time(23, 59, 59), UTC)
        with tenant_session(test_db_url, t) as conn:
            row = conn.execute("SELECT as_of FROM case_file WHERE id = %s", (b.case_id,)).fetchone()
        assert row and row[0] == want
        assert b.shift_days % 7 == 0
    assert count(test_db_url, t, "orders") > 0
    assert count(test_db_url, t, "evidence_pull") > 0
    text = s.render()
    assert s.tenant_id in text and "hidden_reserve" in text and "Kestrel Gift Co Ltd" in text


def test_story_is_verified_in_the_summary(test_db_url: str, lines: Path, tmp_path: Path) -> None:
    s = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 14, 33))
    by_name = {b.name: b for b in s.borrowers}
    assert by_name["Harbour & Pine Homeware Ltd"].loud_checks == ()
    kestrel = dict(by_name["Kestrel Gift Co Ltd"].loud_checks)
    assert kestrel["payouts_vs_deposits"] == "hold"

    policy = load_policy(POLICY_PATH)
    clean = by_name["Harbour & Pine Homeware Ltd"]
    as_of = datetime.combine(clean.last_day, time(23, 59, 59), UTC)
    with tenant_session(test_db_url, s.tenant_id) as conn:
        assert baseline_loud(conn, clean.borrower_id, as_of, policy) == []


def test_seed_is_rerunnable_and_leaves_earlier_data_alone(
    test_db_url: str, lines: Path, tmp_path: Path
) -> None:
    first = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 15, 0))
    before = {
        t: count(test_db_url, first.tenant_id, t) for t in ("orders", "bank_txn", "case_file")
    }
    second = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 15, 0))  # same minute
    assert second.tenant_id != first.tenant_id
    assert second.tenant_name == first.tenant_name
    with admin_connect(test_db_url) as admin:
        row = admin.execute(
            "SELECT count(*) FROM tenant WHERE id IN (%s, %s)", (first.tenant_id, second.tenant_id)
        ).fetchone()
    assert row and row[0] == 2
    assert {b.case_id for b in first.borrowers}.isdisjoint(b.case_id for b in second.borrowers)
    assert before == {
        t: count(test_db_url, first.tenant_id, t) for t in ("orders", "bank_txn", "case_file")
    }
    assert count(test_db_url, second.tenant_id, "case_file") == len(TINY_SPECS)
    # Another tenant's data is invisible through RLS.
    assert count(test_db_url, second.tenant_id, "borrower") == len(TINY_SPECS)


def test_clean_borrower_with_findings_fails_loudly(
    test_db_url: str, lines: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        seed_mod, "baseline_loud", lambda *a, **k: [("payouts_vs_deposits", "hold")]
    )
    with pytest.raises(SeedError, match="meant to be clean"):
        run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 16, 0))


def test_data_moves_back_a_week_when_the_clean_borrower_is_not_clean(
    test_db_url: str, lines: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = seed_mod.loud_findings
    calls: list[int] = []

    def flaky(*a: Any, **k: Any) -> list[tuple[str, str]]:
        calls.append(1)
        return [("books_vs_processor", "review")] if len(calls) == 1 else real(*a, **k)

    monkeypatch.setattr(seed_mod, "loud_findings", flaky)
    s = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 18, 0))
    assert s.requested_end_on == END_ON and s.end_on == END_ON - timedelta(days=7)
    assert s.moved and "requested" in s.render()
    assert all(b.last_day <= s.end_on for b in s.borrowers)


def test_seed_gives_up_when_no_nearby_week_is_clean(
    test_db_url: str, lines: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        seed_mod, "loud_findings", lambda *a, **k: [("books_vs_processor", "review")]
    )
    with pytest.raises(SeedError, match="no clean data within"):
        run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 18, 1))
    with admin_connect(test_db_url) as admin:  # failed before anything reached the database
        row = admin.execute("SELECT count(*) FROM tenant WHERE name = 'demo-20261010-1801'")
        assert row.fetchone() == (0,)


def test_verify_story_rules() -> None:
    clean = BorrowerSpec("A Ltd", 0, (), 1, Decimal(1), "open")
    verify_story(clean, [])
    with pytest.raises(SeedError, match="clean"):
        verify_story(clean, [("books_vs_processor", "review")])
    hidden = BorrowerSpec("B Ltd", 0, ("hidden_reserve",), 1, Decimal(1), "open")
    verify_story(hidden, [("payouts_vs_deposits", "hold")])
    with pytest.raises(SeedError, match="payouts_vs_deposits"):
        verify_story(hidden, [])


def test_default_specs_are_the_designed_stories() -> None:
    by_name = {s.name: s for s in SPECS}
    assert by_name["Harbour & Pine Homeware Ltd"].plants == ()
    assert by_name["Kestrel Gift Co Ltd"].plants == ("hidden_reserve",)
    assert by_name["Marlow Lane Interiors Ltd"].plants == ("undisclosed_lender_debit",)
    assert set(by_name["Brightwell Crafts Ltd"].plants) == {
        "self_transfer_revenue",
        "phantom_payouts",
    }
    assert by_name["Oakden Trading Ltd"].plants == ("partial_coverage",)
    assert {s.name: s.stage for s in SPECS} == {
        "Harbour & Pine Homeware Ltd": "open",
        "Kestrel Gift Co Ltd": "signed",
        "Marlow Lane Interiors Ltd": "signed",
        "Brightwell Crafts Ltd": "review",
        "Oakden Trading Ltd": "review",
        "Juniper & Rye Bakery LLC": "open",
        "Northgate Phone Repair LLC": "open",
    }
    # The US intangibles borrowers carry an identity for the search team; the UK ones do not.
    assert {s.name: s.identity for s in SPECS if s.identity} == {
        "Juniper & Rye Bakery LLC": "juniper_rye",
        "Northgate Phone Repair LLC": "northgate",
    }
    assert len({s.partition for s in SPECS}) == len(SPECS)  # one merchant each
    assert all(Decimal(25000) <= s.loan_amount <= Decimal(150000) for s in SPECS)


def test_run_agent_needs_a_configured_live_model(
    test_db_url: str, lines: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 10, 17, 0)
    monkeypatch.delenv("TANGIBLE_MODEL_BACKEND", raising=False)
    with pytest.raises(SeedError, match="needs a live model"):
        run_seed(test_db_url, lines, tmp_path, now, run_agent=True)
    monkeypatch.setenv("TANGIBLE_MODEL_BACKEND", "bedrock")
    monkeypatch.delenv("TANGIBLE_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("TANGIBLE_BEDROCK_REGION", raising=False)
    with pytest.raises(SeedError, match="TANGIBLE_BEDROCK_MODEL_ID"):
        run_seed(test_db_url, lines, tmp_path, now, run_agent=True)
    monkeypatch.delenv("TANGIBLE_MODEL_BACKEND")
    with pytest.raises(SystemExit) as e:
        demo_main(["seed", "--run-agent", "--lines", str(lines), "--work", str(tmp_path)])
    assert e.value.code == 1


# ---------------------------------------------------------------- identities and connections


def connection_rows(url: str, tenant: str, borrower_id: str) -> dict[str, tuple[Any, ...]]:
    with tenant_session(url, tenant) as conn:
        rows = conn.execute(
            "SELECT source, granted_at, revoked_at, last_success_at, scopes, last_error "
            "FROM connection WHERE borrower_id = %s",
            (borrower_id,),
        ).fetchall()
    return {r[0]: tuple(r) for r in rows}


def test_each_borrower_gets_connections_to_its_processor_and_bank(
    test_db_url: str, lines: Path, tmp_path: Path
) -> None:
    s = run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 19, 0))
    for b, spec in zip(s.borrowers, TINY_SPECS, strict=True):
        rows = connection_rows(test_db_url, s.tenant_id, b.borrower_id)
        assert set(rows) == {spec.profile.processor_source, "plaid"}
        as_of = datetime.combine(b.last_day, time(23, 59, 59), UTC)
        for _, granted, revoked, last_success, scopes, err in rows.values():
            assert granted == datetime.combine(b.first_day, time(9), UTC)  # the start of its data
            assert revoked is None and err is None and scopes
            assert timedelta(0) <= as_of - last_success <= timedelta(days=2)
        assert len(b.connections) == 2
    assert "connections: " in s.render()


def test_stale_and_revoked_connections_fire_as_review_and_hold(
    test_db_url: str, lines: Path, tmp_path: Path
) -> None:
    story = BorrowerSpec(
        "Brightwell Crafts Ltd", 1, ("hidden_reserve",), 102, Decimal("90000"), "review",
        connections=(
            ConnectionSpec("shopify", last_success_ago=timedelta(days=19),
                           last_error="401 invalid_grant"),
            ConnectionSpec("plaid", last_success_ago=timedelta(days=4),
                           revoked_ago=timedelta(days=3)),
        ),
    )  # fmt: skip
    assert connection_story(story) == {"stale_connection", "revoked_mid_review"}
    now = datetime(2026, 10, 10, 19, 1)
    s = run_seed(test_db_url, lines, tmp_path, now, (TINY_SPECS[0], story))
    clean, planted = s.borrowers
    assert clean.loud_checks == ()  # healthy connections leave the clean one clean
    loud = dict(planted.loud_checks)
    assert loud["stale_connection"] == "review" and loud["revoked_mid_review"] == "hold"
    assert loud["payouts_vs_deposits"] == "hold"

    rows = connection_rows(test_db_url, s.tenant_id, planted.borrower_id)
    as_of = datetime.combine(planted.last_day, time(23, 59, 59), UTC)
    assert rows["shopify"][3] == as_of - timedelta(days=19) and rows["shopify"][2] is None
    assert rows["shopify"][5] == "401 invalid_grant"
    assert rows["plaid"][2] == as_of - timedelta(days=3)
    assert rows["plaid"][3] <= rows["plaid"][2]  # no successful pull after the revocation
    # Time rule: nothing here is known before its grant.
    with tenant_session(test_db_url, s.tenant_id) as conn:
        ledger = load_ledger(conn, planted.borrower_id)
    assert len(ledger.known(as_of).connections) == 2
    before = datetime.combine(planted.first_day, time(8), UTC)
    assert ledger.known(before).connections == ()


def test_a_connection_that_does_not_fit_the_data_fails_before_a_wrong_demo(
    test_db_url: str, lines: Path, tmp_path: Path
) -> None:
    bad = BorrowerSpec(
        "Too Old Ltd", 0, (), 101, Decimal(1), "open",
        connections=(ConnectionSpec("shopify", last_success_ago=timedelta(days=900)),),
    )  # fmt: skip
    with pytest.raises(SeedError, match="does not fit the data"):
        run_seed(test_db_url, lines, tmp_path, datetime(2026, 10, 10, 19, 2), (bad,))


def test_verify_story_checks_the_connection_story() -> None:
    stale = BorrowerSpec(
        "A Ltd", 0, (), 1, Decimal(1), "open",
        connections=(ConnectionSpec("shopify", last_success_ago=timedelta(days=19)),),
    )  # fmt: skip
    verify_story(stale, [("stale_connection", "review")])
    with pytest.raises(SeedError, match="stale_connection"):
        verify_story(stale, [])
    with pytest.raises(SeedError, match="stale_connection"):
        verify_story(stale, [("stale_connection", "hold")])  # wrong severity
    revoked = BorrowerSpec(
        "B Ltd", 0, (), 1, Decimal(1), "open",
        connections=(ConnectionSpec("plaid", revoked_ago=timedelta(days=3)),),
    )  # fmt: skip
    verify_story(revoked, [("revoked_mid_review", "hold")])
    with pytest.raises(SeedError, match="revoked_mid_review"):
        verify_story(revoked, [])
    # A connection check firing without a designed story is a wrong demo, planted or not.
    healthy = BorrowerSpec("C Ltd", 0, ("hidden_reserve",), 1, Decimal(1), "open")
    with pytest.raises(SeedError, match="stale_connection fired"):
        verify_story(healthy, [("payouts_vs_deposits", "hold"), ("stale_connection", "review")])
    # Revoked longer ago than the look-back is not a story the check can see.
    old = BorrowerSpec(
        "D Ltd", 0, (), 1, Decimal(1), "open",
        connections=(ConnectionSpec("plaid", revoked_ago=timedelta(days=60)),),
    )  # fmt: skip
    assert connection_story(old) == frozenset()


def test_default_specs_have_distinct_identities_and_the_designed_connections() -> None:
    profiles = [s.profile for s in SPECS]
    assert DEFAULT_PROFILE not in profiles
    for field in ("account_ref", "owner_account", "lender_name", "bank_name", "landlord"):
        assert len({getattr(p, field) for p in profiles}) == len(SPECS), field
    assert len({p.supplier_names for p in profiles}) == len(SPECS)
    assert all(
        p.account_ref[:3] in ("GB-", "US-") and p.account_ref.endswith("-001") for p in profiles
    )
    assert {p.processor_source for p in profiles} >= {"shopify", "stripe", "square", "sumup"}
    # No demo borrower's supplier is named like another demo borrower.
    words = {s.name.upper().split()[0] for s in SPECS}
    assert not words & {w for p in profiles for n in p.supplier_names for w in n.split()}
    by_name = {s.name: s for s in SPECS}
    story = {name: connection_story(s) for name, s in by_name.items()}
    assert story == {
        "Harbour & Pine Homeware Ltd": frozenset(),
        "Kestrel Gift Co Ltd": frozenset(),
        "Marlow Lane Interiors Ltd": frozenset(),
        "Brightwell Crafts Ltd": {"stale_connection"},
        "Oakden Trading Ltd": {"revoked_mid_review"},
        "Juniper & Rye Bakery LLC": frozenset(),
        "Northgate Phone Repair LLC": frozenset(),
    }
    for s in SPECS:
        sources = {c.source for c in connections_of(s)}
        assert s.profile.processor_source in sources and "plaid" in sources
        assert "amazon" not in sources  # Oakden's partial_coverage story; nobody else sells there
    # Connection stories sit on borrowers staged for review.
    assert all(by_name[n].stage == "review" for n, st in story.items() if st)
    # A different (fictional) lender for the undisclosed-debit story.
    assert by_name["Marlow Lane Interiors Ltd"].profile.lender_name != "YRDEAL CAPITAL"
    # Names the web-lookup fixtures rely on (CCJ and charge from the lender; the director).
    assert by_name["Marlow Lane Interiors Ltd"].profile.lender_name == "FERNBANK FUNDING"
    assert by_name["Brightwell Crafts Ltd"].profile.owner_account == "J BRIGHTWELL PERSONAL"
