"""Tampering checks: round-number, duplicated, back-dated or revised-after-the-fact rows."""

import calendar
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from tangible import numfmt
from tangible.checks.registry import FindingDraft, check
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import BankTxn, Invoice, Ledger, Order

BACKDATE_DAYS: Final = 35  # observed_at this far past the business date (or period end)
WINDOW_DAYS: Final = 90
ROUND_STEP: Final = Decimal(100)
ROUND_MAX_SHARE: Final = Decimal("0.20")
ROUND_MIN_COUNT: Final = 5
SELF_TRANSFER_MAX_SHARE: Final = Decimal("0.02")
OWNER_OUTFLOW_WORDS: Final = ("DRAWINGS", "DIVIDEND", "DIRECTOR")
MIN_IDENTITY_LEN: Final = 3  # shorter owner names would match too much by substring
RECURRING_WINDOW_DAYS: Final = 30
RECURRING_MIN_DEBITS: Final = 10
RECURRING_MIN_SAME_AMOUNT_SHARE: Final = Decimal("0.80")
LENDER_WORDS: Final = ("CAPITAL", "LENDING", "LOAN", "FINANCE", "FUNDING", "ADVANCE")
MAX_EVIDENCE: Final = 50

Text = Callable[[Decimal], str]
# How v1 wrote figures (frozen: replay compares summaries). The cs-7 versions use numfmt.
_RAW: Final[Text] = str
_PCT1: Final[Text] = lambda x: f"{x:.1%}"  # noqa: E731
_PCT0: Final[Text] = lambda x: f"{x:.0%}"  # noqa: E731


def _refs(evidence: Iterable[EvidenceRef | None]) -> tuple[str, ...]:
    unique = dict.fromkeys(str(e) for e in evidence if e is not None)  # keeps order
    return tuple(unique)[:MAX_EVIDENCE]


def _norm(s: str | None) -> str:
    return " ".join((s or "").upper().split())


def _inflows_90d(k: Ledger, as_of: datetime) -> list[BankTxn]:
    today = as_of.date()
    start = today - timedelta(days=WINDOW_DAYS)
    return [t for t in k.bank_txns if t.direction == "in" and start < t.posted_on <= today]


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _invoice_date(i: Invoice) -> date:
    # A paid revision is legitimately observed after payment, not after issue.
    return i.paid_on or i.issued_on


@check("backdated_edits", 1)
def backdated_edits(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """(a) Rows observed more than 35 days after their business date; (b) revisions that change
    an order or invoice amount after its month closed more than 35 days earlier."""
    k = ledger.known(as_of)

    late: list[tuple[int, str, EvidenceRef | None]] = []  # (lag days, source_ref, evidence)
    for o in k.orders:
        lag = (o.observed_at.date() - o.occurred_at.date()).days
        if lag > BACKDATE_DAYS:
            late.append((lag, o.source_ref, o.evidence))
    for t in k.bank_txns:
        lag = (t.observed_at.date() - t.posted_on).days
        if lag > BACKDATE_DAYS:
            late.append((lag, t.source_ref, t.evidence))
    for i in k.invoices:
        lag = (i.observed_at.date() - _invoice_date(i)).days
        if lag > BACKDATE_DAYS:
            late.append((lag, i.source_ref, i.evidence))
    late.sort(key=lambda r: (-r[0], r[1]))

    # Revisions need every version observed by as_of, not just the latest, so read the raw
    # tables but still cut on observed_at (the time rule).
    edits: list[tuple[int, str, EvidenceRef | None]] = []
    revisions: dict[tuple[str, str], list[Order | Invoice]] = defaultdict(list)
    for o in ledger.orders:
        if o.observed_at <= as_of:
            revisions[("order", o.source_ref)].append(o)
    for i in ledger.invoices:
        if i.observed_at <= as_of:
            revisions[("invoice", i.source_ref)].append(i)
    for (kind, ref), versions in sorted(revisions.items()):
        versions.sort(key=lambda r: r.observed_at)
        for prev, cur in zip(versions, versions[1:], strict=False):
            if isinstance(prev, Order) and isinstance(cur, Order):
                changed = (prev.gross, prev.discounts) != (cur.gross, cur.discounts)
                biz = prev.occurred_at.date()
            elif isinstance(prev, Invoice) and isinstance(cur, Invoice):
                changed = prev.amount != cur.amount
                biz = prev.issued_on
            else:
                continue
            closed_for = (cur.observed_at.date() - _month_end(biz)).days
            if changed and closed_for > BACKDATE_DAYS:
                edits.append((closed_for, f"{kind}:{ref}", cur.evidence))
    edits.sort(key=lambda r: (-r[0], r[1]))

    if not late and not edits:
        return []
    parts = []
    if late:
        parts.append(
            f"{len(late)} row(s) were first seen more than {BACKDATE_DAYS} days after their "
            f"business date (worst: {late[0][0]} days, {late[0][1]})"
        )
    if edits:
        parts.append(
            f"{len(edits)} revision(s) changed an order or invoice amount more than "
            f"{BACKDATE_DAYS} days after its month closed (worst: {edits[0][0]} days, "
            f"{edits[0][1]})"
        )
    return [
        FindingDraft(
            check_key="backdated_edits",
            check_version=1,
            severity="review",
            pillar="integrity",
            summary="; ".join(parts) + ".",
            evidence_refs=_refs([e for _, _, e in edits] + [e for _, _, e in late]),
            facts={
                "late_observed_rows": str(len(late)),
                "max_lag_days": str(late[0][0]) if late else "0",
                "closed_period_edits": str(len(edits)),
                "max_days_after_close": str(edits[0][0]) if edits else "0",
                "limit_days": str(BACKDATE_DAYS),
            },
        )
    ]


@check("round_number_pattern", 1)
def round_number_pattern(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """More than 20% of the last 90 days of bank inflows (and at least 5) are multiples of 100."""
    return _round_number_pattern(ledger, as_of, 1, _RAW, _PCT1, _PCT0)


@check("round_number_pattern", 2)
def round_number_pattern_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check, written in dollars with shares to two decimals (check set cs-7)."""
    return _round_number_pattern(ledger, as_of, 2, numfmt.usd, numfmt.pct, numfmt.pct)


def _round_number_pattern(
    ledger: Ledger, as_of: datetime, version: int, money: Text, share_text: Text, limit_text: Text
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    inflows = _inflows_90d(k, as_of)
    rnd = [t for t in inflows if t.amount > 0 and t.amount % ROUND_STEP == 0]
    if not inflows or len(rnd) < ROUND_MIN_COUNT:
        return []
    share = Decimal(len(rnd)) / len(inflows)
    if share <= ROUND_MAX_SHARE:
        return []
    rnd.sort(key=lambda t: (-t.amount, t.source_ref))
    return [
        FindingDraft(
            check_key="round_number_pattern",
            check_version=version,
            severity="info",
            pillar="integrity",
            summary=(
                f"{len(rnd)} of {len(inflows)} bank inflows in the last {WINDOW_DAYS} days "
                f"({share_text(share)}) are exact multiples of {money(ROUND_STEP)}; the limit is "
                f"{limit_text(ROUND_MAX_SHARE)}."
            ),
            evidence_refs=_refs(t.evidence for t in rnd),
            facts={
                "round_count": str(len(rnd)),
                "inflow_count": str(len(inflows)),
                "round_share": str(share.quantize(Decimal("0.0001"))),
                "max_share": str(ROUND_MAX_SHARE),
                "round_total": str(sum((t.amount for t in rnd), Decimal(0))),
            },
        )
    ]


@check("self_transfers_as_revenue", 1)
def self_transfers_as_revenue(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """Inflows from the owner or the borrower's own accounts above 2% of the last 90 days of
    inflows. Identities: the application's owner and own-account names (substring match), plus,
    always, any counterparty that received an outflow described as drawings, dividend or
    director payment anywhere in the known data (exact match)."""
    return _self_transfers_as_revenue(ledger, as_of, 1, _RAW, _PCT1, _PCT0)


@check("self_transfers_as_revenue", 2)
def self_transfers_as_revenue_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check, written in dollars with shares to two decimals (check set cs-7)."""
    return _self_transfers_as_revenue(ledger, as_of, 2, numfmt.usd, numfmt.pct, numfmt.pct)


def _self_transfers_as_revenue(
    ledger: Ledger, as_of: datetime, version: int, money: Text, share_text: Text, limit_text: Text
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    app = k.application
    names = {
        n
        for n in (_norm(x) for x in (app.owner_names + app.own_account_names if app else ()))
        if len(n) >= MIN_IDENTITY_LEN
    }
    drawn = {
        _norm(t.counterparty)
        for t in k.bank_txns
        if t.direction == "out"
        and t.counterparty
        and any(w in _norm(t.description) for w in OWNER_OUTFLOW_WORDS)
    }
    inflows = _inflows_90d(k, as_of)
    total = sum((t.amount for t in inflows), Decimal(0))
    if total <= 0:
        return []

    def is_self(t: BankTxn) -> bool:
        cp = _norm(t.counterparty)
        return bool(cp) and (cp in drawn or any(n in cp for n in names))

    own = [t for t in inflows if is_self(t)]
    amount = sum((t.amount for t in own), Decimal(0))
    share = amount / total
    if not own or share <= SELF_TRANSFER_MAX_SHARE:
        return []
    own.sort(key=lambda t: (-t.amount, t.source_ref))
    who = sorted({_norm(t.counterparty) for t in own})
    return [
        FindingDraft(
            check_key="self_transfers_as_revenue",
            check_version=version,
            severity="review",
            pillar="integrity",
            summary=(
                f"{len(own)} bank inflow(s) worth {money(amount)} of {money(total)} over the last "
                f"{WINDOW_DAYS} days ({share_text(share)}) came from the owner's or the "
                f"borrower's own accounts ({', '.join(who)}); the limit is "
                f"{limit_text(SELF_TRANSFER_MAX_SHARE)}."
            ),
            evidence_refs=_refs(t.evidence for t in own),
            facts={
                "self_inflows": str(amount),
                "total_inflows": str(total),
                "self_share": str(share.quantize(Decimal("0.0001"))),
                "max_share": str(SELF_TRANSFER_MAX_SHARE),
                "counterparties": ",".join(who),
            },
        )
    ]


@check("unexplained_recurring_debit", 1)
def unexplained_recurring_debit(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """An outflow counterparty with >= 10 debits in the last 30 days, >= 80% for the same
    amount, and no payable invoice to it in the known data."""
    return _unexplained_recurring_debit(ledger, as_of, 1, _RAW)


@check("unexplained_recurring_debit", 2)
def unexplained_recurring_debit_v2(ledger: Ledger, as_of: datetime) -> list[FindingDraft]:
    """v1's check, written in dollars (check set cs-7)."""
    return _unexplained_recurring_debit(ledger, as_of, 2, numfmt.usd)


def _unexplained_recurring_debit(
    ledger: Ledger, as_of: datetime, version: int, money: Text
) -> list[FindingDraft]:
    k = ledger.known(as_of)
    today = as_of.date()
    start = today - timedelta(days=RECURRING_WINDOW_DAYS)
    payees = {_norm(i.counterparty) for i in k.invoices if i.kind == "payable" and i.counterparty}
    by_cp: dict[str, list[BankTxn]] = defaultdict(list)
    for t in k.bank_txns:
        if t.direction == "out" and t.counterparty and start < t.posted_on <= today:
            by_cp[_norm(t.counterparty)].append(t)
    drafts: list[FindingDraft] = []
    for cp, debits in sorted(by_cp.items()):
        if len(debits) < RECURRING_MIN_DEBITS or cp in payees:
            continue
        amount, n_same = Counter(t.amount for t in debits).most_common(1)[0]
        share = Decimal(n_same) / len(debits)
        if share < RECURRING_MIN_SAME_AMOUNT_SHARE:
            continue
        lender = any(w in cp for w in LENDER_WORDS)
        total = sum((t.amount for t in debits), Decimal(0))
        facts = {
            "counterparty": cp,
            "debit_count": str(len(debits)),
            "common_amount": str(amount),
            "same_amount_share": str(share.quantize(Decimal("0.0001"))),
            "total_debited": str(total),
        }
        if lender:
            facts["lender_like_name"] = "true"
        debits.sort(key=lambda t: (t.posted_on, t.source_ref), reverse=True)
        drafts.append(
            FindingDraft(
                check_key="unexplained_recurring_debit",
                check_version=version,
                severity="review",
                pillar="integrity",
                summary=(
                    f"{len(debits)} debits to {cp} in the last {RECURRING_WINDOW_DAYS} days "
                    f"totalling {money(total)}, {n_same} of them exactly {money(amount)}, with no "
                    f"payable invoice to this counterparty."
                    + (" The name looks like a lender." if lender else "")
                ),
                evidence_refs=_refs(t.evidence for t in debits),
                facts=facts,
            )
        )
    return drafts
