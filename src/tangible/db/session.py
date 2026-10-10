"""Database connections.

App code always goes through `tenant_session`: it runs as `tangible_app` with `app.tenant_id`
set, so row-level security confines every query to one tenant. `admin_connect` exists only for
bootstrap (creating tenant rows, which RLS forbids to `tangible_app`); never use it to read or
write tenant data.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]


@contextmanager
def tenant_session(conninfo: str, tenant_id: str) -> Iterator[Conn]:
    """Connection as `tangible_app` for one tenant. Commits on clean exit, else rolls back."""
    conn: Conn = psycopg.connect(conninfo, autocommit=True)
    try:
        conn.execute("SET ROLE tangible_app")
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
        conn.autocommit = False
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def admin_connect(conninfo: str) -> Conn:
    """Autocommit connection as the owning role (bypasses RLS). Bootstrap only."""
    return psycopg.connect(conninfo, autocommit=True)
