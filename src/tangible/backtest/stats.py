"""Backtest statistics. Floats are used throughout: these are reporting statistics, not money.

Nothing here feeds a decision; the policy and the pure metric/check functions decide. All
functions are pure over `LoanResult`s (or plain number lists for the primitives).
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from tangible.backtest.runner import LoanResult

# plant -> (check that must fire, its severity). Mirrors EXPECTED in tests/test_plants_matrix.py.
EXPECTED: Final[dict[str, tuple[str, str]]] = {
    "hidden_reserve": ("payouts_vs_deposits", "hold"),
    "phantom_payouts": ("orders_vs_payouts", "review"),
    "partial_coverage": ("partial_coverage", "hold"),
    "self_transfer_revenue": ("self_transfers_as_revenue", "review"),
    "undisclosed_lender_debit": ("unexplained_recurring_debit", "review"),
}
LOUD: Final = ("review", "hold")
NO_BAND: Final = "none"  # no decision (a hold knocked the case out)
BAND_ORDER: Final = ("A", "B", "C", "D", NO_BAND)


# ------------------------------------------------------------------ primitives


def auc(risk_scores: Sequence[float], defaulted: Sequence[bool]) -> float | None:
    """P(score of a random defaulter > score of a random repayer), ties count half
    (Mann-Whitney U / (n1 * n0)). None unless both classes are present."""
    bad = [s for s, d in zip(risk_scores, defaulted, strict=True) if d]
    good = [s for s, d in zip(risk_scores, defaulted, strict=True) if not d]
    if not bad or not good:
        return None
    wins = 0.0
    for b in bad:
        for g in good:
            wins += 1.0 if b > g else 0.5 if b == g else 0.0
    return wins / (len(bad) * len(good))


def directed_auc(scores: Sequence[float], defaulted: Sequence[bool]) -> tuple[float, str] | None:
    """AUC with the direction chosen to be >= 0.5: ("higher_is_riskier" | "lower_is_riskier")."""
    a = auc(scores, defaulted)
    if a is None:
        return None
    return (a, "higher_is_riskier") if a >= 0.5 else (1.0 - a, "lower_is_riskier")


def ks(scores: Sequence[float], defaulted: Sequence[bool]) -> float | None:
    """Two-sample Kolmogorov-Smirnov statistic between defaulters and repayers."""
    bad = sorted(s for s, d in zip(scores, defaulted, strict=True) if d)
    good = sorted(s for s, d in zip(scores, defaulted, strict=True) if not d)
    if not bad or not good:
        return None
    best = 0.0
    for x in sorted(set(bad) | set(good)):
        fb = sum(1 for s in bad if s <= x) / len(bad)
        fg = sum(1 for s in good if s <= x) / len(good)
        best = max(best, abs(fb - fg))
    return best


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


# ------------------------------------------------------------------ report sections


def loss_by_band(loans: Sequence[LoanResult]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for band in BAND_ORDER:
        rows = [r for r in loans if (r.band or NO_BAND) == band]
        defaults = [r for r in rows if r.loan.defaulted]
        out[band] = {
            "count": len(rows),
            "defaults": len(defaults),
            "default_rate": _rate(len(defaults), len(rows)),
            "loan_amount": float(sum(r.loan.loan_amount for r in rows)),
            "loss_amount": float(sum(r.loan.loss_amount for r in rows)),
        }
    return out


def metric_discrimination(loans: Sequence[LoanResult]) -> dict[str, dict[str, Any]]:
    """Per metric: AUC (direction chosen to be >= 0.5), KS, and the share of loans with None."""
    keys = sorted({k for r in loans for k in r.metrics})
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        vals = [(r.metrics.get(key), r.loan.defaulted) for r in loans]
        present = [(float(v), d) for v, d in vals if v is not None]
        scores = [v for v, _ in present]
        flags = [d for _, d in present]
        directed = directed_auc(scores, flags)
        out[key] = {
            "n": len(present),
            "none_share": _rate(len(vals) - len(present), len(vals)),
            "auc": directed[0] if directed else None,
            "direction": directed[1] if directed else None,
            "ks": ks(scores, flags),
        }
    return out


def _loud_keys(r: LoanResult) -> set[str]:
    return {f.check_key for f in r.findings if f.severity in LOUD}


def finding_lift(loans: Sequence[LoanResult]) -> dict[str, dict[str, Any]]:
    """Default rate with a review/hold finding of each check present vs absent."""
    keys = sorted({k for r in loans for k in _loud_keys(r)})
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        with_ = [r for r in loans if key in _loud_keys(r)]
        without = [r for r in loans if key not in _loud_keys(r)]
        d_with = sum(r.loan.defaulted for r in with_)
        d_without = sum(r.loan.defaulted for r in without)
        rate_with, rate_without = _rate(d_with, len(with_)), _rate(d_without, len(without))
        lift = rate_with / rate_without if rate_with is not None and rate_without else None
        out[key] = {
            "present": len(with_),
            "absent": len(without),
            "default_rate_present": rate_with,
            "default_rate_absent": rate_without,
            "lift": lift,
        }
    return out


def plant_catch(loans: Sequence[LoanResult]) -> dict[str, Any]:
    """Planted problems whose expected check fired at review or hold severity."""
    per: dict[str, dict[str, int]] = defaultdict(lambda: {"planted": 0, "caught": 0})
    misses: list[dict[str, str]] = []
    for r in loans:
        for plant in r.loan.planted:
            check, _sev = EXPECTED[plant]
            per[plant]["planted"] += 1
            if check in _loud_keys(r):
                per[plant]["caught"] += 1
            else:
                misses.append({"borrower_key": r.loan.borrower_key, "plant": plant})
    planted = sum(v["planted"] for v in per.values())
    caught = sum(v["caught"] for v in per.values())
    return {
        "planted": planted,
        "caught": caught,
        "catch_rate": _rate(caught, planted),
        "by_plant": {p: {**v, "catch_rate": _rate(v["caught"], v["planted"])}
                     for p, v in sorted(per.items())},
        "misses": misses,
    }  # fmt: skip


def false_alarms(loans: Sequence[LoanResult]) -> dict[str, Any]:
    """Review/hold findings on loans with no planted problem."""
    clean = [r for r in loans if not r.loan.planted]
    counts = [sum(1 for f in r.findings if f.severity in LOUD) for r in clean]
    by_check: dict[str, int] = defaultdict(int)
    for r in clean:
        for f in r.findings:
            if f.severity in LOUD:
                by_check[f.check_key] += 1
    return {
        "clean_loans": len(clean),
        "alarms": sum(counts),
        "per_clean_loan": _rate(sum(counts), len(clean)),
        "loans_with_alarm": sum(1 for c in counts if c),
        "by_check": dict(sorted(by_check.items())),
    }


def replay_summary(loans: Sequence[LoanResult]) -> dict[str, Any]:
    bad = [r for r in loans if not r.replay_match]
    return {
        "loans": len(loans),
        "matched": len(loans) - len(bad),
        "mismatches": [{"borrower_key": r.loan.borrower_key, "diffs": list(r.replay_diffs)}
                       for r in bad],
    }  # fmt: skip


def leak_summary(loans: Sequence[LoanResult]) -> dict[str, Any]:
    return {
        "loans": len(loans),
        "violations": sum(r.leak.violations for r in loans),
        "hidden_rows": sum(r.leak.hidden_rows for r in loans),
        "visible_rows": sum(r.leak.visible_rows for r in loans),
        "loans_with_hidden_rows": sum(1 for r in loans if r.leak.hidden_rows > 0),
    }


def agent_summary(loans: Sequence[LoanResult], requested_runs: int) -> dict[str, Any]:
    """Run-to-run spread and cost. "not run" when no agent was supplied."""
    if requested_runs == 0:
        return {"status": "not run"}
    runs = [r.agent_runs for r in loans]
    flat = [a for rs in runs for a in rs]
    multi = [rs for rs in runs if len(rs) >= 2]
    agree = [len({a.band for a in rs}) == 1 for rs in multi]
    pairs = [(a, b) for rs in multi for i, a in enumerate(rs) for b in rs[i + 1 :]]
    cases = len(flat)
    return {
        "status": "run",
        "runs_per_loan": requested_runs,
        "cases": cases,
        "loans_all_runs_agree": _rate(sum(agree), len(agree)),
        "pairwise_band_agreement": _rate(sum(a.band == b.band for a, b in pairs), len(pairs)),
        "spread": "not measurable (agent_runs=1)" if requested_runs < 2 else "measured",
        "total_cost_usd": float(sum(a.cost_usd for a in flat)),
        "cost_per_case_usd": float(sum(a.cost_usd for a in flat)) / cases if cases else None,
        "steps_per_case": sum(a.steps for a in flat) / cases if cases else None,
    }


def compute_stats(loans: Sequence[LoanResult], agent_requested_runs: int = 0) -> dict[str, Any]:
    defaults = sum(r.loan.defaulted for r in loans)
    return {
        "cohort": {
            "loans": len(loans),
            "defaulted": defaults,
            "repaid": len(loans) - defaults,
            "default_rate": _rate(defaults, len(loans)),
            "synthetic": sum(r.loan.synthetic for r in loans),
            "planted_loans": sum(1 for r in loans if r.loan.planted),
            "total_loan_amount": float(sum(r.loan.loan_amount for r in loans)),
            "total_loss_amount": float(sum(r.loan.loss_amount for r in loans)),
            "with_band": sum(1 for r in loans if r.band is not None),
        },
        "loss_by_band": loss_by_band(loans),
        "metrics": metric_discrimination(loans),
        "finding_lift": finding_lift(loans),
        "plants": plant_catch(loans),
        "false_alarms": false_alarms(loans),
        "replay": replay_summary(loans),
        "time_leak": leak_summary(loans),
        "agent": agent_summary(loans, agent_requested_runs),
    }
