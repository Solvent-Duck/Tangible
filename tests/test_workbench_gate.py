"""The shared-password gate for hosted dev-auth demos."""

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tangible.workbench.app import create_app
from tangible.workbench.gate import PasswordGate
from tangible.workbench.settings import Settings


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/")
    def home() -> dict[str, str]:
        return {"page": "home"}

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(PasswordGate, password="s3cret pass:word")
    return TestClient(app)


def test_requests_without_the_password_get_a_basic_challenge(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic ")
    assert r.headers["cache-control"] == "no-store"
    assert "home" not in r.text


@pytest.mark.parametrize(
    "headers",
    [
        basic("anyone", "wrong"),
        basic("anyone", "s3cret pass"),
        {"Authorization": "Bearer s3cret pass:word"},
        {"Authorization": "Basic not-base64!!"},
        {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
    ],
)
def test_wrong_or_malformed_credentials_are_refused(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.get("/", headers=headers).status_code == 401


def test_the_right_password_with_any_username_passes(client: TestClient) -> None:
    for user in ("", "ana"):  # RFC 7617: the user-id holds no colon
        r = client.get("/", headers=basic(user, "s3cret pass:word"))
        assert r.status_code == 200 and r.json() == {"page": "home"}


def test_the_health_check_is_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}


def test_an_empty_password_is_rejected() -> None:
    with pytest.raises(ValueError):
        PasswordGate(FastAPI(), password="")


def test_the_gate_refuses_to_run_with_oidc() -> None:
    s = Settings(database_url="postgresql://unused", evidence_dir=Path("."), access_password="x")
    with pytest.raises(ValueError, match="dev-auth"):
        create_app(s)


def test_create_app_gates_everything_but_the_health_check() -> None:
    s = Settings(
        database_url="postgresql://unused", evidence_dir=Path("."), dev_auth=True,
        access_password="pw",
    )  # fmt: skip
    c = TestClient(create_app(s))
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/api/invites/catalog").status_code == 401
    assert c.get("/api/invites/catalog", headers=basic("", "pw")).status_code == 200
    assert c.get("/api/dev/tenants").status_code == 401
