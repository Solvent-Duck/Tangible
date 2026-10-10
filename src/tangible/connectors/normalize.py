"""Provider records -> ledger rows.

Every row gets observed_at = the pull's fetched_at (when we learned it) and the locator
`record=<provider id>`, which `evidence.resolve` finds inside the stored raw response.

Revisions: a record seen again in a later pull becomes a NEW row with the same source_ref and a
later observed_at; `Ledger.known` then picks the latest as of any date. Nothing is updated.
Consequence: re-pulling identical data inserts duplicate-looking rows. They are harmless (same
source_ref, so the time rule collapses them) and differ in observed_at and evidence pull. Within
one pull, a source_ref is emitted once (the last record wins) so ordering never depends on ties.

Plaid specifics:
- amounts: Plaid reports outflows as positive; we store `direction` ('out' for amount > 0,
  otherwise 'in') and a positive `amount`.
- a `modified` transaction is a new bank_txn row with the same source_ref.
- a `removed` transaction is a new row, same source_ref, amount 0, description
  "REMOVED BY SOURCE" (the removal payload has no amount or date, so posted_on is the fetch
  date, direction 'out', counterparty none). The earlier rows stay; the time rule hides them.
- pending transactions are skipped: only posted bank activity is evidence. (Plaid removes the
  pending record when the posted one arrives; that removal still writes a zero row for a
  source_ref we never stored, which is harmless.)
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from tangible.connectors.base import ConnectorAccount, ConnectorError, Record

REMOVED = "REMOVED BY SOURCE"


@dataclass(frozen=True)
class LedgerRow:
    table: str
    values: dict[str, Any]  # table columns incl. source_ref and observed_at
    locator: str


def _dec(x: Any, what: str) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        raise ConnectorError(f"bad number for {what}") from None


def _day(x: Any, what: str, required: bool = True) -> date | None:
    if x in (None, ""):
        if required:
            raise ConnectorError(f"missing date {what}")
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        raise ConnectorError(f"bad date for {what}") from None


def _when(x: Any, what: str) -> datetime:
    if not x:
        raise ConnectorError(f"missing timestamp {what}")
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        raise ConnectorError(f"bad timestamp for {what}") from None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _name(x: Any) -> str | None:
    if isinstance(x, dict):
        v = x.get("name") or x.get("display_name") or x.get("company_name")
        return str(v) if v else None
    return None


def _order(d: dict[str, Any], at: datetime) -> LedgerRow:
    discount = _dec(d.get("total_discount") or 0, "total_discount")
    tax = _dec(d.get("total_tax") or 0, "total_tax")
    if d.get("subtotal") is not None:
        gross = _dec(d["subtotal"], "subtotal")
    else:  # derive pre-discount, pre-tax value from the total (shipping, if any, is included)
        gross = _dec(d.get("total_amount"), "total_amount") - tax + discount
    customer = d.get("customer")
    cust = customer.get("id") if isinstance(customer, dict) else None
    if not d.get("currency_code"):
        raise ConnectorError(f"order {d['id']}: no currency_code")
    return LedgerRow(
        "orders",
        {
            "source_ref": str(d["id"]),
            "channel": d.get("channel"),
            "gross": gross,
            "discounts": discount,
            "tax": tax,
            "currency": str(d["currency_code"])[:3].upper(),
            "customer_ref": None if cust is None else str(cust),
            "occurred_at": _when(d.get("created_at"), "order created_at"),
            "observed_at": at,
        },
        f"record={d['id']}",
    )


def _refund(d: dict[str, Any], at: datetime, order_id: str, fallback_when: Any) -> LedgerRow:
    return LedgerRow(
        "refund",
        {
            "source_ref": str(d["id"]),
            "order_ref": order_id,
            "amount": abs(_dec(d.get("amount"), "refund amount")),
            "reason": d.get("reason"),
            "occurred_at": _when(d.get("created_at") or fallback_when, "refund created_at"),
            "observed_at": at,
        },
        f"record={d['id']}",
    )


def _payout(d: dict[str, Any], at: datetime, processor: str) -> LedgerRow:
    return LedgerRow(
        "payout",
        {
            "source_ref": str(d["id"]),
            "processor": str(d.get("processor") or processor),
            "amount": _dec(d.get("amount"), "payout amount"),
            "fees": _dec(d.get("total_fees") or 0, "payout fees"),
            "expected_on": _day(d.get("expected_arrival_date"), "expected", False),
            "settled_on": _day(d.get("paid_at") or d.get("settled_on"), "settled", False),
            "observed_at": at,
        },
        f"record={d['id']}",
    )


def _invoice(d: dict[str, Any], at: datetime, kind: str) -> LedgerRow:
    party = _name(d.get("customer")) or _name(d.get("vendor")) or _name(d.get("contact"))
    return LedgerRow(
        "invoice",
        {
            "source_ref": str(d["id"]),
            "kind": "payable" if kind == "invoice_payable" else "receivable",
            "counterparty": party,
            "amount": _dec(d.get("total_amount"), "invoice total_amount"),
            "issued_on": _day(d.get("issue_date") or d.get("created_at"), "issue_date"),
            "due_on": _day(d.get("due_date"), "due_date", False),
            "paid_on": _day(d.get("paid_on"), "paid_on", False),
            "observed_at": at,
        },
        f"record={d['id']}",
    )


def _txn(kind: str, d: dict[str, Any], at: datetime) -> LedgerRow | None:
    tid = str(d["transaction_id"])
    if kind == "txn_removed":
        values: dict[str, Any] = {
            "source_ref": tid,
            "account_ref": str(d.get("account_id") or "unknown"),
            "amount": Decimal(0),
            "direction": "out",
            "counterparty": None,
            "description": REMOVED,
            "posted_on": at.date(),
            "observed_at": at,
        }
        return LedgerRow("bank_txn", values, f"record={tid}")
    if d.get("pending"):
        return None
    amount = _dec(d.get("amount"), "transaction amount")
    cp = d.get("merchant_name") or next(
        (n for c in d.get("counterparties") or [] if (n := _name(c))), None
    )
    return LedgerRow(
        "bank_txn",
        {
            "source_ref": tid,
            "account_ref": str(d["account_id"]),
            "amount": abs(amount),
            "direction": "out" if amount > 0 else "in",
            "counterparty": cp,
            "description": d.get("name"),
            "posted_on": _day(d.get("date"), "transaction date"),
            "observed_at": at,
        },
        f"record={tid}",
    )


def _balance(d: dict[str, Any], at: datetime) -> LedgerRow:
    aid = str(d["account_id"])
    as_of = at.date()
    current = (d.get("balances") or {}).get("current")
    return LedgerRow(
        "account_balance",
        {
            "source_ref": f"{aid}@{as_of.isoformat()}",
            "account_ref": aid,
            "balance": _dec(current, "balance"),
            "as_of": as_of,
            "observed_at": at,
        },
        f"record={aid}",
    )


def normalize(
    account: ConnectorAccount, records: list[Record], fetched_at: datetime
) -> list[LedgerRow]:
    """Ledger rows for one pull, at most one per (table, source_ref), in first-seen order."""
    out: dict[tuple[str, str], LedgerRow] = {}

    def add(row: LedgerRow | None) -> None:
        if row is not None:
            out[(row.table, row.values["source_ref"])] = row

    for rec in records:
        d, k = rec.data, rec.kind
        if k == "order":
            add(_order(d, fetched_at))
        elif k == "refund":
            if not d.get("order_id"):
                raise ConnectorError(f"refund {d.get('id')}: not linked to an order")
            add(_refund(d, fetched_at, str(d["order_id"]), d.get("order_created_at")))
        elif k == "payout":
            add(_payout(d, fetched_at, account.provider))
        elif k in ("invoice_receivable", "invoice_payable"):
            add(_invoice(d, fetched_at, k))
        elif k in ("txn_added", "txn_modified", "txn_removed"):
            add(_txn(k, d, fetched_at))
        elif k == "balance":
            add(_balance(d, fetched_at))
        else:
            raise ConnectorError(f"unknown record kind {k!r}")
    return list(out.values())
