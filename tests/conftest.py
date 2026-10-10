import uuid
from collections.abc import Callable, Iterable, Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tangible.config import database_url
from tangible.datasets.synthetic import generate
from tangible.db.migrate import migrate
from tangible.ledger.csv_dir import read_ledger_dir
from tangible.ledger.types import Ledger


@pytest.fixture(scope="session")
def test_db_url() -> Iterator[str]:
    """A throwaway migrated database. Skips if Postgres isn't reachable (run `make db-up`)."""
    admin_url = database_url()
    name = f"tangible_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not available: {e}")
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(**{**conninfo_to_dict(admin_url), "dbname": name})
    try:
        migrate(url)
        yield url
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


# ---------------------------------------------------------------- synthetic ledgers

SyntheticLedger = Callable[..., Ledger]


@pytest.fixture(scope="session")
def synthetic_ledger(tmp_path_factory: pytest.TempPathFactory) -> SyntheticLedger:
    """Factory: synthetic_ledger(plant={"hidden_reserve"}, days=200, seed=7) -> Ledger.

    Builds a hand-made order file (see test_synthetic.write_lines), runs the synthetic generator
    with the given plants, and reads the output back with `read_ledger_dir`. Cached per args.
    """
    from test_synthetic import write_lines

    cache: dict[tuple[frozenset[str], int, int], Ledger] = {}

    def make(plant: Iterable[str] = (), days: int = 200, seed: int = 7) -> Ledger:
        key = (frozenset(plant), days, seed)
        if key not in cache:
            root = tmp_path_factory.mktemp("synth")
            lines = write_lines(root / "lines.csv", days=days)
            generate(lines, root / "out", seed=seed, plant=key[0])
            cache[key] = read_ledger_dir(root / "out", lines)
        return cache[key]

    return make
