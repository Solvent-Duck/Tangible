"""The workbench passes the configured web provider to the investigation loop's tools."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from tangible.agent.llm import ModelResponse, ScriptedClient
from tangible.db.session import tenant_session
from tangible.web.fixtures import DEMO_FIXTURES_DIR, demo_provider
from tangible.workbench.settings import Settings
from test_assessment import World, datasets  # noqa: F401
from test_assessment import clean as clean_world  # noqa: F401
from test_workbench_api import Stack, make_stack


def lookup_call(source: str, query: str) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=(
            {"id": "w1", "name": "web_lookup", "input": {"source": source, "query": query}},
        ),
        model_id="scripted-1",
    )


def done() -> ModelResponse:
    return ModelResponse(text="Done.", model_id="scripted-1")


def run_with_web(stack: Stack, **overrides: Any) -> tuple[str, list[Any]]:
    """Plan and approve (NullClient), then investigate with a ScriptedClient that calls
    web_lookup once. Returns (case_id, the web_lookup tool_call rows)."""
    case_id = stack.new_case()
    stack.call("POST", f"/api/cases/{case_id}/plan", "ana", 201)
    stack.call("POST", f"/api/cases/{case_id}/plan/approve", "ana", 200)
    client = ScriptedClient([lookup_call("reviews", "Kestrel Gift Co Ltd"), done()])
    app = stack.http.app
    app.state.settings = dataclasses.replace(  # type: ignore[attr-defined]
        app.state.settings,  # type: ignore[attr-defined]
        client_factory=lambda: client,
        **overrides,
    )
    stack.call("POST", f"/api/cases/{case_id}/investigate", "ana", 202)
    assert stack.case(case_id)["status"] == "review"
    with tenant_session(stack.w.url, stack.w.tenant) as conn:
        rows = conn.execute(
            "SELECT args, result, error FROM tool_call WHERE case_id = %s AND tool = 'web_lookup'",
            (case_id,),
        ).fetchall()
    return case_id, rows


@pytest.fixture
def stack(clean_world: World, tmp_path: Path) -> Stack:  # noqa: F811
    return make_stack(clean_world, tmp_path)


@pytest.mark.db
def test_investigation_uses_fixture_provider(stack: Stack) -> None:
    _, rows = run_with_web(stack, web_provider="fixtures")
    (row,) = rows
    assert row[2] is None
    assert row[1]["untrusted"] is True
    (item,) = row[1]["items"]
    assert "Payouts have been late" in item["quoted_content"]


@pytest.mark.db
def test_investigation_with_provider_none_reports_not_available(stack: Stack) -> None:
    _, rows = run_with_web(stack)  # default: web_provider == "none"
    (row,) = rows
    assert row[1] is None and row[2] == "web lookup not available"


@pytest.mark.db
def test_investigation_uses_custom_fixture_index(stack: Stack, tmp_path: Path) -> None:
    (tmp_path / "x.html").write_text("<title>Custom</title><p>Custom page for Kestrel.</p>")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {"https://www.trustpilot.com/search?query=Kestrel+Gift+Co+Ltd": {"file": "x.html"}}
        )
    )
    _, rows = run_with_web(stack, web_provider="fixtures", web_fixtures=index)
    assert "Custom page for Kestrel." in rows[0][1]["items"][0]["quoted_content"]


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TANGIBLE_WEB_PROVIDER", raising=False)
    monkeypatch.delenv("TANGIBLE_WEB_FIXTURES", raising=False)
    s = Settings.from_env()
    assert s.web_provider == "none" and s.web_fixtures is None
    assert s.build_web_provider() is None

    monkeypatch.setenv("TANGIBLE_WEB_PROVIDER", "fixtures")
    s = Settings.from_env()
    assert s.web_fixtures is None
    assert s.build_web_provider() is not None
    assert (DEMO_FIXTURES_DIR / "index.json").is_file()

    index = DEMO_FIXTURES_DIR / "index.json"
    monkeypatch.setenv("TANGIBLE_WEB_FIXTURES", str(index))
    assert Settings.from_env().web_fixtures == index

    monkeypatch.setenv("TANGIBLE_WEB_FIXTURES", str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError):
        Settings.from_env()

    monkeypatch.delenv("TANGIBLE_WEB_FIXTURES")
    monkeypatch.setenv("TANGIBLE_WEB_PROVIDER", "live")
    with pytest.raises(ValueError, match="TANGIBLE_WEB_PROVIDER"):
        Settings.from_env()


def test_demo_provider_default_is_packaged_set() -> None:
    assert demo_provider().fetcher.dir == DEMO_FIXTURES_DIR  # type: ignore[attr-defined]
