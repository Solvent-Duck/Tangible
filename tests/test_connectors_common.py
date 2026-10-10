"""Shared helpers for connector tests (no tests here)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tangible.connectors.base import ConnectorAccount
from tangible.db.repo import create_borrower
from tangible.db.session import admin_connect, tenant_session

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"
TOKEN = "access-sandbox-0123456789-SUPERSECRET"
T0 = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 11, 9, 0, tzinfo=UTC)


def fx(name: str) -> str:
    return (FIXTURES / name).read_text()


class MemSecrets:
    def __init__(self, **values: str) -> None:
        self.values = {f"mem:{k}": v for k, v in values.items()}

    def get(self, ref: str) -> str:
        return self.values[ref]

    def put(self, ref: str, value: str) -> None:
        self.values[ref] = value


def secrets(token: str = TOKEN) -> MemSecrets:
    return MemSecrets(token=token, cid="client-id-xyz", csecret="client-secret-ABC")


class Script:
    """A MockTransport handler that serves queued responses and records requests."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert self.queue, f"unexpected request {request.method} {request.url.path}"
        nxt = self.queue.pop(0)
        return nxt(request) if callable(nxt) else nxt

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, text=fx(name), headers={"content-type": "application/json"})


def new_account(url: str, provider: str, revoked: bool = False) -> ConnectorAccount:
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (f"t-{provider}",)
        ).fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        crow = conn.execute(
            "INSERT INTO connection (tenant_id, borrower_id, source, granted_at, revoked_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (tenant, borrower, provider, T0, T0 if revoked else None),
        ).fetchone()
        assert crow
        arow = conn.execute(
            "INSERT INTO connector_account (tenant_id, borrower_id, connection_id, provider, "
            "secret_ref, external_account_id) VALUES (%s, %s, %s, %s, 'mem:token', 'ext-1') "
            "RETURNING id",
            (tenant, borrower, str(crow[0]), provider),
        ).fetchone()
        assert arow
    return ConnectorAccount(
        str(arow[0]), tenant, borrower, str(crow[0]), provider, "mem:token", "ext-1"
    )
