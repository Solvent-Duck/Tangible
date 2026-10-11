"""Document checks: borrower-supplied documents vs the ledger (documents are untrusted data).

Summaries and facts contain only numbers and our own identifiers, never text copied from a
document, so nothing a borrower writes reaches a finding.
"""

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from tangible import numfmt
from tangible.checks.registry import FindingDraft, check
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import Ledger, Statement

DATE_TOLERANCE_DAYS: Final = 1  # a statement line matches a bank row within this many days
BALANCE_TOLERANCE: Final = Decimal(1)  # currency units, opening/closing vs account_balance
MAX_FONT_FAMILIES: Final = 3  # distinct base fonts beyond this is unlike a bank's template
MAX_EVIDENCE: Final = 50

# Lower-case substrings of the PDF Producer/Creator that real bank statements commonly carry
# (statement-generation engines, not word processors). A starting guess, to be tuned on real
# statements; a producer outside this list (Word, Photoshop, Canva, online editors, "pypdf")
# is a review signal, not proof of forgery.
KNOWN_STATEMENT_PRODUCERS: Final = (
    "oracle bi publisher",
    "adobe pdf library",
    "itext",
    "apache fop",
    "opentext",
    "exstream",
    "quadient",
    "crystal reports",
    "jasperreports",
    "openpdf",
)


def _norm(s: str) -> str:
    return "".join(s.upper().split())


def _refs(evidence: list[EvidenceRef | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(e) for e in evidence if e is not None))[:MAX_EVIDENCE]


def _bank_account(st: Statement, accounts: set[str]) -> str | None:
    """The bank account a statement is about: exact (whitespace/case-insensitive) match on
    account_ref; failing that, the only bank account if there is exactly one."""
    for a in sorted(accounts):
        if _norm(a) == _norm(st.account_ref):
            return a
    if len(accounts) == 1:
        return next(iter(accounts))
    return None


def _balance_on(k: Ledger, account: str, on: date) -> tuple[Decimal, EvidenceRef | None] | None:
    cands = [b for b in k.account_balances if b.account_ref == account and b.as_of == on]
    if not cands:
        return None
    b = max(cands, key=lambda x: (x.observed_at, x.source_ref))
    return b.balance, b.evidence


@check("document_mismatch", 1)
def document_mismatch(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Each statement vs the bank feed for the same account and dates -> hold on any difference.

    Account match: statement account_ref equals a bank account_ref (ignoring case/whitespace);
    if none does and the feed has exactly one account, that account is used. Lines match
    one-to-one on direction, exact amount and date within +/-1 day. A statement line with no
    bank match, a bank row inside [period_start, period_end] absent from the statement, or an
    opening/closing balance differing by more than 1.00 from the end-of-day account_balance
    on the day before period_start / on period_end (skipped when no such row) is a hold.
    If the account cannot be matched, or the feed has no rows for it in the period, the
    statement cannot be verified: info.
    """
    return _document_mismatch(ledger, as_of, 1, lambda x: f"{x:.2f}")


@check("document_mismatch", 2)
def document_mismatch_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check, with the balance tolerance written in dollars (check set cs-7)."""
    return _document_mismatch(ledger, as_of, 2, numfmt.usd)


def _document_mismatch(
    ledger: Ledger, as_of: datetime, version: int, money: Callable[[Decimal], str]
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    accounts = {t.account_ref for t in k.bank_txns}
    out: list[FindingDraft] = []
    for st in k.statements:
        account = _bank_account(st, accounts)
        in_period = (
            []
            if account is None
            else [
                t
                for t in k.bank_txns
                if t.account_ref == account and st.period_start <= t.posted_on <= st.period_end
            ]
        )
        if account is None or not in_period:
            why = "no bank account matches" if account is None else "no bank rows in the period"
            out.append(
                FindingDraft(
                    "document_mismatch", version, "info", "integrity",
                    f"Statement {st.source_ref} could not be checked: {why}.",
                    _refs([st.evidence]),
                    facts={"statement": st.source_ref, "reason": why},
                )
            )  # fmt: skip
            continue

        near = [
            t
            for t in k.bank_txns
            if t.account_ref == account
            and st.period_start - timedelta(days=DATE_TOLERANCE_DAYS)
            <= t.posted_on
            <= st.period_end + timedelta(days=DATE_TOLERANCE_DAYS)
        ]
        used: set[int] = set()
        unmatched_lines = []
        for ln in sorted(st.lines, key=lambda x: (x.posted_on, x.amount, x.direction)):
            best: tuple[int, int] | None = None  # (day distance, index)
            for i, t in enumerate(near):
                if i in used or t.direction != ln.direction or t.amount != ln.amount:
                    continue
                dist = abs((t.posted_on - ln.posted_on).days)
                if dist <= DATE_TOLERANCE_DAYS and (best is None or (dist, i) < best):
                    best = (dist, i)
            if best is None:
                unmatched_lines.append(ln)
            else:
                used.add(best[1])
        missing_rows = [
            t
            for i, t in enumerate(near)
            if i not in used and st.period_start <= t.posted_on <= st.period_end
        ]

        def total(amounts: Sequence[tuple[str, Decimal]], d: str) -> Decimal:
            return sum((a for dd, a in amounts if dd == d), Decimal(0))

        s_amts = [(ln.direction, ln.amount) for ln in st.lines]
        b_amts = [(t.direction, t.amount) for t in in_period]
        facts = {
            "statement": st.source_ref,
            "bank_account": account,
            "period": f"{st.period_start}..{st.period_end}",
            "statement_lines": str(len(st.lines)),
            "bank_rows": str(len(in_period)),
            "unmatched_statement_lines": str(len(unmatched_lines)),
            "unmatched_statement_amount": str(sum((x.amount for x in unmatched_lines), Decimal(0))),
            "bank_rows_missing_from_statement": str(len(missing_rows)),
            "missing_bank_amount": str(sum((t.amount for t in missing_rows), Decimal(0))),
            "statement_total_in": str(total(s_amts, "in")),
            "bank_total_in": str(total(b_amts, "in")),
            "statement_total_out": str(total(s_amts, "out")),
            "bank_total_out": str(total(b_amts, "out")),
        }  # fmt: skip
        evidence: list[EvidenceRef | None] = [st.evidence]
        evidence += [t.evidence for t in missing_rows]
        problems: list[str] = []
        if unmatched_lines:
            problems.append(f"{len(unmatched_lines)} statement line(s) have no bank match")
        if missing_rows:
            problems.append(f"{len(missing_rows)} bank row(s) are missing from the statement")
        for label, stated, on in (
            # Balances are end-of-day: the opening balance is the day before the period starts.
            ("opening", st.opening_balance, st.period_start - timedelta(days=1)),
            ("closing", st.closing_balance, st.period_end),
        ):
            bal = _balance_on(k, account, on) if stated is not None else None
            if stated is None or bal is None:
                continue
            facts[f"{label}_stated"] = str(stated)
            facts[f"{label}_ledger"] = str(bal[0])
            if abs(stated - bal[0]) > BALANCE_TOLERANCE:
                problems.append(
                    f"{label} balance differs from the ledger by more than "
                    f"{money(BALANCE_TOLERANCE)}"
                )
                evidence.append(bal[1])
        if not problems:
            continue
        evidence += [t.evidence for t in in_period if t not in missing_rows]
        out.append(
            FindingDraft(
                "document_mismatch", version, "hold", "integrity",
                f"Statement {st.source_ref} disagrees with the bank feed: " + "; ".join(problems)
                + ".",
                _refs(evidence), facts=facts,
            )
        )  # fmt: skip
    return out


@check("document_file_traits", 1)
def document_file_traits(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """File-level tampering signs in an uploaded statement -> review.

    Signals: modified after creation, incremental updates, a Producer (Creator if none) not in
    KNOWN_STATEMENT_PRODUCERS, or more than MAX_FONT_FAMILIES distinct base fonts.
    """
    k = ledger.known(as_of)
    out: list[FindingDraft] = []
    for st in k.statements:
        t = dict(st.file_traits)
        signs: list[str] = []
        if t.get("modified_after_creation") == "true":
            signs.append("modified after creation")
        if t.get("has_incremental_updates") == "true":
            signs.append("has incremental updates")
        maker = (t.get("producer") or t.get("creator") or "").lower()
        known_producer = any(p in maker for p in KNOWN_STATEMENT_PRODUCERS)
        if not known_producer:
            signs.append("producer is not a known bank statement generator")
        fonts = [f for f in t.get("fonts", "").split(",") if f]
        if len(fonts) > MAX_FONT_FAMILIES:
            signs.append(f"{len(fonts)} distinct fonts")
        if not signs:
            continue
        out.append(
            FindingDraft(
                "document_file_traits", 1, "review", "integrity",
                f"Statement {st.source_ref} file traits: " + "; ".join(signs) + ".",
                _refs([st.evidence]),
                facts={
                    "statement": st.source_ref,
                    "modified_after_creation": t.get("modified_after_creation", ""),
                    "has_incremental_updates": t.get("has_incremental_updates", ""),
                    "known_producer": "true" if known_producer else "false",
                    "font_count": str(len(fonts)),
                },
            )
        )  # fmt: skip
    return out
