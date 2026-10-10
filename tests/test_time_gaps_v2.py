from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

import tangible.checks  # noqa: F401  (registers checks)
from tangible.checks.calendar_uk import holidays, is_business_day
from tangible.checks.completeness import time_gaps_v2
from tangible.checks.registry import REGISTRY
from tangible.checks.sets import check_set
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import BankTxn, Ledger

AS_OF = datetime(2025, 7, 1, 12, tzinfo=UTC)  # Tuesday
LAST = AS_OF.date() - timedelta(days=1)


def bank(per_day: int, skip: set[date] = frozenset(), days: int = 160) -> Ledger:  # type: ignore[assignment]
    """`per_day` rows on every business day for `days` days before AS_OF, except `skip`."""
    rows: list[BankTxn] = []
    for i in range(days):
        d = LAST - timedelta(days=i)
        if not is_business_day(d) or d in skip:
            continue
        for n in range(per_day):
            seen = datetime.combine(d + timedelta(days=1), datetime.min.time(), UTC)
            rows.append(
                BankTxn(
                    f"bt-{d}-{n}",
                    "ACC",
                    Decimal(10),
                    "in",
                    "X",
                    "d",
                    d,
                    seen,
                    EvidenceRef("11111111-1111-1111-1111-111111111111", f"row={i}-{n}"),
                )  # fmt: skip
            )
    return Ledger(bank_txns=tuple(rows))


def gaps(ledger: Ledger) -> list[tuple[str, str]]:
    return [(f.facts["table"], f.severity) for f in time_gaps_v2(ledger, AS_OF)]


# A Wednesday and the Thursday after it, well inside the window and not holidays.
WED = date(2025, 6, 11)
THU = date(2025, 6, 12)


def test_registered_and_in_cs3() -> None:
    assert ("time_gaps", 2) in REGISTRY
    assert ("time_gaps", 2) in check_set("cs-3") and ("time_gaps", 1) not in check_set("cs-3")
    assert ("time_gaps", 1) in check_set("cs-2")  # released sets never change


def test_uk_bank_holidays() -> None:
    assert holidays(2011) >= {
        date(2011, 1, 3), date(2011, 4, 22), date(2011, 4, 25), date(2011, 4, 29),
        date(2011, 5, 2), date(2011, 5, 30), date(2011, 8, 29), date(2011, 12, 26),
        date(2011, 12, 27),
    }  # fmt: skip
    assert date(2012, 5, 28) not in holidays(2012) and date(2012, 6, 4) in holidays(2012)
    assert date(2020, 5, 8) in holidays(2020) and date(2020, 5, 4) not in holidays(2020)
    assert date(2021, 12, 27) in holidays(2021) and date(2021, 12, 28) in holidays(2021)
    assert not is_business_day(date(2025, 8, 25)) and is_business_day(date(2025, 8, 26))


def test_clean_daily_feed_is_quiet() -> None:
    assert gaps(bank(1)) == []


def test_bank_holiday_is_never_a_gap() -> None:
    # 26 May 2025 is the spring bank holiday: no rows that day is normal.
    assert date(2025, 5, 26) in holidays(2025)
    assert gaps(bank(3)) == []


def test_single_quiet_day_on_low_volume_feed_is_info() -> None:
    assert gaps(bank(1, skip={WED})) == [("bank_txns", "info")]


def test_two_quiet_days_on_busy_feed_are_review() -> None:
    # 4 rows/day: P(no rows on two days by luck) = exp(-8) < 0.1%.
    assert gaps(bank(4, skip={WED, THU})) == [("bank_txns", "review")]


def test_two_scattered_days_on_moderate_feed_stay_info() -> None:
    # Not consecutive, so two separate episodes each with exp(-4) ~ 1.8%.
    assert gaps(bank(4, skip={WED, date(2025, 6, 25)})) == [("bank_txns", "info")]


def test_one_empty_day_on_very_busy_feed_is_review() -> None:
    (f,) = time_gaps_v2(bank(8, skip={WED}), AS_OF)
    assert f.severity == "review" and f.facts["worst_episode"] == WED.isoformat()
    assert Decimal(f.facts["worst_chance"]) < Decimal("0.001")
    assert f.evidence_refs


def test_three_day_outage_on_low_volume_feed_is_review() -> None:
    outage = {WED, THU, date(2025, 6, 13)}
    (f,) = time_gaps_v2(bank(1, skip=outage), AS_OF)
    assert f.severity == "review" and f.facts["worst_episode"].count(",") == 2


def test_weekend_does_not_break_an_episode() -> None:
    # Fri + Mon + Tue are consecutive business days.
    outage = {date(2025, 6, 13), date(2025, 6, 16), date(2025, 6, 17)}
    (f,) = time_gaps_v2(bank(1, skip=outage), AS_OF)
    assert f.severity == "review" and f.facts["episodes"] == "1"


def test_trailing_silence_is_staleness_not_a_gap() -> None:
    tail = {LAST - timedelta(days=i) for i in range(10)}
    assert gaps(bank(4, skip=tail)) == []


def test_rows_observed_after_as_of_are_invisible() -> None:
    led = bank(4, skip={WED, THU})
    late = BankTxn("late", "ACC", Decimal(1), "in", "X", "d", WED, AS_OF + timedelta(days=1))
    led2 = Ledger(bank_txns=(*led.bank_txns, late))
    assert gaps(led2) == [("bank_txns", "review")]


@pytest.mark.parametrize("plant", [(), ("hidden_reserve",)])
def test_synthetic_clean_feed_has_no_review(synthetic_ledger, plant) -> None:  # type: ignore[no-untyped-def]
    ledger = synthetic_ledger(plant=set(plant))
    last = max(t.posted_on for t in ledger.bank_txns)
    as_of = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC) + timedelta(
        hours=12
    )
    assert [f.severity for f in time_gaps_v2(ledger, as_of) if f.severity != "info"] == []
