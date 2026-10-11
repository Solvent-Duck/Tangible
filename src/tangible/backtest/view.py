"""Read the latest backtest report JSON and shape it for the workbench's /backtest page.

Read-only and display-only: nothing here computes a metric or a band. The numbers shown are the
ones `tangible-backtest run` wrote (see `report.build_report`). The report path is read from the
environment here (not workbench Settings) so the backtest package stays independent of the app.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_ENV = "TANGIBLE_BACKTEST_REPORT"
DEFAULT_REPORT = Path("data/backtest/report.json")
TOP_METRICS = 10
_BAND_ORDER = ("A", "B", "C", "D", "none")

GENERATE_HINT = (
    "uv run tangible-backtest synth --lines data/processed/uci_lines.csv "
    "--out data/backtest/cohort && "
    "uv run tangible-backtest run --cohort data/backtest/cohort/cohort.csv "
    "--report data/backtest/report.md"
)


@dataclass(frozen=True)
class Loaded:
    """`data` is None when the file is missing or unreadable; `problem` says which."""

    path: Path
    data: dict[str, Any] | None
    problem: str | None = None


def report_path() -> Path:
    return Path(os.environ.get(REPORT_ENV) or DEFAULT_REPORT)


def load(path: Path | None = None) -> Loaded:
    path = path or report_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return Loaded(path, None, "missing")
    except OSError as e:
        return Loaded(path, None, f"could not read the report ({type(e).__name__})")
    try:
        data = json.loads(raw)
    except ValueError:
        return Loaded(path, None, "the report is not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("stats"), dict):
        return Loaded(path, None, "the report has no 'stats' section")
    return Loaded(path, data)


# ---------------------------------------------------------------- formatting


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def pct(v: Any) -> str:
    x = _num(v)
    return "n/a" if x is None else f"{x * 100:.2f}%"


def money(v: Any) -> str:
    x = _num(v)
    return "n/a" if x is None else f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def dec(v: Any, nd: int = 2) -> str:
    x = _num(v)
    return "n/a" if x is None else f"{x:.{nd}f}"


def count(v: Any) -> str:
    x = _num(v)
    return "n/a" if x is None else f"{int(x):,}"


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """Page-ready, pre-formatted strings for the template (which escapes them)."""
    stats = _d(data.get("stats"))
    cohort, plants = _d(stats.get("cohort")), _d(stats.get("plants"))
    alarms, replay = _d(stats.get("false_alarms")), _d(stats.get("replay"))
    leak, agent = _d(stats.get("time_leak")), _d(stats.get("agent"))
    policy = _d(data.get("policy"))

    bands = _d(stats.get("loss_by_band"))
    ordered = [b for b in _BAND_ORDER if b in bands] + sorted(
        b for b in bands if b not in _BAND_ORDER
    )
    loss_by_band = [
        {
            "band": "no band" if b == "none" else str(b),
            "count": count(_d(bands[b]).get("count")),
            "defaults": count(_d(bands[b]).get("defaults")),
            "default_rate": pct(_d(bands[b]).get("default_rate")),
            "loan_amount": money(_d(bands[b]).get("loan_amount")),
            "loss_amount": money(_d(bands[b]).get("loss_amount")),
        }
        for b in ordered
    ]

    by_plant = _d(plants.get("by_plant"))
    plant_rows = [
        {
            "plant": str(k),
            "planted": count(_d(v).get("planted")),
            "caught": count(_d(v).get("caught")),
            "catch_rate": pct(_d(v).get("catch_rate")),
        }
        for k, v in sorted(by_plant.items())
    ]
    misses = plants.get("misses")

    by_check = _d(alarms.get("by_check"))
    mismatches = replay.get("mismatches")

    metrics = [
        (str(k), _d(v))
        for k, v in _d(stats.get("metrics")).items()
        if _num(_d(v).get("auc")) is not None
    ]
    metrics.sort(key=lambda kv: (-(_num(kv[1].get("auc")) or 0.0), kv[0]))
    top_metrics = [
        {
            "metric": k,
            "auc": dec(v.get("auc")),
            "ks": dec(v.get("ks")),
            "direction": str(v.get("direction") or "n/a").replace("_", " "),
            "n": count(v.get("n")),
            "none_share": pct(v.get("none_share")),
        }
        for k, v in metrics[:TOP_METRICS]
    ]

    violations = _num(leak.get("violations"))
    matched, total = _num(replay.get("matched")), _num(replay.get("loans"))
    return {
        "synthetic": data.get("synthetic") is not False,
        "policy_version": str(policy.get("version") or "n/a"),
        "policy_sha": str(policy.get("sha256") or ""),
        "agent_status": str(agent.get("status") or "") or None,
        "cohort": {
            "loans": count(cohort.get("loans")),
            "planted_loans": count(cohort.get("planted_loans")),
            "defaulted": count(cohort.get("defaulted")),
            "repaid": count(cohort.get("repaid")),
            "default_rate": pct(cohort.get("default_rate")),
            "with_band": count(cohort.get("with_band")),
            "total_loan_amount": money(cohort.get("total_loan_amount")),
            "total_loss_amount": money(cohort.get("total_loss_amount")),
        },
        "loss_by_band": loss_by_band,
        "plants": {
            "catch_rate": pct(plants.get("catch_rate")),
            "caught": count(plants.get("caught")),
            "planted": count(plants.get("planted")),
            "rows": plant_rows,
            "misses": [
                f"{_d(m).get('borrower_key')}: {_d(m).get('plant')}"
                for m in (misses if isinstance(misses, list) else [])
            ],
        },
        "false_alarms": {
            "per_clean_loan": dec(alarms.get("per_clean_loan"), 2),
            "alarms": count(alarms.get("alarms")),
            "clean_loans": count(alarms.get("clean_loans")),
            "loans_with_alarm": count(alarms.get("loans_with_alarm")),
            "by_check": [(str(k), count(v)) for k, v in sorted(by_check.items())],
        },
        "replay": {
            "matched": count(replay.get("matched")),
            "loans": count(replay.get("loans")),
            "all_match": matched is not None and matched == total,
            "mismatches": [
                f"{_d(m).get('borrower_key')}: {'; '.join(map(str, _d(m).get('diffs') or []))}"
                for m in (mismatches if isinstance(mismatches, list) else [])
            ],
        },
        "time_leak": {
            "violations": count(leak.get("violations")),
            "clean": violations == 0,
            "hidden_rows": count(leak.get("hidden_rows")),
            "visible_rows": count(leak.get("visible_rows")),
            "loans": count(leak.get("loans")),
        },
        "top_metrics": top_metrics,
        "metric_count": len(metrics),
    }
