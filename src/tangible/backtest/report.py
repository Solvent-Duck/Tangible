"""Markdown + JSON backtest report."""

import json
from pathlib import Path
from typing import Any

from tangible.backtest.runner import BacktestResult
from tangible.backtest.stats import compute_stats

SYNTHETIC_BANNER = (
    "SYNTHETIC COHORT — labels are planted problems, not observed repayment outcomes."
)


def build_report(result: BacktestResult) -> dict[str, Any]:
    loans = list(result.loans)
    return {
        "synthetic": any(r.loan.synthetic for r in loans),
        "policy": {"version": result.policy_version, "sha256": result.policy_hash},
        "stats": compute_stats(loans, result.agent_requested_runs),
        "loans": [
            {
                "borrower_key": r.loan.borrower_key,
                "decision_date": r.loan.decision_date.isoformat(),
                "outcome": r.loan.outcome,
                "loan_amount": str(r.loan.loan_amount),
                "loss_amount": str(r.loan.loss_amount),
                "planted": list(r.loan.planted),
                "band": r.band,
                "limit": None if r.limit is None else str(r.limit),
                "findings": [
                    {"check_key": f.check_key, "severity": f.severity, "origin": f.origin}
                    for f in r.findings
                ],
                "metrics": {k: None if v is None else str(v) for k, v in r.metrics.items()},
                "replay_match": r.replay_match,
                "hidden_rows": r.leak.hidden_rows,
                "visible_rows": r.leak.visible_rows,
            }
            for r in loans
        ],
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None, nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return [*out, ""]


def render_markdown(report: dict[str, Any]) -> str:
    s = report["stats"]
    lines: list[str] = []
    if report["synthetic"]:
        lines += [SYNTHETIC_BANNER, ""]
        lines += [
            "Synthetic runs test the plumbing and the checks' catch rates, not predictive power. "
            "AUC, KS, lift and default rates below reflect how the labels were planted.",
            "",
        ]
    lines += ["# Backtest report", ""]
    p = report["policy"]
    lines += [f"Policy {p['version']} (sha256 {p['sha256'][:12]})", ""]

    c = s["cohort"]
    lines += ["## Cohort summary", ""]
    lines += _table(
        ["loans", "defaulted", "repaid", "default rate", "with a band", "loan amount", "loss"],
        [[str(c["loans"]), str(c["defaulted"]), str(c["repaid"]), _pct(c["default_rate"]),
          str(c["with_band"]), f"{c['total_loan_amount']:,.2f}", f"{c['total_loss_amount']:,.2f}"]],
    )  # fmt: skip

    lines += ["## Loss by band", "", "Band `none` means a hold finding blocked a decision.", ""]
    lines += _table(
        ["band", "loans", "defaults", "default rate", "loan amount", "loss amount"],
        [[b, str(v["count"]), str(v["defaults"]), _pct(v["default_rate"]),
          f"{v['loan_amount']:,.2f}", f"{v['loss_amount']:,.2f}"]
         for b, v in s["loss_by_band"].items() if v["count"]],
    )  # fmt: skip

    lines += ["## Metric discrimination (AUC, KS)", ""]
    lines += ["AUC direction is chosen so AUC >= 0.5; `direction` says which way is riskier.", ""]
    lines += _table(
        ["metric", "n", "AUC", "direction", "KS", "None share"],
        [[k, str(v["n"]), _num(v["auc"]), v["direction"] or "n/a", _num(v["ks"]),
          _pct(v["none_share"])] for k, v in s["metrics"].items()],
    )  # fmt: skip

    lines += [
        "## Finding lift",
        "",
        "Default rate with a review/hold finding present vs absent.",
        "",
    ]
    lines += _table(
        ["check", "present", "absent", "default rate present", "default rate absent", "lift"],
        [[k, str(v["present"]), str(v["absent"]), _pct(v["default_rate_present"]),
          _pct(v["default_rate_absent"]), _num(v["lift"], 2)]
         for k, v in s["finding_lift"].items()],
    )  # fmt: skip

    pl = s["plants"]
    lines += ["## Plants caught", ""]
    lines += [f"Caught {pl['caught']} of {pl['planted']} ({_pct(pl['catch_rate'])}).", ""]
    lines += _table(
        ["plant", "planted", "caught", "catch rate"],
        [[k, str(v["planted"]), str(v["caught"]), _pct(v["catch_rate"])]
         for k, v in pl["by_plant"].items()],
    )  # fmt: skip
    for m in pl["misses"]:
        lines.append(f"- missed: {m['borrower_key']} ({m['plant']})")
    if pl["misses"]:
        lines.append("")

    fa = s["false_alarms"]
    lines += ["## False alarms", ""]
    lines += [
        f"{fa['alarms']} review/hold findings on {fa['clean_loans']} clean loans "
        f"({_num(fa['per_clean_loan'], 2)} per clean loan; "
        f"{fa['loans_with_alarm']} loans affected).",
        "",
    ]
    lines += _table(["check", "alarms"], [[k, str(n)] for k, n in fa["by_check"].items()])

    r = s["replay"]
    lines += [
        "## Replay matches",
        "",
        f"{r['matched']} of {r['loans']} frozen assessments replay.",
        "",
    ]
    for m in r["mismatches"]:
        lines.append(f"- MISMATCH {m['borrower_key']}: {'; '.join(m['diffs'])}")
    if r["mismatches"]:
        lines.append("")

    t = s["time_leak"]
    lines += [
        "## Time-leak guard",
        "",
        f"Violations (visible rows observed after the decision date): {t['violations']}.",
        f"Rows hidden by the time rule: {t['hidden_rows']} across "
        f"{t['loans_with_hidden_rows']} of {t['loans']} loans; {t['visible_rows']} rows visible.",
        "",
    ]

    a = s["agent"]
    lines += ["## Agent spread and cost", ""]
    if a["status"] == "not run":
        lines += ["Agent: not run.", ""]
    else:
        lines += [
            f"{a['cases']} agent cases ({a['runs_per_loan']} run(s) per loan).",
            f"Spread: {a['spread']}; loans with identical band across runs: "
            f"{_pct(a['loans_all_runs_agree'])}; pairwise band agreement: "
            f"{_pct(a['pairwise_band_agreement'])}.",
            f"Cost per case: ${_num(a['cost_per_case_usd'], 4)} "
            f"(total ${_num(a['total_cost_usd'], 4)}); "
            f"steps per case: {_num(a['steps_per_case'], 1)}.",
            "",
        ]
    return "\n".join(lines)


def write_report(result: BacktestResult, md_path: Path) -> tuple[Path, Path]:
    """Write `md_path` and a sibling `.json`; returns both paths."""
    md_path = Path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    report = build_report(result)
    md_path.write_text(render_markdown(report))
    json_path = md_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return md_path, json_path
