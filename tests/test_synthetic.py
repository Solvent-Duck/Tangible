import csv
import json
import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tangible.datasets.synthetic import PLANTS, generate
from tangible.datasets.uci_retail import CSV_COLUMNS

PRICES = ["2.50", "3.95", "4.25", "5.10", "6.75", "7.95", "9.50", "12.00", "14.25", "18.00"]
EVIDENCE = {"evidence_pull_id", "evidence_locator"}
TABLES = ["payout", "bank_txn", "invoice", "inventory_snapshot", "account_balance"]


def write_lines(path: Path, days: int = 80) -> Path:
    """A small hand-made order file: card orders, wholesale orders, credit notes, discounts."""
    rng = random.Random(42)
    start = date(2010, 1, 4)
    rows: list[list[str]] = []
    invoice = 500000

    def line(inv: str, code: str, qty: int, stamp: str, price: str, customer: str) -> list[str]:
        desc = "discount" if code == "D" else f"ITEM {code}"
        return ["0", inv, code, desc, str(qty), stamp, price, customer, "United Kingdom"]

    for i in range(days):
        stamp = f"{(start + timedelta(days=i)).isoformat()}T10:00:00"
        for _ in range(rng.randint(1, 4)):
            invoice += 1
            customer = str(rng.randint(1000, 1040)) if rng.random() < 0.8 else ""
            for _ in range(rng.randint(1, 3)):
                code = f"A{rng.randint(1, 10):02d}"
                rows.append(
                    line(str(invoice), code, rng.randint(1, 6), stamp, rng.choice(PRICES), customer)
                )
            if rng.random() < 0.2:
                rows.append(line(str(invoice), "D", -1, stamp, "2.00", customer))
        if i % 9 == 4:  # wholesale: over GBP 500 net
            invoice += 1
            qty = rng.randint(150, 220)
            rows.append(line(str(invoice), "A01", qty, stamp, "4.50", str(rng.randint(2000, 2010))))
        if i % 13 == 6:  # credit note
            rows.append(line(f"C{invoice}", "A02", -2, stamp, "3.95", "1001"))
    with path.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    return path


@pytest.fixture
def lines(tmp_path: Path) -> Path:
    return write_lines(tmp_path / "lines.csv")


def run(
    lines: Path, tmp_path: Path, name: str, seed: int = 7, plant: set[str] | None = None
) -> Path:
    out = tmp_path / name
    generate(lines, out, seed=seed, plant=frozenset(plant or ()))
    return out


def load(out: Path, name: str) -> list[dict[str, str]]:
    with (out / f"{name}.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def signed(row: dict[str, str]) -> Decimal:
    amount = Decimal(row["amount"])
    return amount if row["direction"] == "in" else -amount


def planted_names(out: Path) -> list[str]:
    return [p["name"] for p in json.loads((out / "planted.json").read_text())]


def weekdays_between(a: date, b: date, weekday: int | None = None) -> set[str]:
    """ISO dates from a to b (inclusive) that are business days, or all of one weekday."""
    days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    if weekday is None:
        return {d.isoformat() for d in days if d.weekday() < 5}
    return {d.isoformat() for d in days if d.weekday() == weekday}


def without_evidence(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{k: v for k, v in r.items() if k not in EVIDENCE} for r in rows]


def test_clean_run_reconciles(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "clean")
    payouts = load(out, "payout")
    bank = load(out, "bank_txn")
    invoices = load(out, "invoice")
    assert payouts

    # Every payout has exactly one deposit of the same amount, within 3 days.
    deposits = [b for b in bank if b["counterparty"] == "SHOPIFY PAYMENTS"]
    assert len(deposits) == len(payouts)
    matched = total = Decimal(0)
    for p in payouts:
        hits = [b for b in deposits if p["source_ref"] in b["description"]]
        assert len(hits) == 1, p["source_ref"]
        (hit,) = hits
        amount = Decimal(p["amount"])
        total += amount
        gap = date.fromisoformat(hit["posted_on"]) - date.fromisoformat(p["settled_on"])
        if abs(gap.days) <= 3 and abs(Decimal(hit["amount"]) - amount) <= amount / 100:
            matched += amount
    assert matched / total >= Decimal("0.99")

    # Balances are opening plus cumulative bank rows, and never negative.
    flows = [(date.fromisoformat(b["posted_on"]), signed(b)) for b in bank]
    balances = load(out, "account_balance")
    openings = set()
    for r in balances:
        as_of = date.fromisoformat(r["as_of"])
        cumulative = sum((v for d, v in flows if d <= as_of), Decimal(0))
        openings.add(Decimal(r["balance"]) - cumulative)
    assert len(openings) == 1
    assert min(Decimal(r["balance"]) for r in balances) > 0

    # Every paid payable has a matching bank outflow.
    paid = [r for r in invoices if r["kind"] == "payable" and r["paid_on"]]
    outflows = Counter(
        (b["counterparty"], b["amount"], b["posted_on"]) for b in bank if b["direction"] == "out"
    )
    needed = Counter((r["counterparty"], r["amount"], r["paid_on"]) for r in paid)
    assert not needed - outflows

    # Wholesale orders are receivables that show up as customer deposits.
    receivables = [r for r in invoices if r["kind"] == "receivable" and r["paid_on"]]
    sales_invoices = [r for r in receivables if Decimal(r["amount"]) > 0]
    credit_notes = [r for r in receivables if Decimal(r["amount"]) < 0]
    customer_rows = [b for b in bank if b["counterparty"].startswith(("CUSTOMER", "GUEST"))]
    assert len([b for b in customer_rows if b["direction"] == "in"]) == len(sales_invoices)
    assert len([b for b in customer_rows if b["direction"] == "out"]) == len(credit_notes)

    # Nothing is observed before it happened.
    for table in TABLES:
        for r in load(out, table):
            observed = datetime.fromisoformat(r["observed_at"])
            if table == "payout":
                event = r["settled_on"]
            elif table == "bank_txn":
                event = r["posted_on"]
            elif table == "invoice":
                event = r["paid_on"] or r["issued_on"]
            else:
                event = r["as_of"]
            event_time = datetime.fromisoformat(event).replace(tzinfo=UTC)
            assert observed >= event_time, (table, r)
            assert r["evidence_locator"].startswith("row=")


def test_same_seed_gives_identical_bytes(lines: Path, tmp_path: Path) -> None:
    first = run(lines, tmp_path, "a")
    second = run(lines, tmp_path, "b")
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_different_seed_changes_output(lines: Path, tmp_path: Path) -> None:
    a = run(lines, tmp_path, "seed7", seed=7)
    b = run(lines, tmp_path, "seed8", seed=8)
    assert (a / "bank_txn.csv").read_bytes() != (b / "bank_txn.csv").read_bytes()


def test_no_plants_means_empty_planted_list(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "clean")
    assert json.loads((out / "planted.json").read_text()) == []


def test_unknown_plant_is_rejected(lines: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown plant"):
        run(lines, tmp_path, "bad", plant={"no_such_problem"})


def test_hidden_reserve(lines: Path, tmp_path: Path) -> None:
    clean = run(lines, tmp_path, "clean")
    planted = run(lines, tmp_path, "reserve", plant={"hidden_reserve"})
    assert planted_names(planted) == ["hidden_reserve"]
    # payout.amount is what the processor reports, so the payout table does not change.
    assert (clean / "payout.csv").read_bytes() == (planted / "payout.csv").read_bytes()

    payouts = {p["source_ref"]: p for p in load(planted, "payout")}
    clean_dep = {
        b["description"]: b
        for b in load(clean, "bank_txn")
        if b["counterparty"] == "SHOPIFY PAYMENTS"
    }
    planted_dep = {
        b["description"]: b
        for b in load(planted, "bank_txn")
        if b["counterparty"] == "SHOPIFY PAYMENTS"
    }
    short = 0
    for ref, p in payouts.items():
        deposit = planted_dep[f"SHOPIFY PAYOUT {ref}"]
        base = clean_dep[f"SHOPIFY PAYOUT {ref}"]
        if deposit["amount"] != base["amount"]:
            short += 1
            assert abs(
                Decimal(deposit["amount"]) - Decimal(p["amount"]) * Decimal("0.85")
            ) <= Decimal("0.01")
            gap = date.fromisoformat(deposit["posted_on"]) - date.fromisoformat(base["posted_on"])
            assert gap.days >= 4
    assert short > 0


def test_undisclosed_lender_debit(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "debit", plant={"undisclosed_lender_debit"})
    (window,) = json.loads((out / "planted.json").read_text())
    debits = [b for b in load(out, "bank_txn") if b["counterparty"] == "YRDEAL CAPITAL"]
    assert {b["amount"] for b in debits} == {"450.00"}
    assert {b["direction"] for b in debits} == {"out"}
    assert {b["posted_on"] for b in debits} == weekdays_between(
        date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    )
    assert not [r for r in load(out, "invoice") if "YRDEAL" in (r["counterparty"] or "")]


def test_self_transfer_revenue(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "self", plant={"self_transfer_revenue"})
    (window,) = json.loads((out / "planted.json").read_text())
    transfers = [
        b
        for b in load(out, "bank_txn")
        if b["counterparty"] == "J SMITH PERSONAL" and b["description"] == "SALES TRANSFER"
    ]
    assert transfers
    for b in transfers:
        amount = Decimal(b["amount"])
        assert b["direction"] == "in"
        assert amount % 500 == 0 and 5000 <= amount <= 9000
        assert date.fromisoformat(b["posted_on"]).weekday() == 4
    assert {b["posted_on"] for b in transfers} == weekdays_between(
        date.fromisoformat(window["start"]), date.fromisoformat(window["end"]), weekday=4
    )


def test_partial_coverage(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "partial", plant={"partial_coverage"})
    bank = load(out, "bank_txn")
    amazon = [b for b in bank if b["counterparty"] == "AMAZON PAYMENTS"]
    assert amazon
    assert all(b["direction"] == "in" for b in amazon)
    assert all(date.fromisoformat(b["posted_on"]).weekday() == 3 for b in amazon)
    amazon_total = sum(Decimal(b["amount"]) for b in amazon)
    deposits_total = sum(
        Decimal(b["amount"]) for b in bank
        if b["direction"] == "in" and b["counterparty"] != "AMAZON PAYMENTS"
        and not b["counterparty"].startswith("J SMITH")
    )  # fmt: skip
    assert Decimal("0.15") < amazon_total / deposits_total < Decimal("0.35")


def test_phantom_payouts(lines: Path, tmp_path: Path) -> None:
    clean = run(lines, tmp_path, "clean")
    planted = run(lines, tmp_path, "phantom", plant={"phantom_payouts"})
    assert planted_names(planted) == ["phantom_payouts"]
    payouts = load(planted, "payout")
    phantom = [p for p in payouts if p["source_ref"].startswith("PO-PH-")]
    real = [p for p in payouts if not p["source_ref"].startswith("PO-PH-")]
    assert phantom
    assert without_evidence(real) == without_evidence(load(clean, "payout"))

    deposits = [b for b in load(planted, "bank_txn") if b["counterparty"] == "SHOPIFY PAYMENTS"]
    assert len(deposits) == len(payouts)
    for p in phantom:
        (hit,) = [b for b in deposits if p["source_ref"] in b["description"]]
        gap = date.fromisoformat(hit["posted_on"]) - date.fromisoformat(p["settled_on"])
        assert abs(gap.days) <= 3
        assert Decimal(hit["amount"]) == Decimal(p["amount"])


def test_plants_combine_and_record_each(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "all", plant=set(PLANTS))
    assert planted_names(out) == sorted(PLANTS)


def test_credit_notes_follow_the_channel_of_the_customers_last_order(tmp_path: Path) -> None:
    def row(inv: str, qty: int, day: str, price: str, customer: str) -> list[str]:
        stamp = f"{day}T10:00:00"
        return ["0", inv, "A01", "ITEM A01", str(qty), stamp, price, customer, "United Kingdom"]

    rows = [
        row("600001", 200, "2010-01-04", "4.50", "2001"),  # wholesale order (GBP 900)
        row("600002", 2, "2010-01-04", "4.50", "1001"),  # card order
        row("C600003", -2, "2010-01-11", "4.50", "2001"),  # small note, wholesale customer
        row("C600004", -2, "2010-01-11", "4.50", "1001"),  # card customer
        row("600005", 1, "2010-02-26", "4.50", "1001"),  # keeps the data running past the notes
    ]
    path = tmp_path / "lines.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    out = run(path, tmp_path, "out")
    invoices = load(out, "invoice")
    assert [r["source_ref"] for r in invoices if r["source_ref"].startswith("CN-")] == [
        "CN-C600003",
        "CN-C600003",
    ]  # issue row and paid revision
    assert any(b["description"] == "REFUND CN C600003" for b in load(out, "bank_txn"))
    assert not any("C600004" in b["description"] for b in load(out, "bank_txn"))


def test_stock_never_negative_and_snapshots_start_when_stocked(lines: Path, tmp_path: Path) -> None:
    out = run(lines, tmp_path, "clean")
    snaps = load(out, "inventory_snapshot")
    assert snaps
    assert all(int(r["units_on_hand"]) >= 0 for r in snaps)
    assert all(Decimal(r["unit_cost"]) > 0 for r in snaps)


def test_operating_costs_are_invoiced_except_payroll_vat_and_drawings(
    lines: Path, tmp_path: Path
) -> None:
    out = run(lines, tmp_path, "clean")
    payables = {r["counterparty"] for r in load(out, "invoice") if r["kind"] == "payable"}
    assert {
        "HOLLOWAY PROPERTY LTD",
        "GOOGLE ADS",
        "META ADS",
        "PARCELPOINT LOGISTICS LTD",
    } <= payables
    assert not payables & {"STAFF PAYROLL", "HMRC VAT", "J SMITH PERSONAL"}


def test_unit_cost_varies_by_sku_and_steps_up_by_year(tmp_path: Path) -> None:
    lines = write_lines(tmp_path / "long.csv", days=500)
    out = run(lines, tmp_path, "clean")
    by_sku: dict[str, set[str]] = {}
    for r in load(out, "inventory_snapshot"):
        by_sku.setdefault(r["sku"], set()).add(r["unit_cost"])
    assert len({min(c) for c in by_sku.values()}) > 3  # SKUs have different margins
    assert any(len(c) > 1 for c in by_sku.values())  # and costs rose across a January


def test_trade_suppliers_pay_late_about_one_in_eight(tmp_path: Path) -> None:
    out = run(write_lines(tmp_path / "long.csv", days=500), tmp_path, "clean")
    paid = [
        r
        for r in load(out, "invoice")
        if r["kind"] == "payable" and r["paid_on"] and r["source_ref"].startswith("PI-")
    ]  # direct debits (rent, ads, software) are never late
    late = sum(1 for r in paid if r["paid_on"] > r["due_on"])
    assert len(paid) > 100
    assert 0.05 <= late / len(paid) <= 0.20
