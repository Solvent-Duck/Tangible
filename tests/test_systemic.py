"""Systemic exposure v1 (docs/systemic-risk-plan.md): macro signals, ms-4, cs-6, policy 0.5.0."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import psycopg
import pytest

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.checks.registry import run_check
from tangible.db.macro import load_macro_signals
from tangible.db.repo import load_ledger
from tangible.db.signals import insert_application
from tangible.ledger.types import (
    AccountBalance,
    Application,
    BankTxn,
    ImportLine,
    InventorySnapshot,
    Invoice,
    Ledger,
    MacroSignal,
    Order,
)
from tangible.macro import DEMO_TARIFFS, MacroError, load_tariff_actions, parse_tariff_action
from tangible.metrics import compute
from tangible.metrics.sets import metric_set
from tangible.policy import evaluate, load_policy

POLICIES = Path(__file__).parent.parent / "policies"
POLICY = load_policy(POLICIES / "default.yaml")
AS_OF = datetime(2026, 10, 1, 23, 59, tzinfo=UTC)
SEEN = AS_OF - timedelta(days=100)


def day(n: int) -> date:
    return AS_OF.date() - timedelta(days=n)


def action(
    ref: str,
    *,
    published: int,
    effective: int,
    chapters: tuple[str, ...] = ("94",),
    countries: tuple[str, ...] = ("CN",),
    duty: str = "0.25",
) -> MacroSignal:
    """published/effective are days relative to AS_OF (negative = before)."""
    payload = {"document_number": ref, "hs_chapters": list(chapters),
               "countries": list(countries), "additional_duty": duty}  # fmt: skip
    return MacroSignal(
        "federal_register", ref, "tariff_action", json.dumps(payload, sort_keys=True),
        observed_at=AS_OF + timedelta(days=published),
        occurred_at=AS_OF + timedelta(days=effective),
    )  # fmt: skip


def business(
    mix: tuple[ImportLine, ...] | None,
    macro: tuple[MacroSignal, ...] = (),
    *,
    purchases: str = "15000",
    balance: str = "20000",
    net_flow_90d: str = "-6000",
) -> Ledger:
    """Revenue 30,000 in the window; opening and closing stock 10,000; COGS = purchases (so margin
    0.5 by default); closing stock / (COGS / 90) = 60 days of inventory (2 months of buffer)."""
    orders = tuple(
        Order(f"o{i}", D(1000), D(0), AS_OF - timedelta(days=2 + 2 * i), SEEN) for i in range(30)
    )
    snaps = (
        InventorySnapshot("s-open", "sku1", D(100), D(100), day(95), SEEN),
        InventorySnapshot("s-close", "sku1", D(100), D(100), day(0), SEEN),
    )
    inv = (Invoice("i1", "payable", "SUPPLIER", D(purchases), day(40), day(10), None, SEEN),)
    flow = D(net_flow_90d)
    txns = (
        BankTxn("b1", "acc", abs(flow), "out" if flow < 0 else "in", None, None, day(30), SEEN),
    )
    bal = (AccountBalance("ab1", "acc", D(balance), day(0), SEEN),)
    app = Application("app", None, (), (), SEEN, import_mix=mix)
    return Ledger(
        orders=orders, inventory_snapshots=snaps, invoices=inv, bank_txns=txns,
        account_balances=bal, applications=(app,), macro_signals=macro,
    )  # fmt: skip


CN_FURNITURE = (ImportLine("94", "CN", D("0.6")),)
NEW = action("DEMO-NEW", published=-7, effective=30)


def m(ledger: Ledger, key: str) -> D | None:
    return compute(key, 1, ledger, AS_OF)


# ---------------------------------------------------------------- metrics


def test_ms4_is_ms3_plus_the_systemic_metrics() -> None:
    assert metric_set("ms-4")[: len(metric_set("ms-3"))] == metric_set("ms-3")
    assert [k for k, _ in metric_set("ms-4")[len(metric_set("ms-3")) :]] == [
        "import_share", "tariff_rate_change", "tariff_margin_shock", "stock_buffer_months",
        "stressed_margin", "stressed_flow_margin", "stressed_runway",
    ]  # fmt: skip


def test_stress_metrics_by_hand() -> None:
    led = business(CN_FURNITURE, (NEW,))
    assert m(led, "gross_margin_est") == D("0.5")
    assert m(led, "import_share") == D("0.6")
    assert m(led, "tariff_rate_change") == D("0.25")
    assert m(led, "tariff_margin_shock") == D("0.075")  # 0.6 x 0.25 x (1 - 0.5)
    assert m(led, "stressed_margin") == D("0.425")
    assert m(led, "stock_buffer_months") == D(2)  # 60 days of inventory
    # (-2,000 flow - 0.075 x 10,000 revenue a month) / 10,000
    assert m(led, "stressed_flow_margin") == D("-0.275")
    # Cash 20,000; flow -2,000 a month; after the 2-month buffer, -750 more (0.075 x 10,000).
    # Months 1-2: 20,000 -> 16,000; then 16,000 / 2,750 = 5.818... months more.
    runway = m(led, "stressed_runway")
    assert runway is not None and abs(runway - (2 + D(16000) / D(2750))) < D("0.0001")


def test_only_actions_known_and_in_the_window_count() -> None:
    old = action("OLD", published=-400, effective=-380)  # long in effect: already in the margin
    later = action("LATER", published=2, effective=30)  # published after as_of: not known
    far = action("FAR", published=-7, effective=365)  # too far out to size a loan on
    other = action("OTHER", published=-7, effective=30, countries=("VN",))
    led = business(CN_FURNITURE, (old, later, far, other))
    assert m(led, "tariff_rate_change") == D(0)
    stacked = business(CN_FURNITURE, (NEW, action("NEW2", published=-3, effective=10, duty="0.1")))
    assert m(stacked, "tariff_rate_change") == D("0.35")  # actions on the same goods stack


def test_no_stated_mix_is_not_evaluated_and_no_imports_is_zero() -> None:
    unstated = business(None, (NEW,))
    for key in ("import_share", "tariff_rate_change", "tariff_margin_shock", "stressed_runway"):
        assert m(unstated, key) is None, key
    none = business((), (NEW,))
    assert m(none, "import_share") == D(0) and m(none, "tariff_margin_shock") == D(0)
    assert m(none, "stressed_runway") == D("10")  # 20,000 / 2,000: the unstressed runway


def test_a_cash_generating_business_lasts_the_horizon() -> None:
    led = business(CN_FURNITURE, (NEW,), net_flow_90d="9000")
    assert m(led, "stressed_runway") == D(24)


# ---------------------------------------------------------------- checks


def test_tariff_exposure_fires_when_the_stressed_margin_is_thin() -> None:
    thin = business((ImportLine("94", "CN", D("0.9")),), (NEW,), purchases="25000")  # margin 1/6
    (f,) = run_check("tariff_exposure", 1, thin, AS_OF)
    assert f.severity == "review" and "DEMO-NEW" in f.summary
    assert f.facts["covered_share_of_cogs"] == "0.90"
    assert run_check("tariff_exposure", 1, business(CN_FURNITURE, (NEW,)), AS_OF) == []
    assert run_check("tariff_exposure", 1, business(None, (NEW,)), AS_OF) == []


def test_import_share_unstated_is_info() -> None:
    (f,) = run_check("import_share_unstated", 1, business(None), AS_OF)
    assert f.severity == "info"
    assert run_check("import_share_unstated", 1, business(()), AS_OF) == []


# ---------------------------------------------------------------- policy 0.5.0


def _base() -> dict[str, D | None]:
    return {
        "refund_rate": D("0.01"), "gross_margin_est": D("0.4"), "payables_discipline": D("0.95"),
        "net_revenue": D(100000), "history_days": D(400),
    }  # fmt: skip


@pytest.mark.parametrize(
    ("runway", "limit", "outcome"),
    [(D(2), D("20000.00"), "reduced"), (D("4.5"), D("30000.00"), "reduced"),
     (D(12), D("40000.00"), "unchanged"), (None, D("40000.00"), "not_evaluated")],
)  # fmt: skip
def test_stressed_limit_only_reduces_and_never_touches_the_band(
    runway: D | None, limit: D, outcome: str
) -> None:
    r = evaluate(POLICY, _base() | {"stressed_runway": runway}, [])
    assert r.band == "A" and r.limit == limit  # A: 0.40 x 100,000 = 40,000 before the stress
    stress = [x for x in r.rules_fired if x["step"] == "stress"]
    assert outcome in {x["outcome"] for x in stress}
    if outcome == "reduced":
        assert stress[-1]["rule"].startswith("stressed limit $40,000.00→")


def test_060_writes_rules_in_dollars_and_050_frozen_rules_are_unchanged() -> None:
    # Same decision under both; only 0.6.0's wording differs (report block). Assessments frozen
    # under 0.5.0 must replay to byte-identical rules.
    m = _base() | {"stressed_runway": D(2)}
    new = evaluate(POLICY, m, [])
    old = evaluate(load_policy(POLICIES / "default-0.5.0.yaml"), m, [])
    assert (new.band, new.limit) == (old.band, old.limit) == ("A", D("20000.00"))
    rule = {r.policy_version: {x["step"]: x for x in r.rules_fired} for r in (new, old)}
    assert rule["0.5.0"]["stress"]["rule"] == (
        "stressed limit 40000.00→20000.00 (x0.5): stressed_runway 2 <= 3"
    )
    assert rule["0.6.0"]["stress"]["rule"] == (
        "stressed limit $40,000.00→$20,000.00 (x0.50): stressed_runway 2 <= 3"
    )
    assert rule["0.5.0"]["limit"]["rule"] == "limit: 0.4 x net_revenue, max 250000"
    assert rule["0.6.0"]["limit"]["rule"] == "limit: 0.40 x net_revenue, max $250,000.00"
    assert rule["0.6.0"]["limit"]["saw"] == "$100,000.00"


def test_stress_schema_is_strict() -> None:
    from tangible.policy.schema import parse_policy

    raw = POLICY.spec | {"stress": {"floor": 0.5, "rules": [
        {"when": {"stressed_runway": {"max": 3}}, "limit_factor": 1.2}]}}  # fmt: skip
    with pytest.raises(ValueError, match="only ever reduces"):
        parse_policy(raw)
    raw["stress"]["rules"][0]["limit_factor"] = 0.3  # below the floor
    with pytest.raises(ValueError, match="only ever reduces"):
        parse_policy(raw)


def test_040_still_evaluates_without_a_stress_step() -> None:
    old = load_policy(POLICIES / "default-0.4.0.yaml")
    r = evaluate(old, _base() | {"stressed_runway": D(1)}, [])
    assert r.limit == D("40000.00") and all(x["step"] != "stress" for x in r.rules_fired)


# ---------------------------------------------------------------- loader and database


def test_demo_actions_parse_and_bad_ones_are_rejected() -> None:
    raw = json.loads(DEMO_TARIFFS.read_text())
    assert "FICTIONAL" in raw["note"]
    for a in raw["actions"]:
        p = parse_tariff_action(a)
        assert p["title"].startswith("[Demo fixture, fictional]")
    good = raw["actions"][0]
    for bad in ({"additional_duty": "-1"}, {"hs_chapters": ["9401"]}, {"countries": ["China"]}):
        with pytest.raises(MacroError):
            parse_tariff_action(good | bad)


@pytest.mark.db
def test_macro_rows_are_global_read_only_for_tenants_and_dated_by_publication(
    test_db_url: str,
) -> None:
    with psycopg.connect(test_db_url, autocommit=True) as admin:
        load_tariff_actions(admin)  # the session's database may already hold them (demo seed)
        assert load_tariff_actions(admin) == 0  # idempotent: same content, nothing new
        t = admin.execute("INSERT INTO tenant (name) VALUES ('t') RETURNING id").fetchone()
        b = admin.execute(
            "INSERT INTO borrower (tenant_id, name) VALUES (%s, 'b') RETURNING id",
            (t[0],),  # type: ignore[index]
        ).fetchone()
        assert t and b
        admin.execute("SET ROLE tangible_app")
        admin.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t[0]),))
        rows = load_macro_signals(admin)  # tenants read the global rows
        assert {r.source_ref for r in rows} >= {"DEMO-2025-0007", "DEMO-2026-0001"}
        new = next(r for r in rows if r.source_ref == "DEMO-2026-0001")
        assert new.observed_at.date() == date(2026, 9, 24)  # published, not effective
        assert new.occurred_at is not None and new.occurred_at.date() == date(2026, 11, 1)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute(
                "INSERT INTO macro_signal (source, source_ref, kind, payload, sha256, observed_at) "
                "VALUES ('x', 'y', 'tariff_action', '{}', 'z', now())"
            )
        # The application's import mix round-trips, and every ledger carries the macro rows.
        mix = (ImportLine("94", "CN", D("0.6")), ImportLine("82", "VN", D("0.1")))
        insert_application(admin, str(t[0]), str(b[0]), Application(
            "app", None, (), (), SEEN, import_mix=mix))  # fmt: skip
        led: Any = load_ledger(admin, str(b[0]))
        assert led.application.import_mix == mix and len(led.macro_signals) == len(rows)
        admin.execute("RESET ROLE")
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute("UPDATE macro_signal SET url = 'x'")


def test_a_tariff_that_turns_cash_flow_negative_cuts_the_limit_by_a_quarter() -> None:
    base = _base() | {"stressed_runway": D(24)}
    hit = evaluate(
        POLICY, base | {"stressed_flow_margin": D("-0.02"), "tariff_margin_shock": D("0.1")}, []
    )
    assert hit.band == "A" and hit.limit == D("30000.00")
    assert "stressed_flow_margin -0.02 <= 0" in hit.rules_fired[-1]["rule"]
    # Already burning cash, but not because of a tariff: the stress does not cut.
    own = evaluate(
        POLICY, base | {"stressed_flow_margin": D("-0.02"), "tariff_margin_shock": D(0)}, []
    )
    assert own.limit == D("40000.00")
