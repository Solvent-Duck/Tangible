"""Number verifier: every numeric claim in agent text must appear in the cited tool results.

Pure, no I/O. The agent never invents a number; this is the check that enforces it.

Not treated as claims (masked before extraction), because they are labels, not results:
- UUIDs, tool-call ids, ISO and slash dates, clock times, list markers.
- Window and period phrases: a number directly followed by a time unit, with an optional
  hyphen or space ("90 days", "30-day", "13-week", "prior 25 weeks", "12 months", "trailing 90d",
  "5 business days", "2 quarters", "1 yr"), and statistical widths ("2 sigma", "2σ",
  "2 standard deviations").
- Digits inside snake_case identifier tokens ("change_30v30", "slope_13w", "revenue_yoy_90d").
- Reference ids with a letter prefix ("INV-574941", "PO-1182").
Money and counts followed by other words ("21 debits", "305 SKUs") are still claims. A bare whole
number is a count and must match exactly; a currency-marked whole amount ("GBP 136,832") may be
the cited value rounded to the unit.

Signs are ignored when matching: a claim of 16.5% matches a cited -0.165 (and vice versa).
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    unmatched: tuple[str, ...]
    checked: int


# Spans blanked out before claim extraction.
_MASKS = (
    re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"\b(?:toolu|call|tool|tc)[_-][\w-]+", re.IGNORECASE),
    re.compile(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    ),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    re.compile(r"(?m)^[ \t]*\d+[.)](?=\s)"),  # list markers
    re.compile(r"\b[A-Za-z]{2,}-\d+\b"),  # reference ids: INV-574941, PO-1182
)

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_TIME_UNIT = (
    r"(?:business[ \t]+days?|days?|weeks?|wks?|months?|mo|years?|yrs?|quarters?"
    r"|sigma|σ|standard[ \t]+deviations?)"
)
_NO_PREFIX = r"(?<![\w.£$€])"

# Window / period / sigma phrases and identifier tokens that carry digits.
_LABEL_MASKS = (
    re.compile(
        rf"{_NO_PREFIX}(?:(?:{_NUM})[ \t]*[-–][ \t]*)?(?:{_NUM})[ \t-]?{_TIME_UNIT}(?!\w)",
        re.IGNORECASE,
    ),  # "90 days", "30-day", ranges "31-60 days"
    re.compile(rf"{_NO_PREFIX}(?:{_NUM})[dw](?!\w)", re.IGNORECASE),  # 90d, 13w
    re.compile(r"\b(?=[A-Za-z0-9_]*\d)[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),
)

_CLAIM = re.compile(
    rf"(?<![\w.])(?P<cur>(?:[£$€]|GBP\s?|USD\s?|EUR\s?))?(?P<num>{_NUM})"
    r"(?P<suf>%|[kKmM](?![\w]))?(?![\w])"
)


@dataclass(frozen=True)
class _Claim:
    raw: str
    value: Decimal
    places: int
    pct: bool
    money: bool  # currency-marked: a whole amount may be the cited value rounded to the unit
    scale: Decimal  # 1, 1000 or 1_000_000


def _mask(text: str) -> str:
    for pat in (*_MASKS, *_LABEL_MASKS):
        text = pat.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _claims(text: str) -> Iterator[_Claim]:
    for m in _CLAIM.finditer(_mask(text)):
        num, suf, cur = m["num"], m["suf"], m["cur"]
        if not suf and not cur and "," not in num and "." not in num:
            if len(num) == 4 and 1900 <= int(num) <= 2100:
                continue  # a standalone year
        digits = num.replace(",", "")
        places = len(digits.split(".")[1]) if "." in digits else 0
        scale = {"k": Decimal(1000), "m": Decimal(1_000_000)}.get((suf or "").lower(), Decimal(1))
        yield _Claim(m.group(0).strip(), Decimal(digits), places, suf == "%", bool(cur), scale)


def _to_decimal(s: str) -> Decimal | None:
    t = s.strip().replace(",", "").lstrip("£$€")
    t = t.removesuffix("%").strip()
    if not t:
        return None
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _numbers(obj: Any) -> Iterator[Decimal]:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, int):
        yield Decimal(obj)
    elif isinstance(obj, float):
        d = Decimal(str(obj))
        if d.is_finite():
            yield d
    elif isinstance(obj, Decimal):
        if obj.is_finite():
            yield obj
    elif isinstance(obj, str):
        parsed = _to_decimal(obj)
        if parsed is not None:
            yield parsed
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers(v)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from _numbers(v)
    else:
        for attr in ("__dict__",):
            inner = getattr(obj, attr, None)
            if isinstance(inner, dict):
                yield from _numbers(inner)


def _round(v: Decimal, places: int) -> Decimal:
    return v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _matches(c: _Claim, x: Decimal) -> bool:
    x = abs(x)  # sign-insensitive: "declining 16.5%" matches a cited -0.165
    if c.pct:
        return _round(x * 100, c.places) == c.value or _round(x, c.places) == c.value
    if c.scale != 1:
        return _round(x / c.scale, c.places) == c.value
    if c.places == 0 and not c.money:
        return x == c.value  # counts must match exactly
    return _round(x, c.places) == c.value


def verify_numbers(text: str, cited_results: Sequence[object]) -> VerifyResult:
    pool = [n for r in cited_results for n in _numbers(r)]
    claims = list(_claims(text))
    # Claims can quote a full-precision Decimal (28+ places); rounding a large pool value to that
    # many places overflows the default 28-digit context and raises InvalidOperation.
    with localcontext() as ctx:
        ctx.prec = 200
        unmatched = tuple(c.raw for c in claims if not any(_matches(c, x) for x in pool))
    return VerifyResult(ok=not unmatched, unmatched=unmatched, checked=len(claims))
