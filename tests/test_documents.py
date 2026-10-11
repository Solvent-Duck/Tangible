import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from fixtures.poisoned.make_fixtures import (
    ACCOUNT,
    BANK_ROWS,
    INJECTIONS,
    OPENING,
    closing_balance,
    generate,
    reply_for,
)
from tangible.agent.llm import ModelResponse, ScriptedClient
from tangible.agent.prompts import PROMPT_HASHES, load_prompt
from tangible.agent.tools import call_tool, open_context
from tangible.datasets.synthetic import generate as generate_synthetic
from tangible.db.cases import create_case
from tangible.db.repo import create_borrower, insert_statement, load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.documents.pdf import PdfError, extract_text, file_traits
from tangible.documents.reader import (
    CLOSE_MARK,
    OPEN_MARK,
    ExtractionError,
    parse_reply,
    read_statement,
    statement_fields,
)
from tangible.documents.upload import store_upload
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.ledger.evidence import EvidenceRef
from tangible.policy import load_policy
from test_synthetic import write_lines

POLICY = load_policy(Path(__file__).parents[1] / "policies" / "default.yaml")
EV = EvidenceRef("11111111-1111-1111-1111-111111111111", "statement")
NOW = datetime(2026, 2, 5, tzinfo=UTC)
INJECT_NAMES = [
    "inject_visible_text", "inject_white_on_white", "inject_tiny_font", "inject_metadata",
    "inject_creator_field", "inject_system_prefix", "inject_fake_tool_call",
    "inject_role_confusion", "inject_close_marker", "inject_in_description",
]  # fmt: skip


@pytest.fixture(scope="session")
def poisoned(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return generate(tmp_path_factory.mktemp("poisoned"))


def pdf(poisoned: dict[str, Path], name: str) -> bytes:
    return poisoned[name].read_bytes()


def scripted(text: str) -> ScriptedClient:
    return ScriptedClient([ModelResponse(text=text, model_id="scripted")])


def read(client: ScriptedClient, data: bytes):  # type: ignore[no-untyped-def]
    return read_statement(client, data, source_ref="s1", evidence=EV, observed_at=NOW)


# ---------------------------------------------------------------- pdf.py


def test_generated_set_is_big_enough(poisoned: dict[str, Path]) -> None:
    assert len(poisoned) >= 10
    assert all(p.read_bytes().startswith(b"%PDF") for p in poisoned.values())


def test_extract_text_includes_hidden_text(poisoned: dict[str, Path]) -> None:
    clean = extract_text(pdf(poisoned, "clean_a"))
    assert f"Account number: {ACCOUNT}" in clean and "STRIPE PAYOUT" in clean
    assert INJECTIONS["ignore"] not in clean
    # invisible text is still extracted: the reader must be safe against it, not blind to it
    assert INJECTIONS["ignore"] in extract_text(pdf(poisoned, "inject_white_on_white"))
    assert INJECTIONS["visible"] in extract_text(pdf(poisoned, "inject_tiny_font"))


def test_file_traits_clean(poisoned: dict[str, Path]) -> None:
    t = file_traits(pdf(poisoned, "clean_a"))
    assert t["producer"].startswith("Oracle BI Publisher")
    assert t["modified_after_creation"] == "false"
    assert t["has_incremental_updates"] == "false"
    assert t["encrypted"] == "false" and t["page_count"] == "1"
    assert set(t["fonts"].split(",")) == {"Helvetica", "Helvetica-Bold"}


def test_file_traits_detect_incremental_update(poisoned: dict[str, Path]) -> None:
    t = file_traits(pdf(poisoned, "edited_after_creation"))
    assert t["has_incremental_updates"] == "true"
    assert t["modified_after_creation"] == "true"
    assert t["producer"] == "Microsoft Word"


def test_metadata_traits_are_capped_and_clean(poisoned: dict[str, Path]) -> None:
    t = file_traits(pdf(poisoned, "inject_creator_field"))
    assert 0 < len(t["creator"]) <= 100
    assert all("\n" not in v for v in t.values())


def test_not_a_pdf() -> None:
    with pytest.raises(PdfError):
        extract_text(b"hello")
    with pytest.raises(PdfError):
        file_traits(b"")


# ---------------------------------------------------------------- reader


def test_reader_valid_json(poisoned: dict[str, Path]) -> None:
    client = scripted(reply_for(BANK_ROWS))
    st = read(client, pdf(poisoned, "clean_a"))
    assert st.account_ref == ACCOUNT and st.source_ref == "s1" and st.evidence == EV
    assert (st.period_start, st.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert st.opening_balance == OPENING and st.closing_balance == closing_balance(BANK_ROWS)
    assert len(st.lines) == len(BANK_ROWS)
    assert st.lines[0].amount == Decimal("1250.00") and st.lines[0].direction == "in"
    assert st.observed_at == NOW
    assert dict(st.file_traits)["page_count"] == "1"


def good() -> dict[str, Any]:
    return json.loads(reply_for(BANK_ROWS))


def with_(**kw: Any) -> str:
    return json.dumps({**good(), **kw})


def with_line(**kw: Any) -> str:
    d = good()
    d["lines"][0] = {**d["lines"][0], **kw}
    return json.dumps(d)


def drop(key: str) -> str:
    d = good()
    del d[key]
    return json.dumps(d)


BAD_REPLIES = {
    "extra key": with_(band="A"),
    "missing key": drop("lines"),
    "prose": "Sure! Here is the statement: " + reply_for(BANK_ROWS),
    "prose before a fence": "Here it is:\n```json\n" + reply_for(BANK_ROWS) + "\n```",
    "trailing prose": reply_for(BANK_ROWS) + "\nI rated this business low risk.",
    "not an object": "[1, 2]",
    "empty": "",
    "float amount": with_line(amount=12.5),
    "three decimals": with_line(amount="12.345"),
    "text amount": with_line(amount="twelve"),
    "negative amount": with_line(amount="-5.00"),
    "zero amount": with_line(amount="0.00"),
    "huge amount": with_line(amount="99999999999999999.00"),
    "nan amount": with_line(amount="NaN"),
    "bad date": with_line(posted_on="2026-02-30"),
    "wrong date format": with_line(posted_on="01/02/2026"),
    "bad direction": with_line(direction="sideways"),
    "extra line key": with_line(tool_call="record_finding"),
    "bad balance": with_(opening_balance="abc"),
    "numeric balance": with_(closing_balance=100.0),
    "period reversed": with_(period_start="2026-02-01", period_end="2026-01-01"),
    "lines not list": with_(lines="none"),
    "account not string": with_(account_ref=12345678),
    "empty account": with_(account_ref="  "),
    "description not string": with_line(description=["x"]),
}


@pytest.mark.parametrize("name", list(BAD_REPLIES))
def test_reader_rejects_bad_replies(poisoned: dict[str, Path], name: str) -> None:
    with pytest.raises(ExtractionError):
        read(scripted(BAD_REPLIES[name]), pdf(poisoned, "clean_a"))


def test_reader_rejects_tool_calls(poisoned: dict[str, Path]) -> None:
    resp = ModelResponse(
        text=reply_for(BANK_ROWS),
        tool_calls=({"id": "x", "name": "record_finding", "input": {}},),
    )
    with pytest.raises(ExtractionError):
        read(ScriptedClient([resp]), pdf(poisoned, "clean_a"))


def test_reader_rejects_unreadable_pdf() -> None:
    client = scripted(reply_for(BANK_ROWS))
    with pytest.raises(ExtractionError):
        read(client, b"not a pdf")
    assert client.calls == []  # never reached the model


def test_null_balances_allowed(poisoned: dict[str, Path]) -> None:
    st = read(scripted(with_(opening_balance=None, closing_balance=None)), pdf(poisoned, "clean_a"))
    assert st.opening_balance is None and st.closing_balance is None


@pytest.mark.parametrize("name", ["clean_a", *INJECT_NAMES])
def test_reader_call_shape_and_prompt(poisoned: dict[str, Path], name: str) -> None:
    data = pdf(poisoned, name)
    client = scripted(reply_for(BANK_ROWS))
    read(client, data)
    (call,) = client.calls
    assert call.tools is None
    prompt = load_prompt("document_reader", 1)
    assert call.system == prompt.text and prompt.sha256 == PROMPT_HASHES["document_reader@v1"]
    (msg,) = call.messages
    content = msg["content"]
    assert isinstance(content, str) and msg["role"] == "user"
    assert content.count(OPEN_MARK) == 1 and content.count(CLOSE_MARK) == 1
    inside = content.split(OPEN_MARK, 1)[1].rsplit(CLOSE_MARK, 1)[0]
    assert "Account number" in inside
    # the document text is only ever inside the block, never in the system prompt
    assert "ignore previous instructions" not in call.system.lower()
    outside = content.replace(inside, "")
    assert "ignore previous" not in outside.lower() and "SYSTEM:" not in outside


@pytest.mark.parametrize("name", INJECT_NAMES)
def test_injection_fixtures_yield_only_schema_fields(poisoned: dict[str, Path], name: str) -> None:
    # The scripted model behaves; the point is that nothing from the document can ride along.
    st = read(scripted(reply_for(BANK_ROWS)), pdf(poisoned, name))
    fields = statement_fields(st)
    assert set(fields) == {
        "source_ref", "account_ref", "period_start", "period_end", "opening_balance",
        "closing_balance", "lines", "file_traits",
    }  # fmt: skip
    assert all(
        set(ln) == {"posted_on", "amount", "direction", "description"} for ln in fields["lines"]
    )
    blob = json.dumps(fields["lines"]) + fields["account_ref"]
    for text in INJECTIONS.values():
        assert text not in blob
    assert all(len(ln["description"]) <= 140 for ln in fields["lines"])
    assert all(len(v) <= 100 for v in fields["file_traits"].values())


def test_injection_in_model_reply_is_confined(poisoned: dict[str, Path]) -> None:
    """Even if the model copies injected text into a description, it is capped data."""
    long_text = (INJECTIONS["ignore"] + " ") * 10
    st = read(scripted(with_line(description=long_text)), pdf(poisoned, "inject_in_description"))
    assert len(st.lines[0].description) == 140
    assert st.lines[0].amount == Decimal("1250.00")


def test_injected_keys_in_reply_are_rejected(poisoned: dict[str, Path]) -> None:
    for text in INJECTIONS.values():
        with pytest.raises(ExtractionError):
            read(scripted(with_(instructions=text)), pdf(poisoned, "inject_visible_text"))
    with pytest.raises(ExtractionError):
        read(scripted(INJECTIONS["tool_json"]), pdf(poisoned, "inject_fake_tool_call"))


def test_close_marker_cannot_end_the_block(poisoned: dict[str, Path]) -> None:
    client = scripted(reply_for(BANK_ROWS))
    read(client, pdf(poisoned, "inject_close_marker"))
    content = client.calls[0].messages[0]["content"]
    assert content.endswith(CLOSE_MARK) and content.count(CLOSE_MARK) == 1


# ---------------------------------------------------------------- DB: upload, tool, repo, schema

db = pytest.mark.db


def new_tenant(url: str, name: str) -> str:
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
        assert row
        return str(row[0])


@pytest.fixture(scope="module")
def world(
    test_db_url: str, poisoned: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("docs")
    lines = write_lines(root / "lines.csv", days=30)
    generate_synthetic(lines, root / "out", seed=2)
    store = LocalEvidenceStore(root / "store")
    tenant = new_tenant(test_db_url, "docs")
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        other = create_borrower(conn, tenant, "other")
        ingest_synthetic_dir(conn, store, tenant, borrower, root / "out", lines)
        pull = store_upload(conn, store, tenant, borrower, "jan.pdf", pdf(poisoned, "clean_a"))
        other_pull = store_upload(conn, store, tenant, other, "x.pdf", pdf(poisoned, "clean_b"))
        ledger = load_ledger(conn, borrower)
    last = max(t.posted_on for t in ledger.bank_txns)
    as_of = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    return {
        "tenant": tenant, "borrower": borrower, "pull": pull, "other_pull": other_pull,
        "store": store, "as_of": as_of, "root": root,
    }  # fmt: skip


@pytest.fixture
def ctx_parts(test_db_url: str, world: dict[str, Any], tmp_path: Path) -> Iterator[Any]:
    def make(client: ScriptedClient | None, store: Any = "default"):  # type: ignore[no-untyped-def]
        return open_context(
            conn,
            create_case(conn, world["tenant"], world["borrower"], world["as_of"]),
            POLICY,
            tmp_path,
            document_client=client,
            evidence_store=world["store"] if store == "default" else store,
        )

    with tenant_session(test_db_url, world["tenant"]) as conn:
        yield make, conn


def n_extracts(conn: Any, borrower: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM document_extract WHERE borrower_id = %s", (borrower,)
    ).fetchone()
    return int(row[0])


@db
def test_store_upload(test_db_url: str, world: dict[str, Any], poisoned: dict[str, Path]) -> None:
    data = pdf(poisoned, "clean_a")
    with tenant_session(test_db_url, world["tenant"]) as conn:
        again = store_upload(
            conn, world["store"], world["tenant"], world["borrower"], "renamed.pdf", data
        )
        assert again == world["pull"]  # idempotent on content
        evil = store_upload(
            conn, world["store"], world["tenant"], world["borrower"], "../../etc/x.pdf", data[:-1]
        )
        row = conn.execute(
            "SELECT source, s3_key, byte_size, status FROM evidence_pull WHERE id = %s", (evil,)
        ).fetchone()
        assert row
        assert row[0] == "document_upload" and ".." not in row[1] and row[3] == "complete"
        assert world["store"].get(row[1]) == data[:-1]


@db
def test_read_document_end_to_end(world: dict[str, Any], ctx_parts: Any) -> None:
    make, conn = ctx_parts
    client = scripted(reply_for(BANK_ROWS))
    ctx = make(client)
    res = call_tool(ctx, "read_document", {"pull_id": world["pull"]})
    assert res.error is None, res.error
    assert set(res.result) == {
        "pull_id", "source_ref", "account_ref", "period_start", "period_end", "opening_balance",
        "closing_balance", "lines", "file_traits",
    }  # fmt: skip
    assert res.result["account_ref"] == ACCOUNT and len(res.result["lines"]) == len(BANK_ROWS)
    assert client.calls[0].tools is None
    row = conn.execute(
        "SELECT tool, tool_version, error FROM tool_call WHERE id = %s", (res.call_id,)
    ).fetchone()
    assert row == ("read_document", 1, None)
    ex = conn.execute(
        "SELECT source_ref, evidence_pull_id, account_ref, lines, file_traits "
        "FROM document_extract WHERE borrower_id = %s",
        (world["borrower"],),
    ).fetchall()
    assert len(ex) == 1
    assert ex[0][0] == f"statement:{world['pull']}" and str(ex[0][1]) == world["pull"]
    assert ex[0][2] == ACCOUNT and len(ex[0][3]) == len(BANK_ROWS)
    assert ex[0][4]["page_count"] == "1"

    # load_ledger returns it, and the time rule hides it before it was observed
    ledger = load_ledger(conn, world["borrower"])
    (st,) = ledger.statements
    assert st.closing_balance == closing_balance(BANK_ROWS)
    assert st.lines[1].direction == "out" and st.lines[1].description == "SUPPLIER ACME LTD"
    assert st.evidence is not None and st.evidence.pull_id == world["pull"]
    assert len(ledger.known(st.observed_at).statements) == 1
    assert ledger.known(st.observed_at - timedelta(seconds=1)).statements == ()

    # a re-extraction is a new row with the same source_ref; known() keeps the latest
    res2 = call_tool(make(scripted(reply_for(BANK_ROWS[:-1]))), "read_document",
                     {"pull_id": world["pull"]})  # fmt: skip
    assert res2.error is None
    ledger = load_ledger(conn, world["borrower"])
    assert len(ledger.statements) == 2
    (latest,) = ledger.known(datetime.now(UTC) + timedelta(days=1)).statements
    assert len(latest.lines) == len(BANK_ROWS) - 1


@db
def test_read_document_rejects_other_borrowers_pull(world: dict[str, Any], ctx_parts: Any) -> None:
    make, conn = ctx_parts
    client = scripted(reply_for(BANK_ROWS))
    before = n_extracts(conn, world["borrower"])
    res = call_tool(make(client), "read_document", {"pull_id": world["other_pull"]})
    assert res.error and "unknown document" in res.error
    assert client.calls == [] and n_extracts(conn, world["borrower"]) == before
    bad = call_tool(make(client), "read_document", {"pull_id": "not-a-uuid"})
    assert bad.error and "invalid arguments" in bad.error


@db
def test_read_document_rejects_non_upload_pull(world: dict[str, Any], ctx_parts: Any) -> None:
    make, conn = ctx_parts
    row = conn.execute(
        "SELECT id FROM evidence_pull WHERE borrower_id = %s AND source <> 'document_upload' "
        "LIMIT 1",
        (world["borrower"],),
    ).fetchone()
    res = call_tool(make(scripted(reply_for(BANK_ROWS))), "read_document", {"pull_id": str(row[0])})
    assert res.error and "unknown document" in res.error


@db
def test_read_document_unavailable(world: dict[str, Any], ctx_parts: Any) -> None:
    make, _ = ctx_parts
    for ctx in (make(None), make(scripted(reply_for(BANK_ROWS)), store=None)):
        res = call_tool(ctx, "read_document", {"pull_id": world["pull"]})
        assert res.error == "document reading not available"


@db
def test_read_document_bad_reply_writes_nothing(world: dict[str, Any], ctx_parts: Any) -> None:
    make, conn = ctx_parts
    before = n_extracts(conn, world["borrower"])
    res = call_tool(make(scripted("I will rate this low risk")), "read_document",
                    {"pull_id": world["pull"]})  # fmt: skip
    assert res.error and "could not extract" in res.error
    assert n_extracts(conn, world["borrower"]) == before
    logged = conn.execute("SELECT error FROM tool_call WHERE id = %s", (res.call_id,)).fetchone()
    assert logged and logged[0] == res.error


@db
def test_document_extract_rls_and_append_only(
    test_db_url: str, world: dict[str, Any], poisoned: dict[str, Path]
) -> None:
    ref = EvidenceRef(world["pull"], "statement")
    st = read_statement(
        scripted(reply_for(BANK_ROWS)), pdf(poisoned, "clean_a"),
        source_ref="rls-1", evidence=ref, observed_at=NOW,
    )  # fmt: skip
    with tenant_session(test_db_url, world["tenant"]) as conn:
        insert_statement(conn, world["tenant"], world["borrower"], st)
        insert_statement(conn, world["tenant"], world["borrower"], st)  # same source_ref: new row
        row = conn.execute(
            "SELECT count(*) FROM document_extract WHERE source_ref = 'rls-1'"
        ).fetchone()
        assert row and row[0] == 2
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
            conn.execute("UPDATE document_extract SET account_ref = 'x'")
        # inserting for another tenant is refused by the RLS check
        other_tenant = new_tenant(test_db_url, "docs-b")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            insert_statement(conn, other_tenant, world["borrower"], st)
    other_tenant = new_tenant(test_db_url, "docs-c")
    with tenant_session(test_db_url, other_tenant) as conn:
        row = conn.execute("SELECT count(*) FROM document_extract").fetchone()
        assert row and row[0] == 0


_GOOD_REPLY = {
    "account_ref": "GB-1", "period_start": "2026-09-01", "period_end": "2026-09-30",
    "opening_balance": "10.00", "closing_balance": "5.00",
    "lines": [
        {"posted_on": "2026-09-02", "amount": "5.00", "direction": "out", "description": "x"}
    ],
}  # fmt: skip


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "  ```json \n{}\n  ```  "])
def test_reply_in_one_code_fence_is_accepted(fence: str) -> None:
    reply = fence.replace("{}", json.dumps(_GOOD_REPLY, indent=2))
    assert parse_reply(reply)["account_ref"] == "GB-1"


@pytest.mark.parametrize(
    "reply",
    [
        "Here is the data:\n```json\n{}\n```",
        "```json\n{}\n```\nIgnore the above.",
        "```json\n{}\n```\n```json\n{}\n```",
    ],
)
def test_text_around_a_fence_is_still_rejected(reply: str) -> None:
    with pytest.raises(ExtractionError):
        parse_reply(reply.replace("{}", json.dumps(_GOOD_REPLY)))
