"""tangible-replay: replay the N most recent assessments of a tenant (nightly job)."""

import argparse
import json
from pathlib import Path

from tangible.assessment.replay import replay_sample
from tangible.config import database_url
from tangible.db.session import tenant_session


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tangible-replay")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--sample", type=int, default=10)
    p.add_argument("--policy", type=Path, default=Path("policies/default.yaml"))
    args = p.parse_args(argv)

    with tenant_session(database_url(), args.tenant_id) as conn:
        reports = replay_sample(conn, args.sample, args.policy)
    ok = all(r.match for r in reports)
    print(
        json.dumps(
            {
                "ok": ok,
                "replayed": len(reports),
                "mismatches": [
                    {"assessment_id": r.assessment_id, "diffs": r.diffs}
                    for r in reports
                    if not r.match
                ],
            },
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
