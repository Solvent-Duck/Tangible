"""Typed policy file. Strict: unknown keys, wrong types and bad band order raise ValueError."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

BAND_ORDER = ("A", "B", "C", "D")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_STATES = ("open", "accepted", "rejected", "disputed")


@dataclass(frozen=True)
class Condition:
    metric: str
    min: Decimal | None = None
    max: Decimal | None = None


@dataclass(frozen=True)
class BandRule:
    band: str
    when: tuple[Condition, ...]


@dataclass(frozen=True)
class Knockout:
    open_finding_severity: str
    hold_states: tuple[str, ...]


@dataclass(frozen=True)
class ReviewCap:
    cap_band_at: str


@dataclass(frozen=True)
class HistoryRule:
    metric: str
    min_days: Decimal
    short_history_share_factor: Decimal


@dataclass(frozen=True)
class LimitRule:
    base_metric: str
    share: Mapping[str, Decimal]
    max: Mapping[str, Decimal]
    history: HistoryRule


@dataclass(frozen=True)
class PolicySpec:
    version: str
    metric_set: str
    check_set: str
    knockout: Knockout
    bands: tuple[BandRule, ...]
    review_findings: ReviewCap
    limit: LimitRule


def _mapping(
    v: Any, path: str, allowed: set[str], required: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(v).__name__}")
    unknown = sorted(str(k) for k in v if k not in allowed)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown}")
    missing = sorted((allowed if required is None else required) - set(v))
    if missing:
        raise ValueError(f"{path}: missing key(s) {missing}")
    return v


def _str(v: Any, path: str) -> str:
    if not isinstance(v, str) or not v:
        raise ValueError(f"{path}: expected a non-empty string")
    return v


def _num(v: Any, path: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{path}: expected a number, got {type(v).__name__}")
    d = Decimal(str(v))
    if not d.is_finite():
        raise ValueError(f"{path}: not finite: {v!r}")
    return d


def _band_map(v: Any, path: str) -> dict[str, Decimal]:
    m = _mapping(v, path, set(BAND_ORDER))
    return {b: _num(m[b], f"{path}.{b}") for b in BAND_ORDER}


def _band(v: Any, path: str) -> str:
    if v not in BAND_ORDER:
        raise ValueError(f"{path}: band must be one of {list(BAND_ORDER)}, got {v!r}")
    return str(v)


def _conditions(v: Any, path: str) -> tuple[Condition, ...]:
    if not isinstance(v, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(v).__name__}")
    out = []
    for metric, bounds in v.items():
        p = f"{path}.{metric}"
        b = _mapping(bounds, p, {"min", "max"}, required=set())
        if not b:
            raise ValueError(f"{p}: needs 'min' and/or 'max'")
        out.append(
            Condition(
                metric=_str(metric, p),
                min=_num(b["min"], f"{p}.min") if "min" in b else None,
                max=_num(b["max"], f"{p}.max") if "max" in b else None,
            )
        )
    return tuple(out)


def parse_policy(spec: Any) -> PolicySpec:
    top = _mapping(
        spec,
        "policy",
        {"version", "metric_set", "check_set", "knockout", "bands", "review_findings", "limit"},
    )
    version = _str(str(top["version"]), "policy.version")
    if not _VERSION_RE.match(version):
        raise ValueError(f"policy.version: expected MAJOR.MINOR.PATCH, got {version!r}")

    ko = _mapping(
        top["knockout"],
        "policy.knockout",
        {"open_finding_severity", "hold_states"},
        {"hold_states"},
    )
    states = ko["hold_states"]
    if not isinstance(states, list) or not states:
        raise ValueError("policy.knockout.hold_states: expected a non-empty list")
    for i, s in enumerate(states):
        if s not in _STATES:
            raise ValueError(f"policy.knockout.hold_states[{i}]: unknown review state {s!r}")
    severity = ko.get("open_finding_severity", "hold")
    if severity != "hold":
        raise ValueError("policy.knockout.open_finding_severity: only 'hold' is supported")
    knockout = Knockout(severity, tuple(states))

    raw_bands = top["bands"]
    if not isinstance(raw_bands, list) or not raw_bands:
        raise ValueError("policy.bands: expected a non-empty list")
    bands = []
    for i, rb in enumerate(raw_bands):
        p = f"policy.bands[{i}]"
        m = _mapping(rb, p, {"band", "when"})
        bands.append(BandRule(_band(m["band"], f"{p}.band"), _conditions(m["when"], f"{p}.when")))
    got = [b.band for b in bands]
    if got != list(BAND_ORDER):
        raise ValueError(
            f"policy.bands: bands must be in order {list(BAND_ORDER)} (D last), got {got}"
        )
    if bands[-1].when:
        raise ValueError("policy.bands[3].when: band D must be unconditional (empty)")

    rf = _mapping(top["review_findings"], "policy.review_findings", {"cap_band_at"})
    cap = ReviewCap(_band(rf["cap_band_at"], "policy.review_findings.cap_band_at"))

    lim = _mapping(top["limit"], "policy.limit", {"base_metric", "share", "max", "history"})
    hist = _mapping(
        lim["history"],
        "policy.limit.history",
        {"metric", "min_days", "short_history_share_factor"},
    )
    factor = _num(
        hist["short_history_share_factor"], "policy.limit.history.short_history_share_factor"
    )
    if not Decimal(0) < factor <= Decimal(1):
        raise ValueError("policy.limit.history.short_history_share_factor: must be in (0, 1]")
    limit = LimitRule(
        base_metric=_str(lim["base_metric"], "policy.limit.base_metric"),
        share=_band_map(lim["share"], "policy.limit.share"),
        max=_band_map(lim["max"], "policy.limit.max"),
        history=HistoryRule(
            _str(hist["metric"], "policy.limit.history.metric"),
            _num(hist["min_days"], "policy.limit.history.min_days"),
            factor,
        ),
    )
    return PolicySpec(
        version=version,
        metric_set=_str(top["metric_set"], "policy.metric_set"),
        check_set=_str(top["check_set"], "policy.check_set"),
        knockout=knockout,
        bands=tuple(bands),
        review_findings=cap,
        limit=limit,
    )
