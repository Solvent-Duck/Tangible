"""tangible-ingest: load a synthetic directory + order lines for a named tenant and borrower."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from tangible.config import database_url
from tangible.db.repo import create_borrower
from tangible.db.session import admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tangible-ingest")
    p.add_argument("--tenant", required=True)
    p.add_argument("--borrower", required=True)
    p.add_argument("--dir", type=Path, default=Path("data/synthetic"))
    p.add_argument("--lines", type=Path, default=Path("data/processed/uci_lines.csv"))
    p.add_argument("--store", type=Path, default=Path(".evidence"))
    args = p.parse_args(argv)
    url = database_url()

    with admin_connect(url) as admin:
        row = admin.execute("SELECT id FROM tenant WHERE name = %s", (args.tenant,)).fetchone()
        if row is None:
            row = admin.execute(
                "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (args.tenant,)
            ).fetchone()
        assert row
        tenant_id = str(row[0])

    with tenant_session(url, tenant_id) as conn:
        found = conn.execute(
            "SELECT id FROM borrower WHERE name = %s ORDER BY created_at LIMIT 1", (args.borrower,)
        ).fetchone()
        borrower_id = str(found[0]) if found else create_borrower(conn, tenant_id, args.borrower)
        result = ingest_synthetic_dir(
            conn, LocalEvidenceStore(args.store), tenant_id, borrower_id, args.dir, args.lines
        )

    print(json.dumps({"tenant_id": tenant_id, "borrower_id": borrower_id, **asdict(result)}))


if __name__ == "__main__":
    main()
