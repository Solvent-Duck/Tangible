import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tangible.config import database_url
from tangible.db.migrate import migrate


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
