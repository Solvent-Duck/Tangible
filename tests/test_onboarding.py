"""Borrower onboarding: invites, the outbox, the public invite link, the borrower's side and the
bridge to diligence. Dev header auth through FastAPI's TestClient; no model, no network."""

import dataclasses
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from tangible.agent.llm import NullClient
from tangible.db.session import admin_connect, tenant_session
from tangible.onboarding import catalog
from tangible.workbench.app import create_app
from tangible.workbench.settings import Settings
from test_assessment import POLICY_PATH

pytestmark = pytest.mark.db

STAFF = (
    ("adm", "credit_admin"),
    ("ana", "analyst"),
    ("len", "lender_underwriter"),
)
STOCK_CSV = b"sku,qty,unit_cost\nA1,4,9.50\n"
TOKEN = re.compile(r"/invite/([A-Za-z0-9_-]{20,})")


def make_tenant(url: str, name: str = "Harbor Credit", prefix: str = "") -> str:
    """A tenant with one user per staff role; subjects are `prefix` + adm/ana/len."""
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
    assert row
    tenant = str(row[0])
    with tenant_session(url, tenant) as conn:
        for subject, role in STAFF:
            conn.execute(
                "INSERT INTO app_user (tenant_id, subject, email, display_name, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant, prefix + subject, f"{subject}@x.test", subject.title(), role),
            )
    return tenant


class On(NamedTuple):
    http: TestClient
    url: str
    tenant: str

    def h(self, user: str, tenant: str | None = None) -> dict[str, str]:
        return {"X-Tangible-Tenant": tenant or self.tenant, "X-Tangible-User": user}

    def call(
        self, method: str, path: str, user: str | None, expect: int | None = None, **kw: Any
    ) -> Any:
        headers = self.h(user) if user else {}
        headers.update(kw.pop("headers", {}))
        r = self.http.request(method, path, headers=headers, **kw)
        if expect is not None:
            assert r.status_code == expect, (r.status_code, r.text)
        return (
            r.json()
            if r.content and r.headers.get("content-type", "").startswith("application/json")
            else r
        )

    def invite(self, **over: Any) -> dict[str, Any]:
        body = {
            "email": "owner@acme.test",
            "business_name": "Acme Hardware",
            "contact_name": "Rosa",
            "loan_amount": "75000",
            "message": "Looking forward to it.",
            "items": ["stock_report", "website", "inventory_system"],
            "custom": [{"label": "Lease agreement", "kind": "upload"}],
        } | over
        out: dict[str, Any] = self.call("POST", "/api/invites", "adm", 201, json=body)
        return out

    def accept(self, token: str, subject: str, expect: int = 200) -> Any:
        return self.call(
            "POST", "/api/onboarding/accept", None, expect,
            headers={"X-Tangible-User": subject, "X-Tangible-Email": f"{subject}@acme.test"},
            json={"token": token},
        )  # fmt: skip


def token_of(link: str) -> str:
    m = TOKEN.search(link)
    assert m, link
    return m.group(1)


def make_on(url: str, tmp_path: Path, **settings: Any) -> On:
    s = Settings(
        database_url=url, evidence_dir=tmp_path / "evidence", policy_path=POLICY_PATH,
        dev_auth=True, client_factory=NullClient, public_url="https://app.test",
    )  # fmt: skip
    s = dataclasses.replace(s, **settings)
    return On(TestClient(create_app(s)), url, make_tenant(url))


@pytest.fixture
def on(test_db_url: str, tmp_path: Path) -> On:
    return make_on(test_db_url, tmp_path)


# ---------------------------------------------------------------- catalog


def test_catalog_locks_standard_financials_and_rejects_unknown_items() -> None:
    req = catalog.resolve_requests(["website"], None)
    locked = [i.id for i in catalog.ITEMS.values() if i.locked]
    assert locked and set(locked) <= set(req["items"])
    assert "website" in req["items"]
    with pytest.raises(catalog.CatalogError):
        catalog.resolve_requests(["ssn"], None)
    with pytest.raises(catalog.CatalogError):
        catalog.resolve_requests([], [{"label": "x", "kind": "connector"}])
    with pytest.raises(catalog.CatalogError):
        catalog.resolve_requests([], [{"label": f"c{n}"} for n in range(catalog.MAX_CUSTOM + 1)])
    # No government identifiers anywhere in the catalog.
    text = " ".join(f"{i.label} {i.help}" for i in catalog.ITEMS.values()).lower()
    assert not any(w in text for w in ("ssn", "social security", "passport", "ein"))


# ---------------------------------------------------------------- the whole flow


def test_invite_to_case_end_to_end(on: On) -> None:
    sent = on.invite()
    token = token_of(sent["link"])
    assert sent["link"].startswith("https://app.test/invite/")

    # The raw token is never stored, only its hash.
    with tenant_session(on.url, on.tenant) as conn:
        row = conn.execute("SELECT token_hash FROM invite WHERE id = %s", (sent["id"],)).fetchone()
    assert row and bytes(row[0]) == hashlib.sha256(token.encode()).digest()

    # The outbox holds the email, with the link and the requested items.
    outbox = on.call("GET", "/api/outbox", "adm", 200)
    assert [e["to_addr"] for e in outbox] == ["owner@acme.test"]
    email = on.call("GET", f"/api/outbox/{outbox[0]['id']}", "len", 200)
    assert token in email["body_text"] and token in email["body_html"]
    assert "Lease agreement" in email["body_text"] and "Stock report" in email["body_text"]
    assert email["transport"] == "outbox" and email["delivered_at"]

    # Public preview: no auth, minimal, records 'opened' once.
    pv = on.call("GET", f"/api/onboarding/invite/{token}", None, 200)
    assert pv["state"] == "live" and pv["lender_name"] == "Harbor Credit"
    assert pv["business_name"] == "Acme Hardware"
    assert {"bank", "stock_report", "custom-1"} <= {i["id"] for i in pv["items"]}
    on.call("GET", f"/api/onboarding/invite/{token}", None, 200)
    detail = on.call("GET", f"/api/invites/{sent['id']}", "adm", 200)
    assert detail["stage"] == "opened" and detail["email"] == "owner@acme.test"
    assert token in detail["sent_email"]["body_text"]

    # Accept: a borrower user appears; accepting again is a no-op.
    acc = on.accept(token, "owner")
    assert acc["tenant_id"] == on.tenant and acc["borrower_id"] == sent["borrower_id"]
    on.accept(token, "owner")
    me = on.call("GET", "/api/me", None, 200, headers={"X-Tangible-User": "owner"})
    assert [(m["tenant_id"], m["role"]) for m in me["memberships"]] == [(on.tenant, "borrower")]

    st = on.call("GET", "/api/onboarding/state", "owner", 200)
    assert st["business_name"] == "Acme Hardware" and not st["can_submit"]
    on.call("POST", "/api/onboarding/submit", "owner", 409)

    on.call("POST", "/api/onboarding/profile", "owner", 200,
            json={"legal_name": "Acme Hardware LLC", "state": "or", "year_founded": 2009,
                  "website": "acme.test", "stated_revenue_90d": "120,000"})  # fmt: skip
    on.call("POST", "/api/onboarding/items/bank/connect", "owner", 200, json={"provider": "plaid"})
    on.call("POST", "/api/onboarding/items/sales/connect", "owner", 200,
            json={"provider": "shopify"})  # fmt: skip
    on.call("POST", "/api/onboarding/items/stock_report/documents", "owner", 201,
            files={"file": ("stock.csv", STOCK_CSV, "text/csv")})  # fmt: skip
    on.call("POST", "/api/onboarding/items/custom-1/documents", "owner", 201,
            files={"file": ("lease.pdf", b"%PDF-1.4 lease", "application/pdf")})  # fmt: skip
    st = on.call("POST", "/api/onboarding/items/website/answer", "owner", 200,
                 json={"value": "https://acme.test"})  # fmt: skip
    done = {i["id"] for i in st["items"] if i["done"]}
    assert {"bank", "sales", "stock_report", "custom-1", "website"} <= done
    assert "entity" not in {i["id"] for i in st["items"]}  # not requested in this invite
    assert st["profile"]["state"] == "OR" and st["profile"]["stated_revenue_90d"] == "120000.00"
    assert st["can_submit"]

    with tenant_session(on.url, on.tenant) as conn:
        conns = conn.execute(
            "SELECT source, scopes FROM connection WHERE borrower_id = %s ORDER BY source",
            (sent["borrower_id"],),
        ).fetchall()
        uploads = conn.execute(
            "SELECT count(*) FROM evidence_pull WHERE borrower_id = %s "
            "AND source = 'document_upload'",
            (sent["borrower_id"],),
        ).fetchone()
    assert conns == [("plaid", ["transactions", "balances"]),
                     ("shopify", ["orders", "refunds", "payouts"])]  # fmt: skip
    assert uploads == (2,)

    on.call("POST", f"/api/invites/{sent['id']}/open-case", "adm", 409)  # not submitted yet
    on.call("POST", "/api/onboarding/submit", "owner", 200)
    on.call("POST", "/api/onboarding/submit", "owner", 409)
    on.call("POST", "/api/onboarding/items/website/answer", "owner", 409,
            json={"value": "https://late.test"})  # fmt: skip

    dash = on.call("GET", "/api/dashboard", "len", 200)
    assert dash["kpis"]["ready_for_diligence"] == 1
    assert [r["stage"] for r in dash["pipeline"]] == ["submitted"]
    assert dash["activity"][0]["kind"] == "submitted"

    case = on.call("POST", f"/api/invites/{sent['id']}/open-case", "adm", 201,
                   json={"assigned_analyst": "ana"})  # fmt: skip
    assert case["borrower_id"] == sent["borrower_id"] and case["loan_amount"] == "75000.00"
    on.call("POST", f"/api/invites/{sent['id']}/open-case", "adm", 409)
    dash = on.call("GET", "/api/dashboard", "adm", 200)
    assert dash["pipeline"][0]["stage"] == "in_diligence"
    assert dash["kpis"]["in_diligence"] == 1
    assert on.call("GET", f"/api/cases/{case['id']}", "ana", 200)["status"] == "open"


# ---------------------------------------------------------------- authorization


def test_borrower_sees_onboarding_only(on: On) -> None:
    sent = on.invite()
    on.accept(token_of(sent["link"]), "owner")
    case = on.call("POST", "/api/cases", "adm", 201, json={
        "borrower_id": sent["borrower_id"], "as_of": "2026-01-01T00:00:00+00:00",
    })  # fmt: skip
    for method, path in (
        ("GET", "/api/cases"),
        ("GET", f"/api/cases/{case['id']}"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/invites"),
        ("GET", "/api/outbox"),
        ("POST", "/api/invites"),
    ):
        on.call(method, path, "owner", 403, json={})
    # Staff have no onboarding state.
    on.call("GET", "/api/onboarding/state", "adm", 403)


def test_only_credit_admin_invites(on: On) -> None:
    body = {"email": "a@b.test", "business_name": "B"}
    on.call("POST", "/api/invites", "ana", 403, json=body)
    on.call("POST", "/api/invites", "len", 403, json=body)
    on.call("GET", "/api/invites", "ana", 403)
    on.call("GET", "/api/invites", "len", 200)
    on.call("POST", "/api/invites", "adm", 422, json={"email": "nope", "business_name": "B"})
    on.call("POST", "/api/invites", "adm", 422, json=body | {"items": ["ssn"]})


def test_each_borrower_sees_its_own_business(on: On) -> None:
    a = on.invite()
    b = on.invite(email="b@b.test", business_name="Bayside Bakery")
    on.accept(token_of(a["link"]), "owner-a")
    on.accept(token_of(b["link"]), "owner-b")
    assert on.call("GET", "/api/onboarding/state", "owner-a", 200)["business_name"] == (
        "Acme Hardware"
    )
    assert on.call("GET", "/api/onboarding/state", "owner-b", 200)["business_name"] == (
        "Bayside Bakery"
    )
    # A borrower user belongs to one borrower: owner-a cannot also accept B's invite.
    on.accept(token_of(b["link"]), "owner-a", 409)
    # And staff cannot become a borrower in their own tenant.
    on.accept(token_of(a["link"]), "adm", 409)


def test_item_rules(on: On) -> None:
    sent = on.invite(items=[])
    on.accept(token_of(sent["link"]), "owner")
    on.call("POST", "/api/onboarding/items/bank/connect", "owner", 422, json={"provider": "xero"})
    on.call("POST", "/api/onboarding/items/website/answer", "owner", 404, json={"value": "x.test"})
    on.call("POST", "/api/onboarding/items/bank/answer", "owner", 422, json={"value": "x.test"})
    on.call("POST", "/api/onboarding/items/bank_statements/documents", "owner", 422,
            files={"file": ("empty.pdf", b"", "application/pdf")})  # fmt: skip
    on.call("POST", "/api/onboarding/profile", "owner", 422, json={"legal_name": ""})
    on.call("POST", "/api/onboarding/profile", "owner", 422,
            json={"legal_name": "X", "year_founded": 1066})  # fmt: skip


# ---------------------------------------------------------------- invite lifecycle


def test_revoked_invite_cannot_be_accepted(on: On) -> None:
    sent = on.invite()
    token = token_of(sent["link"])
    on.call("POST", f"/api/invites/{sent['id']}/revoke", "adm", 200, json={"reason": "dup"})
    assert on.call("GET", f"/api/onboarding/invite/{token}", None, 200)["state"] == "revoked"
    on.accept(token, "owner", 409)
    on.call("POST", f"/api/invites/{sent['id']}/revoke", "adm", 409, json={})
    assert on.call("GET", "/api/invites", "adm", 200)[0]["state"] == "revoked"


def test_resend_replaces_the_old_link(on: On) -> None:
    sent = on.invite()
    again = on.call("POST", f"/api/invites/{sent['id']}/resend", "adm", 201)
    assert again["borrower_id"] == sent["borrower_id"] and again["id"] != sent["id"]
    on.accept(token_of(sent["link"]), "owner", 409)
    on.accept(token_of(again["link"]), "owner")
    assert [i["id"] for i in on.call("GET", "/api/invites", "adm", 200)] == [again["id"]]
    assert len(on.call("GET", "/api/outbox", "adm", 200)) == 2


def test_expired_invite(test_db_url: str, tmp_path: Path) -> None:
    on = make_on(test_db_url, tmp_path, invite_days=0)
    sent = on.invite()
    token = token_of(sent["link"])
    assert on.call("GET", f"/api/onboarding/invite/{token}", None, 200)["state"] == "expired"
    on.accept(token, "owner", 409)


def test_unknown_token(on: On) -> None:
    on.call("GET", "/api/onboarding/invite/not-a-real-token", None, 404)
    on.accept("not-a-real-token", "owner", 404)
    on.call("GET", f"/api/invites/{uuid.uuid4()}", "adm", 404)


# ---------------------------------------------------------------- tenancy


def test_new_tables_are_tenant_isolated(on: On, test_db_url: str) -> None:
    sent = on.invite()
    on.accept(token_of(sent["link"]), "owner")
    on.call("POST", "/api/onboarding/profile", "owner", 200, json={"legal_name": "Acme LLC"})
    other = make_tenant(test_db_url, "Other Lender")
    with tenant_session(test_db_url, other) as conn:
        for table in ("invite", "invite_event", "borrower_profile", "outbound_email"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row == (0,), table
    on.call("GET", f"/api/invites/{sent['id']}", "adm", 404,
            headers={"X-Tangible-Tenant": other})  # fmt: skip
    # The invite token still resolves to its own tenant, whoever opens the link.
    assert (
        on.call("GET", f"/api/onboarding/invite/{token_of(sent['link'])}", None, 200)["lender_name"]
        == "Harbor Credit"
    )


def test_invites_and_events_are_append_only(on: On) -> None:
    sent = on.invite()
    import psycopg

    for sql in (
        "UPDATE invite SET email = 'x@y.test'",
        "DELETE FROM invite_event",
    ):
        with pytest.raises(psycopg.Error), tenant_session(on.url, on.tenant) as conn:
            conn.execute(sql)
    assert sent["id"]


# ---------------------------------------------------------------- application ledger row


def test_profile_writes_an_application_row_with_the_import_mix(on: On) -> None:
    sent = on.invite(items=["import_mix", "entity"])
    on.accept(token_of(sent["link"]), "owner")
    on.call("POST", "/api/onboarding/profile", "owner", 422, json={
        "legal_name": "Acme LLC", "import_mix": [{"hs_chapter": "9", "country": "CHN",
                                                  "share_of_cogs": 0.5}]})  # fmt: skip
    on.call("POST", "/api/onboarding/profile", "owner", 422, json={
        "legal_name": "Acme LLC", "import_mix": [
            {"hs_chapter": "94", "country": "CN", "share_of_cogs": 0.7},
            {"hs_chapter": "73", "country": "VN", "share_of_cogs": 0.4}]})  # fmt: skip
    st = on.call("POST", "/api/onboarding/profile", "owner", 200, json={
        "legal_name": "Acme Hardware LLC", "state": "OR", "website": "acme.test",
        "import_mix": [{"hs_chapter": "94", "country": "cn", "share_of_cogs": "0.6"}]})  # fmt: skip
    assert next(i for i in st["items"] if i["id"] == "import_mix")["done"]
    with tenant_session(on.url, on.tenant) as conn:
        rows = conn.execute(
            "SELECT source_ref, legal_name, state, website, requested_amount, import_mix "
            "FROM application WHERE borrower_id = %s ORDER BY observed_at",
            (sent["borrower_id"],),
        ).fetchall()
    assert len(rows) == 1  # refused saves wrote nothing
    ref, name, state, site, amount, mix = rows[0]
    assert ref == f"onboarding:{sent['borrower_id']}" and name == "Acme Hardware LLC"
    assert (state, site, str(amount)) == ("OR", "https://acme.test", "75000.00")
    assert mix == [{"hs_chapter": "94", "country": "CN", "share_of_cogs": "0.6000"}]
    # a correction is a new row with the same source_ref; "imports nothing" is an empty list
    on.call("POST", "/api/onboarding/profile", "owner", 200,
            json={"legal_name": "Acme Hardware LLC", "import_mix": []})  # fmt: skip
    with tenant_session(on.url, on.tenant) as conn:
        mixes = [r[0] for r in conn.execute(
            "SELECT import_mix FROM application WHERE borrower_id = %s ORDER BY observed_at",
            (sent["borrower_id"],),
        ).fetchall()]  # fmt: skip
    assert mixes == [[{"hs_chapter": "94", "country": "CN", "share_of_cogs": "0.6000"}], []]


def test_public_invite_routes_are_rate_limited(test_db_url: str, tmp_path: Path) -> None:
    on = make_on(test_db_url, tmp_path, public_rate_per_minute=3)
    for _ in range(3):
        on.call("GET", "/api/onboarding/invite/nope", None, 404)
    on.call("GET", "/api/onboarding/invite/nope", None, 429)
    on.accept("nope", "x", 429)  # same client, same window
    r = on.http.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_demo_pipeline_reaches_every_stage(test_db_url: str, tmp_path: Path) -> None:
    from tangible.demo.pipeline import PIPELINE, seed_pipeline
    from tangible.workbench.auth import lookup_principal

    on = make_on(test_db_url, tmp_path)
    settings = on.http.app.state.settings  # type: ignore[attr-defined]
    seed_pipeline(settings, lookup_principal(on.url, on.tenant, "adm"), log=lambda _: None)
    stages = {r["business_name"]: r["stage"]
              for r in on.call("GET", "/api/invites", "adm", 200)}  # fmt: skip
    assert stages == {p.business: p.stage for p in PIPELINE}
    dash = on.call("GET", "/api/dashboard", "adm", 200)
    assert (
        dash["kpis"]["ready_for_diligence"] == 1 and len(on.call("GET", "/api/outbox", "adm")) == 4
    )
    ready = next(r for r in dash["pipeline"] if r["stage"] == "submitted")
    case = on.call("POST", f"/api/invites/{ready['id']}/open-case", "adm", 201,
                   json={"assigned_analyst": "ana"})  # fmt: skip
    ws = on.call("GET", f"/api/cases/{case['id']}/workspace", "ana", 200)
    assert ws["systemic"]["stated"] and {
        line["hs_chapter"] for line in ws["systemic"]["lines"]
    } == {"94", "44"}


def test_catalog_rc2_keeps_every_rc1_item_and_adds_payroll_and_the_support_desk():
    rc1 = {
        "bank", "accounting", "sales", "bank_statements", "inventory_system", "stock_report",
        "aged_stock", "website", "google_business", "yelp", "social", "reviews", "customers",
        "entity", "year_founded", "press", "import_mix",
    }
    assert catalog.CURRENT_VERSION == "rc-2"
    assert set(catalog.ITEMS) == rc1 | {"payroll", "support_desk"}
    assert "devrev" in catalog.ITEMS["support_desk"].providers
    assert not catalog.ITEMS["support_desk"].locked and not catalog.ITEMS["payroll"].locked
    for item in catalog.ITEMS.values():
        assert len(set(item.providers)) == len(item.providers), item.id
