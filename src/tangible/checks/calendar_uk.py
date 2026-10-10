"""Bank holidays in England and Wales, computed from the statutory rules plus known one-offs.

Used by time_gaps v2 so a holiday with no trading is not read as missing data. Scotland and
Northern Ireland differ; per-borrower calendars are a later refinement. One-off moves and extra
days are from gov.uk/bank-holidays; add new ones here as they are announced.
"""

from datetime import date, timedelta
from functools import cache
from typing import Final

# Extra one-off holidays, and regular holidays that were moved (old date -> new date).
EXTRA: Final[frozenset[date]] = frozenset(
    {
        date(2011, 4, 29),  # royal wedding
        date(2012, 6, 5),  # diamond jubilee
        date(2022, 6, 3),  # platinum jubilee
        date(2022, 9, 19),  # state funeral
        date(2023, 5, 8),  # coronation
    }
)
MOVED: Final[dict[date, date]] = {
    date(2012, 5, 28): date(2012, 6, 4),  # spring bank holiday
    date(2020, 5, 4): date(2020, 5, 8),  # early May bank holiday (VE day)
    date(2022, 5, 30): date(2022, 6, 2),  # spring bank holiday
}


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def _last_monday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=d.weekday())


@cache
def holidays(year: int) -> frozenset[date]:
    easter = _easter(year)
    new_year = date(year, 1, 1)
    if new_year.weekday() >= 5:
        new_year += timedelta(days=7 - new_year.weekday())
    xmas, boxing = date(year, 12, 25), date(year, 12, 26)
    if xmas.weekday() == 5:  # Saturday: Mon 27 and Tue 28
        xmas, boxing = date(year, 12, 27), date(year, 12, 28)
    elif xmas.weekday() == 6:  # Sunday: Boxing Day Mon 26, Christmas Tue 27
        xmas = date(year, 12, 27)
    elif boxing.weekday() == 5:  # Boxing Day Saturday: Mon 28
        boxing = date(year, 12, 28)
    regular = {
        new_year,
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        _first_monday(year, 5),
        _last_monday(year, 5),
        _last_monday(year, 8),
        xmas,
        boxing,
    }
    days = {MOVED.get(d, d) for d in regular}
    return frozenset(days | {d for d in EXTRA if d.year == year})


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)
