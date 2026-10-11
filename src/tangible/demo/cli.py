"""tangible-demo: set up a demo tenant on synthetic data dated to the present.

    tangible-demo seed [--name demo] [--end-on DATE] [--store .evidence] [--work data/demo]
                       [--lines data/processed/uci_lines.csv] [--run-agent]
                       [--link-subject CLERK_USER_ID] [--link-analyst CLERK_USER_ID]
    tangible-demo statements --tenant-id T --borrower NAME --out DIR [--month YYYY-MM]
                             [--account REF]

`seed`: each run creates a new tenant and prints its web-app sign-in URL. `--run-agent` makes
live model calls (about $1.50 per staged case) and needs TANGIBLE_MODEL_BACKEND and its
settings; the Makefile has the demo values.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from tangible.config import database_url
from tangible.demo.seed import (
    DEFAULT_BASE_URL,
    WORKBENCH_DEMO_URL,
    SeedError,
    seed_demo,
)
from tangible.demo.statements import (
    StatementError,
    borrower_ledger,
    build_spec,
    describe,
    parse_month,
    write_statements,
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
    s.add_argument("--base-url", default=DEFAULT_BASE_URL, help="where the web app is served")
    s.add_argument(
        "--link-subject", default=None, metavar="SUBJECT",
        help="also make this sign-in subject (a Clerk user id) the tenant's credit admin",
    )  # fmt: skip
    s.add_argument(
        "--link-analyst", default=None, metavar="SUBJECT",
        help="also make this sign-in subject the tenant's analyst, with the cases assigned to it",
    )  # fmt: skip
    s.add_argument(
        "--run-agent", action="store_true",
        help="drive the staged cases with the live model (costs about $1.50 per case)",
    )  # fmt: skip
    st = sub.add_parser(
        "statements", help="write genuine, forged, edited and injection PDF statements"
    )
    st.add_argument("--tenant-id", required=True, help="tenant id (see the seed summary)")
    st.add_argument("--borrower", required=True, help="borrower name, e.g. 'Harbour & Pine ...'")
    st.add_argument("--out", type=Path, required=True, help="folder for the four PDFs")
    st.add_argument(
        "--month", type=parse_month, default=None, metavar="YYYY-MM",
        help="statement month (default: the last full month in the data)",
    )  # fmt: skip
    st.add_argument("--account", default=None, help="bank account (default: the busiest one)")
    args = p.parse_args(argv)

    if args.command == "statements":
        _statements(args)
        return
    try:
        summary = seed_demo(
            name=args.name, end_on=args.end_on, store=args.store, work=args.work,
            lines=args.lines, run_agent=args.run_agent, base_url=args.base_url,
            link_subject=args.link_subject, link_analyst=args.link_analyst,
        )  # fmt: skip
    except SeedError as e:
        print(f"tangible-demo: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print()
    print(summary.render())
    if args.base_url == DEFAULT_BASE_URL:
        print(f"\nWith the API on the workbench-demo launch config, run the web app as "
              f"TANGIBLE_API_URL={WORKBENCH_DEMO_URL} make web-dev")  # fmt: skip


def _statements(args: argparse.Namespace) -> None:
    try:
        ledger = borrower_ledger(database_url(), args.tenant_id, args.borrower)
        spec = build_spec(ledger, month=args.month, account=args.account)
    except StatementError as e:
        print(f"tangible-demo: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print(describe(spec))
    for kind, path in write_statements(spec, args.borrower, args.out).items():
        print(f"  {kind:<9} {path}")


if __name__ == "__main__":
    main()
