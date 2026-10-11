"""tangible-demo: set up a demo tenant on synthetic data dated to the present.

    tangible-demo seed [--name demo] [--end-on DATE] [--store .evidence] [--work data/demo]
                       [--lines data/processed/uci_lines.csv] [--run-agent]

Each run creates a new tenant and prints its dev-login URL. `--run-agent` makes live model
calls (about $1.50 per staged case) and needs TANGIBLE_MODEL_BACKEND and its settings; the
Makefile has the demo values.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from tangible.demo.seed import (
    DEFAULT_BASE_URL,
    WORKBENCH_DEMO_URL,
    SeedError,
    seed_demo,
)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tangible-demo")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("seed", help="create a fresh demo tenant with borrowers, users and cases")
    s.add_argument("--name", default="demo", help="tenant name prefix (a timestamp is appended)")
    s.add_argument(
        "--end-on", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="the newest order lands on or just before this date (default: today minus 7 days)",
    )  # fmt: skip
    s.add_argument("--store", type=Path, default=Path(".evidence"), help="evidence store folder")
    s.add_argument("--work", type=Path, default=Path("data/demo"), help="scratch folder")
    s.add_argument("--lines", type=Path, default=Path("data/processed/uci_lines.csv"))
    s.add_argument("--base-url", default=DEFAULT_BASE_URL, help="where the workbench is served")
    s.add_argument(
        "--run-agent", action="store_true",
        help="drive the staged cases with the live model (costs about $1.50 per case)",
    )  # fmt: skip
    args = p.parse_args(argv)

    try:
        summary = seed_demo(
            name=args.name, end_on=args.end_on, store=args.store, work=args.work,
            lines=args.lines, run_agent=args.run_agent, base_url=args.base_url,
        )  # fmt: skip
    except SeedError as e:
        print(f"tangible-demo: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print()
    print(summary.render())
    if args.base_url == DEFAULT_BASE_URL:
        print(f"\nOn the workbench-demo launch config: {WORKBENCH_DEMO_URL}/dev/login"
              f"?tenant={summary.tenant_id}")  # fmt: skip


if __name__ == "__main__":
    main()
