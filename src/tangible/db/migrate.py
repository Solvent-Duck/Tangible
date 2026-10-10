"""Apply the SQL files in migrations/ in filename order, once each."""

from pathlib import Path

import psycopg

from tangible.config import database_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(conninfo: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def main() -> None:
    applied = migrate(database_url())
    print("applied: " + ", ".join(applied) if applied else "up to date")


if __name__ == "__main__":
    main()
