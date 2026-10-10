"""Pure policy evaluation. The policy decides; nothing here consults an agent or a clock.

Boundaries are inclusive: a ``max`` condition holds when ``value <= max`` and a ``min``
condition holds when ``value >= min``.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tangible.checks.registry import Severity
from tangible.policy.loader import Policy
from tangible.policy.review import ReviewState
from tangible.policy.schema import BAND_ORDER, PolicySpec

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class FindingInput:
    finding_id: str
    check_key: str
    severity: Severity
    state: ReviewState


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


def evaluate(
    policy: PolicySpec | Policy,
    metrics: Mapping[str, Decimal | None],
    findings: Sequence[FindingInput],
    *,
    policy_sha256: str = "",
) -> PolicyResult:
    """Run knockout, bands, review cap and limit in order. Pass a loaded ``Policy`` to carry
    its sha256 into the result, or a bare ``PolicySpec`` plus ``policy_sha256``."""
    if isinstance(policy, Policy):
        if policy.parsed is None:
            raise ValueError("Policy has no parsed spec")
        policy_sha256, spec = policy.sha256, policy.parsed
    else:
        spec = policy
    rules: list[dict[str, str]] = []

    def done(band: str | None, limit: Decimal | None) -> PolicyResult:
        return PolicyResult(
            band, limit, rules, spec.version, policy_sha256, _result_hash(band, limit, rules)
        )

    # 1. knockout
    ko = spec.knockout
    blocking = [
        f for f in findings if f.severity == ko.open_finding_severity and f.state in ko.hold_states
    ]
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
    rules.append(
        {
            "rule": "knockout: no blocking hold finding",
            "step": "knockout",
            "saw": str(len(findings)),
            "op": "in",
            "threshold": ",".join(ko.hold_states),
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
                    "rule": f"band {br.band}: {c.metric} {op} {thr}",
                    "step": "band",
                    "band": br.band,
                    "metric": c.metric,
                    "op": op,
                    "threshold": str(thr),
                }
                if value is None:
                    entry.update(saw="none", outcome="not_evaluated", status="not_evaluated")
                    satisfied = False
                else:
                    ok = value <= thr if op == "<=" else value >= thr
                    entry.update(saw=str(value), outcome="pass" if ok else "fail")
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
            "rule": f"limit: history {lim.history.metric} >= {lim.history.min_days}",
            "step": "limit",
            "metric": lim.history.metric,
            "op": ">=",
            "threshold": str(lim.history.min_days),
            "saw": "none" if hist is None else str(hist),
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
            "rule": f"limit: {share} x {lim.base_metric}, max {cap_amt}",
            "step": "limit",
            "saw": str(base),
            "op": "share*base,max",
            "threshold": str(cap_amt),
            "outcome": "capped_at_max" if raw > cap_amt else "computed",
        }
    )
    return done(band, limit)
