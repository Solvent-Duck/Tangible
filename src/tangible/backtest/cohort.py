"""Backtest cohorts: one row per past loan.

CSV columns: borrower_key, data_dir, lines_csv, decision_date, loan_amount, outcome
(repaid|defaulted), loss_amount, synthetic (true|false), planted (semicolon list, may be empty).
Relative `data_dir` / `lines_csv` paths resolve against the cohort file's directory.
"""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

COLUMNS: Final = (
    "borrower_key",
    "data_dir",
    "lines_csv",
    "decision_date",
    "loan_amount",
    "outcome",
    "loss_amount",
    "synthetic",
    "planted",
)
OUTCOMES: Final = ("repaid", "defaulted")


@dataclass(frozen=True)
class Loan:
    borrower_key: str
    data_dir: Path
    lines_csv: Path
    decision_date: date
    loan_amount: Decimal
    outcome: str
    loss_amount: Decimal
    synthetic: bool
    planted: tuple[str, ...] = ()

    @property
    def defaulted(self) -> bool:
        return self.outcome == "defaulted"


def _money(row: dict[str, str], col: str, where: str) -> Decimal:
    try:
        v = Decimal(row[col])
    except InvalidOperation:
        raise ValueError(f"{where}: {col} {row[col]!r} is not a number") from None
    if not v.is_finite() or v < 0:
        raise ValueError(f"{where}: {col} must be a finite number >= 0, got {row[col]!r}")
    return v


def load_cohort(path: Path) -> list[Loan]:
    path = Path(path)
    base = path.parent
    loans: list[Loan] = []
    seen: set[str] = set()
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {missing}")
        for n, row in enumerate(reader, start=2):
            where = f"{path}:{n}"
            key = row["borrower_key"].strip()
            if not key:
                raise ValueError(f"{where}: borrower_key is empty")
            if key in seen:
                raise ValueError(f"{where}: duplicate borrower_key {key!r}")
            seen.add(key)
            try:
                decision = date.fromisoformat(row["decision_date"])
            except ValueError:
                raise ValueError(f"{where}: bad decision_date {row['decision_date']!r}") from None
            outcome = row["outcome"].strip()
            if outcome not in OUTCOMES:
                raise ValueError(f"{where}: outcome must be one of {OUTCOMES}, got {outcome!r}")
            loan_amount = _money(row, "loan_amount", where)
            loss = _money(row, "loss_amount", where)
            if loan_amount <= 0:
                raise ValueError(f"{where}: loan_amount must be > 0")
            if outcome == "repaid" and loss != 0:
                raise ValueError(f"{where}: a repaid loan cannot have loss_amount {loss}")
            if loss > loan_amount:
                raise ValueError(f"{where}: loss_amount {loss} exceeds loan_amount {loan_amount}")
            flag = row["synthetic"].strip().lower()
            if flag not in ("true", "false"):
                raise ValueError(f"{where}: synthetic must be true or false, got {flag!r}")
            planted = tuple(p.strip() for p in row["planted"].split(";") if p.strip())
            data_dir, lines_csv = (base / row[c] for c in ("data_dir", "lines_csv"))
            for p in (data_dir, lines_csv):
                if not p.exists():
                    raise ValueError(f"{where}: {p} does not exist")
            loans.append(
                Loan(key, data_dir, lines_csv, decision, loan_amount, outcome, loss,
                     flag == "true", planted)
            )  # fmt: skip
    if not loans:
        raise ValueError(f"{path}: cohort is empty")
    return loans


def write_cohort(path: Path, loans: list[Loan]) -> None:
    """Write `loans`; paths are stored relative to the cohort file when they sit under it."""
    path = Path(path)

    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(path.parent.resolve()))
        except ValueError:
            return str(p.resolve())

    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        for ln in loans:
            w.writerow(
                [ln.borrower_key, rel(ln.data_dir), rel(ln.lines_csv), ln.decision_date.isoformat(),
                 f"{ln.loan_amount:.2f}", ln.outcome, f"{ln.loss_amount:.2f}",
                 "true" if ln.synthetic else "false", ";".join(ln.planted)]
            )  # fmt: skip
