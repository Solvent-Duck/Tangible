"""The shared demo login: credentials for a fixed subject's token, beside Clerk auth."""

import dataclasses
import time
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from tangible.workbench.app import create_app
from tangible.workbench.demo_login import ISSUER, TOKEN_PREFIX, DemoLogin
from test_onboarding import make_tenant
from test_workbench_clerk import FakeClerk, bearer, settings

pytestmark = pytest.mark.db

SECRET = "s" * 40


@pytest.fixture
def clerk() -> FakeClerk:
    return FakeClerk()


def demo_app(url: str, tmp_path: Path, clerk: FakeClerk, subject: str) -> TestClient:
    s = settings(
        url, tmp_path, demo_login_user="tester", demo_login_password="pw-123",
        demo_login_secret=SECRET, demo_login_subject=subject,
    )  # fmt: skip
    return TestClient(create_app(s, http_client=clerk.client()))


def test_login_gives_the_linked_subjects_seat(
    test_db_url: str, tmp_path: Path, clerk: FakeClerk
) -> None:
    px = f"d{time.time_ns()}-"
    tenant = make_tenant(test_db_url, prefix=px)
    http = demo_app(test_db_url, tmp_path, clerk, px + "adm")

    def login(username: str, password: str) -> httpx.Response:
        return http.post("/api/demo-login", json={"username": username, "password": password})

    assert login("tester", "x").status_code == 401
    assert login("x", "pw-123").status_code == 401
    r = login("tester", "pw-123")
    assert r.status_code == 200
    token = r.json()["token"]
    assert token.startswith(TOKEN_PREFIX)

    assert http.get("/api/invites", headers=bearer(token)).status_code == 200
    me = http.get("/api/me", headers=bearer(token)).json()
    assert [(m["tenant_id"], m["role"]) for m in me["memberships"]] == [(tenant, "credit_admin")]
    # Clerk tokens still work beside it.
    assert http.get("/api/invites", headers=bearer(clerk.token(px + "adm"))).status_code == 200


def test_forged_expired_and_other_subject_tokens_are_rejected(
    test_db_url: str, tmp_path: Path, clerk: FakeClerk
) -> None:
    px = f"d{time.time_ns()}-"
    make_tenant(test_db_url, prefix=px)
    http = demo_app(test_db_url, tmp_path, clerk, px + "adm")
    now = int(time.time())

    def tok(secret: str, **over: object) -> str:
        claims = {"iss": ISSUER, "sub": px + "adm", "iat": now, "exp": now + 60} | over
        return TOKEN_PREFIX + jwt.encode(claims, secret, algorithm="HS256")

    assert http.get("/api/invites", headers=bearer(tok(SECRET))).status_code == 200
    for bad in (
        tok("x" * 40),  # wrong secret
        tok(SECRET, exp=now - 600),
        tok(SECRET, sub=px + "ana"),  # only the configured subject
        tok(SECRET, iss="someone-else"),
    ):
        assert http.get("/api/invites", headers=bearer(bad)).status_code == 401


def test_off_unless_fully_configured(test_db_url: str, tmp_path: Path, clerk: FakeClerk) -> None:
    http = TestClient(create_app(settings(test_db_url, tmp_path), http_client=clerk.client()))
    assert http.post("/api/demo-login", json={"username": "a", "password": "b"}).status_code == 404
    half = settings(test_db_url, tmp_path, demo_login_user="tester")
    with pytest.raises(ValueError):
        create_app(half, http_client=clerk.client())
    with pytest.raises(ValueError):
        DemoLogin("u", "p", "short")
    dev = dataclasses.replace(
        settings(test_db_url, tmp_path), dev_auth=True, demo_login_user="u",
        demo_login_password="p", demo_login_secret=SECRET,
    )  # fmt: skip
    with pytest.raises(ValueError):
        dev.build_demo_login()
