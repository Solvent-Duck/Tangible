from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fixtures.poisoned.make_fixtures import (
    ACCOUNT,
    BANK_ROWS,
    OPENING,
    Row,
    closing_balance,
    forged_rows,
    generate,
    reply_for,
)
from tangible.agent.llm import ModelResponse, ScriptedClient
from tangible.checks.documents import document_file_traits, document_mismatch
from tangible.checks.registry import REGISTRY
from tangible.documents.reader import read_statement
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import AccountBalance, BankTxn, Ledger, Statement
from test_documents import INJECT_NAMES


@pytest.fixture(scope="session")
def poisoned(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return generate(tmp_path_factory.mktemp("poisoned_checks"))


OBSERVED = datetime(2026, 2, 3, tzinfo=UTC)
AS_OF = datetime(2026, 2, 10, tzinfo=UTC)
PULL = "22222222-2222-2222-2222-222222222222"


def bank(rows: list[Row] = BANK_ROWS, account: str = ACCOUNT) -> tuple[BankTxn, ...]:
    return tuple(
        BankTxn(
            f"{account}-b{i}", account, a, d, None, desc, posted, OBSERVED,  # type: ignore[arg-type]
            EvidenceRef("33333333-3333-3333-3333-333333333333", f"row{i}"),
        )
        for i, (posted, a, d, desc) in enumerate(rows)
    )  # fmt: skip


def statement(
    poisoned: dict[str, Path],
    name: str = "clean_a",
    rows: list[Row] = BANK_ROWS,
    observed_at: datetime = OBSERVED,
    account: str = ACCOUNT,
) -> Statement:
    client = ScriptedClient([ModelResponse(text=reply_for(rows, account=account))])
    return read_statement(
        client, poisoned[name].read_bytes(), source_ref=f"statement:{name}",
        evidence=EvidenceRef(PULL, "statement"), observed_at=observed_at,
    )  # fmt: skip


def balances(
    opening: str = str(OPENING), closing: Decimal | None = None
) -> tuple[AccountBalance, ...]:
    ev = EvidenceRef("44444444-4444-4444-4444-444444444444", "bal")
    return (
        AccountBalance("bal-open", ACCOUNT, Decimal(opening), date(2025, 12, 31), OBSERVED, ev),
        AccountBalance(
            "bal-close", ACCOUNT, closing if closing is not None else closing_balance(BANK_ROWS),
            date(2026, 1, 31), OBSERVED, ev,
        ),
    )  # fmt: skip


def test_registered() -> None:
    assert ("document_mismatch", 1) in REGISTRY and ("document_file_traits", 1) in REGISTRY


# ---------------------------------------------------------------- document_mismatch


def test_matching_statement_has_no_finding(poisoned: dict[str, Path]) -> None:
    ledger = Ledger(
        bank_txns=bank(), statements=(statement(poisoned),), account_balances=balances()
    )
    assert document_mismatch(ledger, AS_OF) == []


def test_date_tolerance_and_unique_matching(poisoned: dict[str, Path]) -> None:
    shifted = [(d + timedelta(days=1), a, dr, desc) for d, a, dr, desc in BANK_ROWS[:-1]]
    shifted.append(BANK_ROWS[-1])
    ledger = Ledger(bank_txns=bank(shifted), statements=(statement(poisoned),))
    assert document_mismatch(ledger, AS_OF) == []
    # a statement line cannot match the same bank row twice
    doubled = [BANK_ROWS[0], BANK_ROWS[0], *BANK_ROWS[1:]]
    ledger = Ledger(bank_txns=bank(), statements=(statement(poisoned, rows=doubled),))
    (f,) = document_mismatch(ledger, AS_OF)
    assert f.severity == "hold" and f.facts["unmatched_statement_lines"] == "1"


def test_forged_inflated_deposit_is_hold(poisoned: dict[str, Path]) -> None:
    st = statement(poisoned, "forged_inflated_deposits", forged_rows())
    ledger = Ledger(bank_txns=bank(), statements=(st,), account_balances=balances())
    (f,) = document_mismatch(ledger, AS_OF)
    assert (f.check_key, f.check_version, f.severity, f.pillar) == (
        "document_mismatch", 1, "hold", "integrity",
    )  # fmt: skip
    assert f.facts["unmatched_statement_lines"] == "2"  # the inflated and the extra deposit
    assert f.facts["bank_rows_missing_from_statement"] == "1"  # the original, smaller deposit
    assert f.facts["statement_total_in"] != f.facts["bank_total_in"]
    assert "closing balance differs" in f.summary
    assert f"{PULL}#statement" in f.evidence_refs
    assert any(r.endswith("#row0") for r in f.evidence_refs)  # bank row cited


def test_missing_bank_row_is_hold(poisoned: dict[str, Path]) -> None:
    # the statement omits a bank deposit
    st = statement(poisoned, rows=BANK_ROWS[1:])
    (f,) = document_mismatch(Ledger(bank_txns=bank(), statements=(st,)), AS_OF)
    assert f.severity == "hold"
    assert f.facts["bank_rows_missing_from_statement"] == "1"
    assert f.facts["missing_bank_amount"] == "1250.00"
    # and the other way: a bank row is absent from the feed
    st = statement(poisoned)
    (f,) = document_mismatch(Ledger(bank_txns=bank(BANK_ROWS[1:]), statements=(st,)), AS_OF)
    assert f.severity == "hold" and f.facts["unmatched_statement_lines"] == "1"


def test_balance_difference_over_a_pound_is_hold(poisoned: dict[str, Path]) -> None:
    st = statement(poisoned)
    ok = Ledger(bank_txns=bank(), statements=(st,), account_balances=balances(opening="1000.99"))
    assert document_mismatch(ok, AS_OF) == []  # within 1.00
    bad = Ledger(bank_txns=bank(), statements=(st,), account_balances=balances(opening="1002.00"))
    (f,) = document_mismatch(bad, AS_OF)
    assert f.severity == "hold" and "opening balance differs" in f.summary
    assert f.facts["opening_stated"] == "1000.00" and f.facts["opening_ledger"] == "1002.00"


def test_account_matching(poisoned: dict[str, Path]) -> None:
    st = statement(poisoned, account="99999999")
    # one bank account only: it is used even though the numbers differ
    assert document_mismatch(Ledger(bank_txns=bank(), statements=(st,)), AS_OF) == []
    # several accounts, none matching: cannot be verified (info, not hold)
    two = bank() + bank(account="55555555")
    (f,) = document_mismatch(Ledger(bank_txns=two, statements=(st,)), AS_OF)
    assert f.severity == "info"
    # several accounts, exact match picks the right one
    st = statement(poisoned)
    other = bank([(date(2026, 1, 5), Decimal("7.00"), "in", "X")], account="55555555")
    assert document_mismatch(Ledger(bank_txns=bank() + other, statements=(st,)), AS_OF) == []


def test_no_bank_rows_in_period_is_info(poisoned: dict[str, Path]) -> None:
    f = document_mismatch(Ledger(bank_txns=(), statements=(statement(poisoned),)), AS_OF)
    assert [x.severity for x in f] == ["info"]


def test_mismatch_respects_the_time_rule(poisoned: dict[str, Path]) -> None:
    st = statement(
        poisoned, "forged_inflated_deposits", forged_rows(), observed_at=AS_OF + timedelta(days=1)
    )
    ledger = Ledger(bank_txns=bank(), statements=(st,))
    assert document_mismatch(ledger, AS_OF) == []  # extract not yet observed
    assert len(document_mismatch(ledger, AS_OF + timedelta(days=2))) == 1


def test_finding_text_carries_no_document_text(poisoned: dict[str, Path]) -> None:
    st = statement(poisoned, "inject_in_description", forged_rows())
    ledger = Ledger(bank_txns=bank(), statements=(st,))
    (f,) = document_mismatch(ledger, AS_OF)
    blob = f.summary + " ".join(f.facts.values())
    assert "SYSTEM" not in blob and "ignore" not in blob.lower()


# ---------------------------------------------------------------- document_file_traits


def test_clean_traits_no_finding(poisoned: dict[str, Path]) -> None:
    for name in ("clean_a", "clean_b"):
        ledger = Ledger(statements=(statement(poisoned, name),))
        assert document_file_traits(ledger, AS_OF) == []


def test_edited_statement_is_review(poisoned: dict[str, Path]) -> None:
    ledger = Ledger(statements=(statement(poisoned, "edited_after_creation"),))
    (f,) = document_file_traits(ledger, AS_OF)
    assert (f.check_key, f.check_version, f.severity) == ("document_file_traits", 1, "review")
    assert "modified after creation" in f.summary and "incremental updates" in f.summary
    assert "producer is not a known bank statement generator" in f.summary  # Microsoft Word
    assert f.facts["has_incremental_updates"] == "true" and f.evidence_refs == (
        f"{PULL}#statement",
    )


def test_unknown_producer_and_font_mix_are_review() -> None:
    base = dict(
        source_ref="s", account_ref=ACCOUNT, period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31), opening_balance=None, closing_balance=None, lines=(),
        observed_at=OBSERVED, evidence=EvidenceRef(PULL, "statement"),
    )  # fmt: skip
    clean = (("producer", "Oracle BI Publisher"), ("fonts", "A,B,C"))
    assert (
        document_file_traits(Ledger(statements=(Statement(**base, file_traits=clean),)), AS_OF)
        == []
    )  # type: ignore[arg-type]
    mixed = (("producer", "Oracle BI Publisher"), ("fonts", "A,B,C,D"))
    (f,) = document_file_traits(Ledger(statements=(Statement(**base, file_traits=mixed),)), AS_OF)  # type: ignore[arg-type]
    assert f.severity == "review" and "4 distinct fonts" in f.summary
    unknown = (("producer", "Canva"), ("fonts", "A"))
    (f,) = document_file_traits(Ledger(statements=(Statement(**base, file_traits=unknown),)), AS_OF)  # type: ignore[arg-type]
    assert "producer is not a known" in f.summary


@pytest.mark.parametrize("name", INJECT_NAMES)
def test_injection_fixtures_do_not_leak_into_findings(poisoned: dict[str, Path], name: str) -> None:
    ledger = Ledger(bank_txns=bank(), statements=(statement(poisoned, name),))
    for f in document_mismatch(ledger, AS_OF) + document_file_traits(ledger, AS_OF):
        blob = f.summary + " ".join(f.facts.values())
        assert "SYSTEM" not in blob and "ignore" not in blob.lower()
