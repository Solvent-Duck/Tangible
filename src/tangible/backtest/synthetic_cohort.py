"""Build a SYNTHETIC backtest cohort from one order-lines file.

READ THIS BEFORE QUOTING ANY NUMBER FROM A SYNTHETIC BACKTEST.
The outcome labels here are PLANTED, not observed: a borrower is labelled `defaulted` exactly when
we planted a problem into its generated financial footprint, and `repaid` otherwise. Nobody
repaid or defaulted on anything. So this cohort tests the plumbing (ingest, as-of discipline,
checks, policy, freeze, replay) and the catch rate of the checks against known plants. It says
nothing about predictive power on real lending outcomes; AUC and default rates computed on it are
artifacts of how the labels were made.

Construction: the order lines are split into `n_borrowers` disjoint "merchants" by a stable hash
of customer_id (anonymous lines by hash of invoice, so an invoice stays whole). Each partition is
fed to `tangible.datasets.synthetic.generate(seed=seed+i, plant=...)`.
"""

import csv
import hashlib
import random
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.backtest.cohort import Loan, write_cohort
from tangible.datasets.synthetic import PLANTS, generate
from tangible.datasets.uci_retail import CSV_COLUMNS, build_ledger
from tangible.metrics import compute

DECISION_LEAD_DAYS: Final = 14  # decision_date = data horizon - 14 days
LOAN_SHARE: Final = Decimal("0.25")  # of trailing-90d net revenue
DEFAULT_LOSS_SHARE: Final = Decimal("0.60")  # of loan_amount, for defaults
CENT: Final = Decimal("0.01")


def stable_bucket(token: str, n: int) -> int:
    """Process-independent hash (Python's hash() is salted per run)."""
    return int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big") % n


def partition_lines(lines_csv: Path, out_dir: Path, n: int) -> list[Path]:
    """Write n disjoint lines files; returns their paths (some may hold only a header)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"lines_{i:03d}.csv" for i in range(n)]
    files = [p.open("w", newline="") for p in paths]
    try:
        writers = [csv.writer(f, lineterminator="\n") for f in files]
        for w in writers:
            w.writerow(CSV_COLUMNS)
        with lines_csv.open(newline="") as src:
            for row in csv.DictReader(src):
                token = f"c:{row['customer_id']}" if row["customer_id"] else f"i:{row['invoice']}"
                writers[stable_bucket(token, n)].writerow([row[c] for c in CSV_COLUMNS])
    finally:
        for f in files:
            f.close()
    return paths


def choose_plants(seed: int, n: int, plant_rate: float) -> list[tuple[str, ...]]:
    """Deterministic: round(plant_rate * n) borrowers get 1-2 distinct plants each."""
    if not 0.0 <= plant_rate <= 1.0:
        raise ValueError("plant_rate must be in [0, 1]")
    rng = random.Random(f"plants:{seed}")
    k = int(Decimal(str(plant_rate * n)).quantize(Decimal(1), ROUND_HALF_UP))
    planted_idx = set(rng.sample(range(n), k))
    pool = sorted(PLANTS)
    out: list[tuple[str, ...]] = []
    for i in range(n):
        if i in planted_idx:
            out.append(tuple(sorted(rng.sample(pool, rng.choice((1, 2))))))
        else:
            out.append(())
    return out


def build_synthetic_cohort(
    lines_csv: Path, out_dir: Path, n_borrowers: int, seed: int, plant_rate: float
) -> Path:
    """Write the partitions, their generated footprints and `cohort.csv` under `out_dir`."""
    if n_borrowers < 1:
        raise ValueError("n_borrowers must be >= 1")
    lines_csv, out_dir = Path(lines_csv), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = partition_lines(lines_csv, out_dir / "lines", n_borrowers)
    plants = choose_plants(seed, n_borrowers, plant_rate)
    loans: list[Loan] = []
    for i, (part, planted) in enumerate(zip(parts, plants, strict=True)):
        ledger = build_ledger(part)
        dates = [o.occurred_at.date() for o in ledger.orders]
        dates += [r.occurred_at.date() for r in ledger.refunds]
        if not dates:  # nothing hashed to this bucket (tiny input)
            continue
        key = f"synth-{i:03d}"
        data_dir = out_dir / key
        generate(part, data_dir, seed=seed + i, plant=frozenset(planted))
        decision = max(dates) - timedelta(days=DECISION_LEAD_DAYS)
        as_of = datetime.combine(decision, time(23, 59, 59), UTC)
        revenue = compute("net_revenue", 1, ledger.known(as_of), as_of) or Decimal(0)
        loan_amount = max((revenue * LOAN_SHARE).quantize(CENT, ROUND_HALF_UP), CENT)
        defaulted = bool(planted)
        loss = (loan_amount * DEFAULT_LOSS_SHARE).quantize(CENT, ROUND_HALF_UP) if defaulted else 0
        loans.append(
            Loan(key, data_dir, part, decision, loan_amount,
                 "defaulted" if defaulted else "repaid", Decimal(loss), True, planted)
        )  # fmt: skip
    if not loans:
        raise ValueError(f"no usable borrowers could be built from {lines_csv}")
    path = out_dir / "cohort.csv"
    write_cohort(path, loans)
    return path
