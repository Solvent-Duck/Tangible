"""Pure policy evaluation. The policy decides; nothing here consults an agent or a clock.

Boundaries are inclusive: a ``max`` condition holds when ``value <= max`` and a ``min``
condition holds when ``value >= min``.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from tangible import numfmt
from tangible.checks.registry import Severity
from tangible.policy.loader import Policy
from tangible.policy.review import ReviewState
from tangible.policy.schema import BAND_ORDER, PolicySpec, Stress, Uplift

_CENT = Decimal("0.01")


FindingOrigin = Literal["rule", "model"]


@dataclass(frozen=True)
class FindingInput:
    """`origin` is "rule" for a check's finding and "model" for the agent's. The default is
    "rule", so a caller that forgets it gets the stricter (open holds block) behaviour."""

    finding_id: str
    check_key: str
    severity: Severity
    state: ReviewState
    origin: FindingOrigin = "rule"


@dataclass(frozen=True)
class PolicyResult:
    band: str | None
    limit: Decimal | None
    rules_fired: list[dict[str, str]]
    policy_version: str
    policy_sha256: str
    result_sha256: str


def _result_hash(band: str | None, limit: Decimal | None, rules: list[dict[str, str]]) -> str:
    payload: dict[str, Any] = {
        "band": band,
        "limit": None if limit is None else str(limit),
        "rules_fired": rules,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


_FAILED_OP = {">=": "<", "<=": ">"}


@dataclass(frozen=True)
class _Words:
    """How figures are written into rules. Policies without a `report` block keep plain Decimal
    text, so assessments frozen under them replay to the same rules; 0.6.0+ write dollars to
    the cent and other numbers to two decimals. Comparisons always use the exact values."""

    num: Callable[[Decimal], str]
    money: Callable[[Decimal], str]


_PLAIN = _Words(str, str)
_REPORT = _Words(numfmt.num, numfmt.usd)


def _uplift(
    up: Uplift,
    band: str,
    metrics: Mapping[str, Decimal | None],
    rules: list[dict[str, str]],
    w: _Words,
) -> str:
    """Lift `band` by at most one notch, never above the ceiling, when any enabled route's
    conditions all hold. Every route is recorded with the values it saw, including why it did
    not fire (adverse-action reasons may need them)."""
    idx, ceiling = BAND_ORDER.index(band), BAND_ORDER.index(up.ceiling)
    if up.max_notches == 0 or idx <= ceiling:
        why = (
            "max_notches is 0"
            if up.max_notches == 0
            else f"band {band} is at or above the ceiling {up.ceiling}"
        )
        rules.append(
            {
                "rule": f"uplift: not applied, {why}",
                "step": "uplift",
                "saw": band,
                "op": "ceiling",
                "threshold": up.ceiling,
                "outcome": "not_applicable",
            }
        )
        return band
    fired: tuple[str, str] | None = None
    for route in up.routes:
        conds = up.conditions(route)
        thresholds = "; ".join(
            f"{c.metric} {op} {w.num(thr)}"
            for c in conds
            for op, thr in (("<=", c.max), (">=", c.min))
            if thr is not None
        )
        if not route.enabled:
            rules.append(
                {
                    "rule": f"uplift route {route.name}: disabled in this policy",
                    "step": "uplift",
                    "route": route.name,
                    "saw": "not evaluated",
                    "op": "all",
                    "threshold": thresholds,
                    "outcome": "disabled",
                }
            )
            continue
        parts: list[str] = []
        missing = failed = False
        for c in conds:
            value = metrics.get(c.metric)
            for op, thr in (("<=", c.max), (">=", c.min)):
                if thr is None:
                    continue
                if value is None:
                    missing = True
                    parts.append(f"{c.metric} none (needs {op} {w.num(thr)})")
                    continue
                ok = value <= thr if op == "<=" else value >= thr
                failed = failed or not ok
                parts.append(
                    f"{c.metric} {w.num(value)} {op if ok else _FAILED_OP[op]} {w.num(thr)}"
                )
        outcome = "not_evaluated" if missing else ("fail" if failed else "pass")
        saw = "; ".join(parts)
        entry = {
            "rule": f"uplift route {route.name}: {saw}",
            "step": "uplift",
            "route": route.name,
            "saw": saw,
            "op": "all",
            "threshold": thresholds,
            "outcome": outcome,
        }
        if missing:
            entry["status"] = "not_evaluated"
        rules.append(entry)
        if outcome == "pass" and fired is None:
            fired = (route.name, saw)
    if fired is None:
        rules.append(
            {
                "rule": f"uplift: no route fired; band stays {band}",
                "step": "uplift",
                "saw": band,
                "op": "notch",
                "threshold": up.ceiling,
                "outcome": "none",
            }
        )
        return band
    lifted = BAND_ORDER[idx - 1]  # one notch; idx > ceiling, so never above it
    rules.append(
        {
            "rule": f"uplift {band}\u2192{lifted} via {fired[0]}: {fired[1]}",
            "step": "uplift",
            "route": fired[0],
            "saw": band,
            "op": "notch",
            "threshold": up.ceiling,
            "outcome": "lifted",
        }
    )
    return lifted


def evaluate(
    policy: PolicySpec | Policy,
    metrics: Mapping[str, Decimal | None],
    findings: Sequence[FindingInput],
    *,
    policy_sha256: str = "",
) -> PolicyResult:
    """Run knockout, bands, uplift (when the policy has one), review cap and limit in order.
    Pass a loaded ``Policy`` to carry its sha256 into the result, or a bare ``PolicySpec`` plus
    ``policy_sha256``."""
    if isinstance(policy, Policy):
        if policy.parsed is None:
            raise ValueError("Policy has no parsed spec")
        policy_sha256, spec = policy.sha256, policy.parsed
    else:
        spec = policy
    rules: list[dict[str, str]] = []
    w = _PLAIN if spec.report is None else _REPORT

    def done(band: str | None, limit: Decimal | None) -> PolicyResult:
        return PolicyResult(
            band, limit, rules, spec.version, policy_sha256, _result_hash(band, limit, rules)
        )

    # 1. knockout
    ko = spec.knockout
    holds = [f for f in findings if f.severity == ko.open_finding_severity]
    if ko.model_hold_states is None:
        # Policies that do not distinguish origin (0.2.x): the original rule, unchanged.
        blocking = [f for f in holds if f.state in ko.hold_states]
        for f in sorted(blocking, key=lambda f: f.finding_id):
            rules.append(
                {
                    "rule": f"knockout: hold finding {f.check_key} is {f.state}",
                    "step": "knockout",
                    "finding_id": f.finding_id,
                    "saw": f.state,
                    "op": "in",
                    "threshold": ",".join(ko.hold_states),
                    "outcome": "knockout",
                }
            )
        if blocking:
            return done(None, None)
        threshold = ",".join(ko.hold_states)
    else:
        # Rule holds block in `hold_states`; agent holds only in `model_hold_states`
        # (an agent finding is a lead until an analyst accepts it). Every hold is reported.
        model_states = ko.model_hold_states
        blocking = []
        for f in sorted(holds, key=lambda f: f.finding_id):
            is_model = f.origin == "model"
            states = model_states if is_model else ko.hold_states
            blocks = f.state in states
            if blocks:
                blocking.append(f)
            if not is_model:
                if blocks:
                    rules.append(
                        {
                            "rule": f"knockout: hold finding {f.check_key} is {f.state}",
                            "step": "knockout",
                            "finding_id": f.finding_id,
                            "origin": "rule",
                            "saw": f.state,
                            "op": "in",
                            "threshold": ",".join(states),
                            "outcome": "knockout",
                        }
                    )
                continue
            if blocks:
                text = f"knockout: agent hold finding {f.check_key} is {f.state}"
            elif f.state == "open":
                text = (
                    f"knockout: agent hold finding {f.check_key} is open (not accepted), "
                    "not blocking"
                )
            else:
                text = f"knockout: agent hold finding {f.check_key} is {f.state}, not blocking"
            rules.append(
                {
                    "rule": text,
                    "step": "knockout",
                    "finding_id": f.finding_id,
                    "origin": "model",
                    "saw": f.state,
                    "op": "in",
                    "threshold": ",".join(states),
                    "outcome": "knockout" if blocks else "not_blocking",
                }
            )
        if blocking:
            return done(None, None)
        threshold = f"rule:{','.join(ko.hold_states)};model:{','.join(model_states)}"
    rules.append(
        {
            "rule": "knockout: no blocking hold finding",
            "step": "knockout",
            "saw": str(len(findings)),
            "op": "in",
            "threshold": threshold,
            "outcome": "pass",
        }
    )

    # 2. bands
    band = BAND_ORDER[-1]
    for br in spec.bands:
        satisfied = True
        for c in br.when:
            value = metrics.get(c.metric)
            for op, thr in (("<=", c.max), (">=", c.min)):
                if thr is None:
                    continue
                entry = {
                    "rule": f"band {br.band}: {c.metric} {op} {w.num(thr)}",
                    "step": "band",
                    "band": br.band,
                    "metric": c.metric,
                    "op": op,
                    "threshold": w.num(thr),
                }
                if value is None:
                    entry.update(saw="none", outcome="not_evaluated", status="not_evaluated")
                    satisfied = False
                else:
                    ok = value <= thr if op == "<=" else value >= thr
                    entry.update(saw=w.num(value), outcome="pass" if ok else "fail")
                    satisfied = satisfied and ok
                rules.append(entry)
        if satisfied:
            band = br.band
            rules.append(
                {
                    "rule": f"band {br.band} selected",
                    "step": "band",
                    "band": br.band,
                    "outcome": "selected",
                }
            )
            break

    # 2b. uplift (0.4.0+): after bands, before the review cap. A knockout returned above, so it
    # never runs after one. Policies without the key produce exactly the rules they always did.
    if spec.uplift is not None:
        band = _uplift(spec.uplift, band, metrics, rules, w)

    # 3. review cap
    cap = spec.review_findings.cap_band_at
    capping = [f for f in findings if f.severity == "review" and f.state in ("open", "accepted")]
    if capping and BAND_ORDER.index(band) < BAND_ORDER.index(cap):
        rules.append(
            {
                "rule": f"review findings cap band at {cap}",
                "step": "review_cap",
                "saw": band,
                "op": "cap",
                "threshold": cap,
                "outcome": "capped",
            }
        )
        band = cap

    # 4. limit
    lim = spec.limit
    base = metrics.get(lim.base_metric)
    if base is None:
        rules.append(
            {
                "rule": f"limit: base metric {lim.base_metric} unavailable",
                "step": "limit",
                "saw": "none",
                "outcome": "not_evaluated",
                "status": "not_evaluated",
            }
        )
        return done(band, None)
    share = lim.share[band]
    hist = metrics.get(lim.history.metric)
    short = hist is None or hist < lim.history.min_days
    rules.append(
        {
            "rule": f"limit: history {lim.history.metric} >= {w.num(lim.history.min_days)}",
            "step": "limit",
            "metric": lim.history.metric,
            "op": ">=",
            "threshold": w.num(lim.history.min_days),
            "saw": "none" if hist is None else w.num(hist),
            "outcome": "short_history" if short else "pass",
        }
    )
    if short:
        share = share * lim.history.short_history_share_factor
    raw = base * share
    cap_amt = lim.max[band]
    limit = min(raw, cap_amt).quantize(_CENT, rounding=ROUND_HALF_UP)
    rules.append(
        {
            "rule": f"limit: {w.num(share)} x {lim.base_metric}, max {w.money(cap_amt)}",
            "step": "limit",
            "saw": w.money(base),
            "op": "share*base,max",
            "threshold": w.money(cap_amt),
            "outcome": "capped_at_max" if raw > cap_amt else "computed",
        }
    )

    # 5. stressed limit (0.5.0+): may only reduce the limit; never touches the band.
    if spec.stress is not None:
        limit = _stress(spec.stress, limit, metrics, rules, w)
    return done(band, limit)


def _stress(
    stress: Stress,
    limit: Decimal,
    metrics: Mapping[str, Decimal | None],
    rules: list[dict[str, str]],
    w: _Words,
) -> Decimal:
    for i, r in enumerate(stress.rules):
        saw, missing, ok = [], [], True
        for c in r.when:
            value = metrics.get(c.metric)
            for op, thr in (("<=", c.max), (">=", c.min)):
                if thr is None:
                    continue
                if value is None:
                    missing.append(c.metric)
                    ok = False
                    continue
                holds = value <= thr if op == "<=" else value >= thr
                ok = ok and holds
                saw.append(
                    f"{c.metric} {w.num(value)} {op if holds else _FAILED_OP[op]} {w.num(thr)}"
                )
        if missing:
            rules.append(
                {
                    "rule": f"stress rule {i + 1}: not evaluated, missing {', '.join(missing)}",
                    "step": "stress", "saw": "none", "op": "all",
                    "threshold": f"x{w.num(r.limit_factor)}",
                    "outcome": "not_evaluated", "status": "not_evaluated",
                }
            )  # fmt: skip
            continue
        if ok:
            factor = max(r.limit_factor, stress.floor)
            stressed = (limit * factor).quantize(_CENT, rounding=ROUND_HALF_UP)
            rules.append(
                {
                    "rule": (
                        f"stressed limit {w.money(limit)}\u2192{w.money(stressed)} "
                        f"(x{w.num(factor)}): {'; '.join(saw)}"
                    ),
                    "step": "stress", "saw": "; ".join(saw), "op": "x",
                    "threshold": w.num(factor), "outcome": "reduced",
                }
            )  # fmt: skip
            return stressed
        rules.append(
            {
                "rule": f"stress rule {i + 1}: {'; '.join(saw)}",
                "step": "stress", "saw": "; ".join(saw), "op": "all",
                "threshold": f"x{w.num(r.limit_factor)}", "outcome": "fail",
            }
        )  # fmt: skip
    rules.append(
        {
            "rule": "stress: no rule fired; limit unchanged",
            "step": "stress", "saw": w.money(limit), "op": "x", "threshold": "1",
            "outcome": "unchanged",
        }
    )  # fmt: skip
    return limit
