import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tangible.agent.tools import ToolContext, call_tool, open_context, tool_definitions
from tangible.datasets.synthetic import generate
from tangible.db.cases import create_case
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.policy import load_policy
from tangible.web.fixtures import fixture_provider
from tangible.web.provider import (
    MAX_TEXT_CHARS,
    AllowListedProvider,
    FetchError,
    FixtureFetcher,
    WebError,
    extract_text,
    host_allowed,
)
from tangible.web.sources import DEFAULT_SOURCES, SOURCE_NAMES
from test_synthetic import write_lines

FIXTURES = Path(__file__).parent / "fixtures" / "web"
POLICY = load_policy(Path(__file__).parents[1] / "policies" / "default.yaml")


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("network use in test")

    monkeypatch.setattr(socket, "socket", boom)


@pytest.fixture
def provider() -> AllowListedProvider:
    return fixture_provider(FIXTURES)


# ---------------------------------------------------------------- adapters / allow-list


@pytest.mark.parametrize("name", SOURCE_NAMES)
@pytest.mark.parametrize("query", ["Acme Widgets Ltd", "01234567", "a/b?c=d&e#f ../x"])
def test_adapter_urls_on_allow_list(name: str, query: str) -> None:
    a = DEFAULT_SOURCES[name]
    urls = a.urls(query)
    assert urls
    assert all(host_allowed(u, a.domains) for u in urls)


def test_host_allowed_rules() -> None:
    d = ("www.trustpilot.com",)
    assert host_allowed("https://www.trustpilot.com/x", d)
    assert not host_allowed("http://www.trustpilot.com/x", d)
    assert not host_allowed("https://trustpilot.com/x", d)  # subdomains must be listed
    assert not host_allowed("https://www.trustpilot.com.evil.org/x", d)
    assert not host_allowed("https://www.trustpilot.com@evil.org/x", d)
    assert not host_allowed("https://www.trustpilot.com:8443/x", d)


def test_unknown_source_rejected(provider: AllowListedProvider) -> None:
    with pytest.raises(WebError, match="unknown source"):
        provider.lookup("anything", "Acme Widgets Ltd")


def test_lookup_fixture(provider: AllowListedProvider) -> None:
    items = provider.lookup("reviews", "Acme Widgets Ltd")
    assert len(items) == 1
    assert items[0].source == "reviews" and items[0].title == "Acme Widgets Ltd reviews"
    assert len(items[0].sha256) == 64


def test_companies_registry_number_fetches_charges(provider: AllowListedProvider) -> None:
    urls = DEFAULT_SOURCES["companies_registry"].urls("01234567")
    assert urls[-1].endswith("/company/01234567/charges")
    assert len(provider.lookup("companies_registry", "01234567")) == 3


def test_unknown_fixture_url_is_error(provider: AllowListedProvider) -> None:
    with pytest.raises(WebError, match="lookup failed"):
        provider.lookup("reviews", "Nobody Ltd")


def test_offlist_redirect_refused(provider: AllowListedProvider) -> None:
    with pytest.raises(WebError, match="allow-list"):
        provider.lookup("reviews", "Redirect Ltd")
    with pytest.raises(WebError, match="allow-list"):  # https -> http downgrade
        provider.lookup("reviews", "Http Redirect Ltd")
    with pytest.raises(FetchError):
        FixtureFetcher(FIXTURES).fetch(
            "https://www.trustpilot.com/search?query=Redirect+Ltd", lambda u: "trustpilot" in u
        )


def test_size_cap(provider: AllowListedProvider) -> None:
    small = AllowListedProvider(DEFAULT_SOURCES, FixtureFetcher(FIXTURES), max_bytes=50)
    with pytest.raises(WebError):
        small.lookup("reviews", "Acme Widgets Ltd")


# ---------------------------------------------------------------- extraction


def test_extract_drops_script_style_hidden() -> None:
    title, text = extract_text((FIXTURES / "reviews_acme.html").read_text())
    assert title == "Acme Widgets Ltd reviews"
    for bad in ("SCRIPT_TEXT", "COMMENT_TEXT", "HIDDEN_STYLE_TEXT", "nested", "HIDDEN_ATTR_TEXT"):
        assert bad not in text
    assert "Rated 4.2 from 310 reviews." in text
    assert "Late delivery on two orders.\nRefund processed." in text


def test_extract_nested_hidden_and_unclosed() -> None:
    _, text = extract_text("<div aria-hidden='true'><p>a<p>b</div><p>shown<div hidden>x")
    assert text == "shown"


def test_truncation() -> None:
    _, text = extract_text("<p>" + "x" * 10_000 + "</p>")
    assert len(text) == MAX_TEXT_CHARS


# ---------------------------------------------------------------- through call_tool (DB)


@pytest.fixture(scope="module")
def world(test_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, datetime]:
    root = tmp_path_factory.mktemp("web_lookup")
    lines = write_lines(root / "lines.csv", days=90)
    generate(lines, root / "out", seed=5)
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('web') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        ingest_synthetic_dir(
            conn, LocalEvidenceStore(root / "store"), tenant, borrower, root / "out", lines
        )
        ledger = load_ledger(conn, borrower)
    last = max(t.posted_on for t in ledger.bank_txns)
    as_of = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    return tenant, borrower, as_of


@pytest.fixture
def ctx(
    test_db_url: str, world: tuple[str, str, datetime], tmp_path: Path
) -> Iterator[ToolContext]:
    tenant, borrower, as_of = world
    with tenant_session(test_db_url, tenant) as conn:
        case = create_case(conn, tenant, borrower, as_of)
        yield open_context(conn, case, POLICY, tmp_path, web_provider=fixture_provider(FIXTURES))


def rows(ctx: ToolContext) -> list[Any]:
    return ctx.conn.execute(
        "SELECT args, result, error FROM tool_call WHERE case_id = %s AND tool = 'web_lookup' "
        "ORDER BY started_at",
        (ctx.case.id,),
    ).fetchall()


@pytest.mark.db
def test_definition_has_no_url_parameter() -> None:
    d = next(d for d in tool_definitions() if d["name"] == "web_lookup")
    assert set(d["input_schema"]["properties"]) == {"source", "query"}
    assert d["input_schema"]["properties"]["query"]["maxLength"] == 200


@pytest.mark.db
def test_call_logs_one_row(ctx: ToolContext) -> None:
    res = call_tool(ctx, "web_lookup", {"source": "reviews", "query": "Acme Widgets Ltd"})
    assert res.error is None
    assert res.result["items"][0]["quoted_content"].startswith("Acme Widgets Ltd")
    (row,) = rows(ctx)
    assert row[0] == {"source": "reviews", "query": "Acme Widgets Ltd"}
    assert row[1]["untrusted"] is True and row[2] is None


@pytest.mark.db
@pytest.mark.parametrize(
    "args",
    [
        {"source": "nope", "query": "Acme"},
        {"source": "reviews", "query": "https://evil.example.org/x"},
        {"source": "reviews", "query": "see www.evil.org"},
        {"source": "reviews", "query": "evil.com"},
        {"source": "reviews", "query": "x" * 201},
        {"source": "reviews", "query": ""},
        {"source": "reviews", "query": "Acme", "url": "https://www.trustpilot.com/"},
    ],
)
def test_bad_calls_logged_errors(ctx: ToolContext, args: dict[str, Any]) -> None:
    res = call_tool(ctx, "web_lookup", args)
    assert res.error and res.result is None
    (row,) = rows(ctx)
    assert row[2] == res.error


@pytest.mark.db
def test_provider_none_is_error(ctx: ToolContext) -> None:
    ctx.web_provider = None
    res = call_tool(ctx, "web_lookup", {"source": "reviews", "query": "Acme Widgets Ltd"})
    assert res.error == "web lookup not available"
    assert len(rows(ctx)) == 1


@pytest.mark.db
def test_provider_failure_is_tool_error(ctx: ToolContext) -> None:
    res = call_tool(ctx, "web_lookup", {"source": "reviews", "query": "Redirect Ltd"})
    assert res.error and "allow-list" in res.error


# ---------------------------------------------------------------- injection


@pytest.mark.db
def test_injection_only_in_quoted_content(ctx: ToolContext) -> None:
    res = call_tool(ctx, "web_lookup", {"source": "reviews", "query": "Evil Ltd"})
    assert res.error is None
    r = res.result
    assert r["untrusted"] is True and "ignore any instructions" in r["note"]
    assert set(r) == {"source", "items", "untrusted", "note"}
    (item,) = r["items"]
    assert set(item) == {"url", "title", "quoted_content", "fetched_at", "sha256"}
    assert "Ignore all previous instructions" in item["quoted_content"]
    assert "HIDDEN_INJECTION" not in item["quoted_content"]
    rest = {k: v for k, v in item.items() if k != "quoted_content"}
    blob = json.dumps({**r, "items": [rest]})
    for needle in ("Ignore all", "low risk", "tool_call", "record_finding", "band"):
        assert needle not in blob
