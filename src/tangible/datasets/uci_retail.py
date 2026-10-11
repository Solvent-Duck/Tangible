"""Loader for UCI Online Retail II (CC BY 4.0, Chen 2012, doi:10.24432/C5CG6D).

A real UK online gift-ware retailer, Dec 2009 to Dec 2011. Order lines only: no payouts, bank,
inventory or accounting data (see tangible.datasets.synthetic for those).

Mapping choices (the source has no observation times or order links on credit notes):
- One `Order` per invoice. gross = sum of merchandise/postage lines; discounts = the 'D' lines.
- Invoices prefixed 'C' are credit notes -> `Refund`. They don't reference the original invoice,
  so order_ref is the credit note's own invoice number.
- observed_at = occurred_at + `observed_lag`. Pass a lag to model late-arriving data.
- Rows with non-positive price, bad-debt 'A' invoices, and fee/test stock codes are dropped.
- Prices are used as they are. The source prices are GBP; Tangible's ledgers and demo are in US
  dollars, so the rows are labelled USD (the Order default). The amounts are not converted.
- 2010-12 appears in both worksheets; an invoice is taken from the first sheet that has it.
"""

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl

from tangible.ledger.types import Ledger, Order, Refund

CSV_COLUMNS = ["sheet", "invoice", "stock_code", "description", "quantity", "invoice_date",
               "price", "customer_id", "country"]  # fmt: skip
EXCLUDED_CODES = {"BANK CHARGES", "AMAZONFEE", "ADJUST", "ADJUST2", "B", "TEST001", "TEST002"}
DISCOUNT_CODE = "D"


def xlsx_to_csv(xlsx: Path, dest: Path) -> int:
    """One-off flatten of both worksheets to CSV (xlsx is slow to stream). Returns row count."""
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    n = 0
    with dest.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(CSV_COLUMNS)
        for sheet_no, ws in enumerate(wb.worksheets):
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                inv, code, desc, qty, when, price, cust, country = row
                assert isinstance(when, datetime)
                customer = "" if cust is None else int(float(str(cust)))
                out.writerow([sheet_no, inv, code, desc, qty, when.isoformat(), price,
                              customer, country])  # fmt: skip
                n += 1
    return n


@dataclass(frozen=True)
class Line:
    invoice: str
    stock_code: str
    description: str
    quantity: int
    price: Decimal
    occurred_at: datetime
    customer_id: str | None


def read_lines(csv_path: Path) -> Iterator[Line]:
    """Cleaned lines in file order, with the sheet-overlap duplicates removed."""
    sheet_of_invoice: dict[str, str] = {}
    with csv_path.open(newline="") as f:
        for r in csv.DictReader(f):
            invoice = r["invoice"]
            if sheet_of_invoice.setdefault(invoice, r["sheet"]) != r["sheet"]:
                continue
            price = Decimal(r["price"])
            code = r["stock_code"].strip()
            if invoice.startswith("A") or code in EXCLUDED_CODES:
                continue
            if code != DISCOUNT_CODE and price <= 0:
                continue
            yield Line(
                invoice=invoice,
                stock_code=code,
                description=r["description"].strip(),
                quantity=int(r["quantity"]),
                price=price,
                occurred_at=datetime.fromisoformat(r["invoice_date"]).replace(tzinfo=UTC),
                customer_id=r["customer_id"] or None,
            )


def build_ledger(csv_path: Path, observed_lag: timedelta = timedelta(0)) -> Ledger:
    orders: dict[str, Order] = {}
    refunds: dict[str, Refund] = {}
    for ln in read_lines(csv_path):
        amount = ln.price * abs(ln.quantity)
        observed = ln.occurred_at + observed_lag
        if ln.invoice.startswith("C"):
            if ln.stock_code == DISCOUNT_CODE:
                continue
            prev = refunds.get(ln.invoice)
            total = amount + (prev.amount if prev else Decimal(0))
            refunds[ln.invoice] = Refund(ln.invoice, ln.invoice, total, ln.occurred_at, observed)
            continue
        prev_o = orders.get(ln.invoice)
        gross = prev_o.gross if prev_o else Decimal(0)
        disc = prev_o.discounts if prev_o else Decimal(0)
        if ln.stock_code == DISCOUNT_CODE:
            disc += abs(ln.price * ln.quantity)
        elif ln.quantity > 0:
            gross += amount
        orders[ln.invoice] = Order(
            ln.invoice, gross, disc, ln.occurred_at, observed, customer_ref=ln.customer_id
        )
    return Ledger(orders=tuple(orders.values()), refunds=tuple(refunds.values()))
