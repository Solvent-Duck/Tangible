from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tangible.checks import completeness as c
from tangible.checks.registry import FindingDraft
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import BankTxn, Connection, Ledger, Order, Payout

AS_OF = datetime(2025, 6, 30, 12, tzinfo=UTC)  # a Monday
TODAY = AS_OF.date()
GRANTED = datetime(2025, 1, 1, tzinfo=UTC)


def at(d: date, hour: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def bank(
    ref: str,
    day: date,
    amount: str = "10.00",
    direction: str = "in",
    cp: str | None = "CUSTOMER",
    observed: datetime | None = None,
) -> BankTxn:
    return BankTxn(
        source_ref=ref,
        account_ref="A",
        amount=Decimal(amount),
        direction=direction,  # type: ignore[arg-type]
        counterparty=cp,
        description=None,
        posted_on=day,
        observed_at=observed or at(day, 6),
        evidence=EvidenceRef("pull", ref),
    )


def conn(
    source: str = "shopify",
    last_success: datetime | None = AS_OF,
    revoked: datetime | None = None,
    granted: datetime = GRANTED,
) -> Connection:
    return Connection(
        source_ref=f"c-{source}",
        source=source,
        granted_at=granted,
        revoked_at=revoked,
        last_success_at=last_success,
        observed_at=granted,
        evidence=EvidenceRef("pull", f"c-{source}"),
    )


def weekdays(n_days: int) -> list[date]:
    days = (TODAY - timedelta(days=i) for i in range(1, n_days))
    return [d for d in days if d.weekday() < 5]


def weekday_bank(n_days: int, skip: set[date] | None = None) -> Ledger:
    skip = skip or set()
    return Ledger(bank_txns=tuple(bank(f"b{d}", d) for d in weekdays(n_days) if d not in skip))


def gap_days(led: Ledger) -> set[str]:
    return {d for f in c.time_gaps(led, AS_OF) for d in f.facts["gap_days"].split(",")}


# ---------------------------------------------------------------- time_gaps

GAP = TODAY - timedelta(days=10)  # a Friday inside the window


def test_time_gaps_fires_on_missing_weekday() -> None:
    assert GAP.weekday() == 4
    out = c.time_gaps(weekday_bank(140, skip={GAP}), AS_OF)
    assert len(out) == 1 and out[0].severity == "review"
    assert out[0].facts["gap_days"] == GAP.isoformat()
    assert out[0].facts["table"] == "bank_txns"
    assert out[0].evidence_refs  # the row just before the gap


def test_time_gaps_rate_boundary() -> None:
    # 8 of 8 earlier Fridays active -> fires; 7 of 8 (87.5% < 95%) -> does not
    prior_miss = GAP - timedelta(weeks=3)
    assert gap_days(weekday_bank(140, skip={GAP})) == {GAP.isoformat()}
    assert GAP.isoformat() not in gap_days(weekday_bank(140, skip={GAP, prior_miss}))


def test_time_gaps_weekends_trailing_silence_old_gaps_and_empty() -> None:
    assert c.time_gaps(weekday_bank(140), AS_OF) == []
    trailing = {TODAY - timedelta(days=i) for i in range(1, 5)}
    assert c.time_gaps(weekday_bank(140, skip=trailing), AS_OF) == []
    old = TODAY - timedelta(days=100)
    old -= timedelta(days=old.weekday() - 2)  # a Wednesday outside the 90-day window
    assert c.time_gaps(weekday_bank(160, skip={old}), AS_OF) == []
    assert c.time_gaps(Ledger(), AS_OF) == []


def test_time_gaps_orders_and_payouts() -> None:
    days = [d for d in weekdays(140) if d != GAP]
    orders = tuple(Order(f"o{d}", Decimal(5), Decimal(0), at(d, 10), at(d, 11)) for d in days)
    payouts = tuple(
        Payout(f"p{d}", "SHOPIFY", Decimal(5), Decimal(0), d, d, at(d, 11)) for d in days
    )
    out = c.time_gaps(Ledger(orders=orders, payouts=payouts), AS_OF)
    assert {f.facts["table"] for f in out} == {"orders", "payouts"}


# ---------------------------------------------------------------- stale_connection


def test_stale_connection_boundary() -> None:
    edge = Ledger(connections=(conn(last_success=AS_OF - timedelta(days=7)),))
    assert c.stale_connection(edge, AS_OF) == []
    over = Ledger(connections=(conn(last_success=AS_OF - timedelta(days=7, seconds=1)),))
    out = c.stale_connection(over, AS_OF)
    assert len(out) == 1 and out[0].severity == "review"
    assert out[0].facts["age_days"] == "7"


def test_stale_connection_revoked_none_and_never_succeeded() -> None:
    assert c.stale_connection(Ledger(), AS_OF) == []
    old = AS_OF - timedelta(days=30)
    revoked = Ledger(connections=(conn(last_success=old, revoked=old + timedelta(days=1)),))
    assert c.stale_connection(revoked, AS_OF) == []
    never = Ledger(connections=(conn(last_success=None, granted=old),))
    assert len(c.stale_connection(never, AS_OF)) == 1


# ---------------------------------------------------------------- partial_coverage


def coverage_ledger(amazon: str, connections: tuple[Connection, ...] = ()) -> Ledger:
    d = TODAY - timedelta(days=5)
    return Ledger(
        bank_txns=(
            bank("b1", d, "9900.00", cp="SHOPIFY PAYMENTS"),
            bank("b2", d, amazon, cp="amazon payments"),
        ),
        connections=connections,
        payouts=(Payout("p1", "SHOPIFY PAYMENTS", Decimal(1), Decimal(0), d, d, at(d)),),
    )


def test_partial_coverage_boundary_with_payout_fallback() -> None:
    assert c.partial_coverage(coverage_ledger("100.00"), AS_OF) == []  # 100 of 10000: 1.00%
    out = c.partial_coverage(coverage_ledger("100.01"), AS_OF)
    assert len(out) == 1 and out[0].severity == "hold"
    assert out[0].facts["unconnected_sources"] == "AMAZON"
    assert out[0].facts["connected_sources"] == "SHOPIFY"


def test_partial_coverage_connections_override_payouts() -> None:
    only_amazon = coverage_ledger("500.00", connections=(conn("amazon"),))
    out = c.partial_coverage(only_amazon, AS_OF)
    assert out and out[0].facts["unconnected_sources"] == "SHOPIFY"
    both = coverage_ledger("500.00", connections=(conn("amazon"), conn("shopify")))
    assert c.partial_coverage(both, AS_OF) == []


def test_partial_coverage_ignores_old_inflows() -> None:
    old = TODAY - timedelta(days=91)
    led = Ledger(bank_txns=(bank("b", old, "500.00", cp="AMAZON PAYMENTS"),))
    assert c.partial_coverage(led, AS_OF) == []


# ---------------------------------------------------------------- short_history


def test_short_history_boundary() -> None:
    def led(days: int) -> Ledger:
        return Ledger(bank_txns=(bank("b", TODAY - timedelta(days=days)),))

    assert c.short_history(led(183), AS_OF) == []
    out = c.short_history(led(182), AS_OF)
    assert len(out) == 1 and out[0].severity == "info"
    assert out[0].facts["history_days"] == "182" and out[0].facts["table"] == "bank_txns"


def test_short_history_one_finding_per_short_source() -> None:
    d = TODAY - timedelta(days=10)
    led = Ledger(
        bank_txns=(bank("b", d),),
        orders=(Order("o", Decimal(1), Decimal(0), at(d), at(d)),),
    )
    assert {f.facts["table"] for f in c.short_history(led, AS_OF)} == {"bank_txns", "orders"}


# ---------------------------------------------------------------- revoked_mid_review


def test_revoked_mid_review_boundary() -> None:
    def run(revoked: datetime) -> list[FindingDraft]:
        return c.revoked_mid_review(Ledger(connections=(conn(revoked=revoked),)), AS_OF)

    out = run(AS_OF - timedelta(days=30))
    assert len(out) == 1 and out[0].severity == "hold"
    assert run(AS_OF - timedelta(days=30, seconds=1)) == []
    assert run(AS_OF) != []
    assert run(AS_OF + timedelta(seconds=1)) == []  # not revoked yet at as_of
    assert c.revoked_mid_review(Ledger(connections=(conn(),)), AS_OF) == []


# ---------------------------------------------------------------- shared rules


def test_time_rule_hides_rows_observed_after_as_of() -> None:
    d = TODAY - timedelta(days=5)
    later = AS_OF + timedelta(hours=1)
    stale = conn("shopify", last_success=AS_OF - timedelta(days=30))
    visible = Ledger(
        bank_txns=(bank("b1", d, "9000.00", cp="SHOPIFY"), bank("b2", d, "9000.00", cp="AMAZON")),
        connections=(stale,),
    )
    hidden_args: dict[str, Any] = {"observed_at": later}
    hidden = Ledger(
        bank_txns=tuple(BankTxn(**{**x.__dict__, **hidden_args}) for x in visible.bank_txns),
        connections=(Connection(**{**stale.__dict__, **hidden_args}),),
    )
    assert c.partial_coverage(visible, AS_OF) and c.stale_connection(visible, AS_OF)
    for fn in (c.partial_coverage, c.stale_connection, c.short_history, c.time_gaps):
        assert fn(hidden, AS_OF) == []


def test_deterministic_and_evidence_capped() -> None:
    d = TODAY - timedelta(days=5)
    txns = tuple(bank(f"b{i}", d, "100.00", cp="AMAZON") for i in range(80))
    a = c.partial_coverage(Ledger(bank_txns=txns), AS_OF)
    b = c.partial_coverage(Ledger(bank_txns=txns[::-1]), AS_OF)
    assert a == b and len(a[0].evidence_refs) == c.MAX_EVIDENCE


# ---------------------------------------------------------------- synthetic plants


def run_all(led: Ledger) -> dict[str, FindingDraft]:
    last = max(t.posted_on for t in led.bank_txns)
    as_of = at(last + timedelta(days=1), 12)
    fns = (c.time_gaps, c.stale_connection, c.partial_coverage, c.short_history)
    return {f.check_key: f for fn in fns for f in fn(led, as_of)}


@pytest.mark.parametrize("plant", [(), ("hidden_reserve",), ("phantom_payouts",)])
def test_synthetic_no_hold(synthetic_ledger: Any, plant: tuple[str, ...]) -> None:
    found = run_all(synthetic_ledger(plant=plant, days=200))
    assert not [f for f in found.values() if f.severity == "hold"]


def test_synthetic_partial_coverage_plant(synthetic_ledger: Any) -> None:
    f = run_all(synthetic_ledger(plant=("partial_coverage",), days=200))["partial_coverage"]
    assert f.severity == "hold" and f.facts["unconnected_sources"] == "AMAZON"
