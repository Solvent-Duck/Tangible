"""tangible-backtest: build a synthetic cohort, or run a cohort through the pipeline."""

import argparse
import sys
import time
import uuid
from pathlib import Path

from tangible.backtest.cohort import load_cohort
from tangible.backtest.report import SYNTHETIC_BANNER, write_report
from tangible.backtest.runner import run_backtest
from tangible.backtest.synthetic_cohort import build_synthetic_cohort
from tangible.config import database_url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tangible-backtest")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="build a SYNTHETIC cohort (planted labels, not outcomes)")
    s.add_argument("--lines", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--plant-rate", type=float, default=0.4)

    r = sub.add_parser("run", help="run a cohort and write the report")
    r.add_argument("--cohort", type=Path, required=True)
    r.add_argument("--policy", type=Path, default=Path("policies/default.yaml"))
    r.add_argument("--report", type=Path, required=True)
    r.add_argument("--store", type=Path, help="evidence store dir (default: next to the report)")
    r.add_argument("--tenant-name", help="name for the fresh tenant (default: backtest-<id>)")

    args = p.parse_args(argv)
    if args.cmd == "synth":
        path = build_synthetic_cohort(args.lines, args.out, args.n, args.seed, args.plant_rate)
        print(SYNTHETIC_BANNER, file=sys.stderr)
        print(path)
        return 0

    cohort = load_cohort(args.cohort)
    store = args.store or args.report.parent / "evidence"
    name = args.tenant_name or f"backtest-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()

    def progress(i: int, n: int, key: str) -> None:
        print(f"[{i}/{n}] {key} ({time.monotonic() - t0:.0f}s)", file=sys.stderr)

    result = run_backtest(database_url(), name, cohort, args.policy, store, progress=progress)
    md, js = write_report(result, args.report)
    print(f"{md}\n{js}\nelapsed {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
