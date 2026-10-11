"""Seed a demo tenant: synthetic borrowers dated to the present, users, and cases.

Every run creates a NEW tenant (`<name>-<YYYYMMDD-HHMM>`): ledger rows are append-only, so a
re-run never touches earlier data. Each borrower is one disjoint partition of the UCI order
lines, shifted by whole weeks so it ends just before `end_on`, with one plant story generated
around it (see tangible.datasets.synthetic) and ingested through the normal ingest path.

Time rule: a case's as_of is the end of the borrower's last data day, so every ledger row is
visible to `known_as_of` and nothing is read from "the future".

Cases start `open` with no plan and need no model. With `run_agent=True` (a live model, about
$1.50 a case) the staged cases are driven through the workbench services exactly as the UI
drives them: propose plan, approve, run the investigation, and (for two of them) sign.
"""

import dataclasses
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.agent.llm import ModelConfigError, client_from_env
from tangible.backtest.synthetic_cohort import partition_lines
from tangible.checks.registry import run_check
from tangible.checks.sets import check_set
from tangible.datasets.synthetic import PLANTS, generate
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import Conn, admin_connect, tenant_session
from tangible.demo.shift import default_end_on, shift_lines
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.ledger.csv_dir import read_ledger_dir
from tangible.ledger.types import Ledger
from tangible.policy import Policy, load_policy
from tangible.workbench import services
from tangible.workbench.auth import Principal, lookup_principal
from tangible.workbench.settings import Settings

N_PARTITIONS: Final = 12
# books_vs_processor judges a month once the 5th has passed, but payouts for its last sales days
# can still be settling then, so a clean merchant can show a gap on e.g. the 5th-9th. If the
# clean borrower trips a finding at `end_on`, the data moves back a week at a time, up to this.
MAX_BACKOFF_WEEKS: Final = 4
ANALYST: Final = "ana"
ADMIN: Final = "adm"
USERS: Final[tuple[tuple[str, str, str], ...]] = (
    (ANALYST, "analyst", "Ana Okafor"),
    (ADMIN, "credit_admin", "Adam Reilly"),
    ("len", "lender_underwriter", "Len Marsh"),
)
DEFAULT_BASE_URL: Final = "http://127.0.0.1:8000"
WORKBENCH_DEMO_URL: Final = "http://127.0.0.1:8001"  # the `workbench-demo` launch config

# The check each plant must trip (tests/test_plants_matrix.py), asserted at seed time.
EXPECTED_CHECK: Final[dict[str, str]] = {
    "hidden_reserve": "payouts_vs_deposits",
    "undisclosed_lender_debit": "unexplained_recurring_debit",
    "self_transfer_revenue": "self_transfers_as_revenue",
    "partial_coverage": "partial_coverage",
    "phantom_payouts": "orders_vs_payouts",
}


class SeedError(RuntimeError):
    """The demo data did not come out as designed. Raised rather than seeding a wrong demo."""


@dataclass(frozen=True)
class BorrowerSpec:
    name: str  # fictional UK company, used as the borrower name
    partition: int  # index into the N-way split of the order lines
    plants: tuple[str, ...]
    seed: int  # generator seed
    loan_amount: Decimal
    stage: str  # with --run-agent: "open", "review" (investigated) or "signed"


# Partitions and seeds were picked by running the baseline over every partition: the clean
# borrower stays free of review/hold findings, and each plant trips exactly its own check(s).
# The seed re-asserts both on every run (the UK calendar shifts with `end_on`).
SPECS: Final[tuple[BorrowerSpec, ...]] = (
    BorrowerSpec("Harbour & Pine Homeware Ltd", 5, (), 101, Decimal("75000"), "open"),
    BorrowerSpec("Kestrel Gift Co Ltd", 2, ("hidden_reserve",), 102, Decimal("120000"), "signed"),
    BorrowerSpec(
        "Marlow Lane Interiors Ltd",
        8,
        ("undisclosed_lender_debit",),
        103,
        Decimal("40000"),
        "review",
    ),  # fmt: skip
    BorrowerSpec(
        "Brightwell Crafts Ltd",
        3,
        ("self_transfer_revenue", "phantom_payouts"),
        104,
        Decimal("90000"),
        "signed",
    ),  # fmt: skip
    BorrowerSpec("Oakden Trading Ltd", 6, ("partial_coverage",), 105, Decimal("60000"), "review"),
)


@dataclass(frozen=True)
class SeededBorrower:
    name: str
    borrower_id: str
    case_id: str
    stage: str
    plants: tuple[str, ...]
    first_day: date
    last_day: date
    shift_days: int
    loan_amount: Decimal
    loud_checks: tuple[tuple[str, str], ...]  # (check_key, severity) above info at seed time


@dataclass(frozen=True)
class SeedSummary:
    tenant_id: str
    tenant_name: str
    login_url: str
    borrowers: tuple[SeededBorrower, ...]
    end_on: date  # the date the data was actually shifted to end on or just before
    requested_end_on: date

    @property
    def moved(self) -> bool:
        return self.end_on != self.requested_end_on

    def render(self) -> str:
        lines = [
            f"tenant   {self.tenant_name}",
            f"tenant id {self.tenant_id}",
            f"login    {self.login_url}",
            "users    " + ", ".join(f"{s} ({r})" for s, r, _ in USERS),
            f"end-on   {self.end_on}"
            + (
                f" (requested {self.requested_end_on}; moved back to keep the clean one clean)"
                if self.moved
                else ""
            ),  # fmt: skip
            "",
        ]
        for b in self.borrowers:
            plants = ", ".join(b.plants) or "none (clean)"
            lines += [
                f"{b.name}",
                f"  case {b.case_id}  stage {b.stage}  loan GBP {b.loan_amount:,.0f}",
                f"  plants: {plants}",
                f"  data {b.first_day} to {b.last_day} (shifted {b.shift_days} days)",
                "  baseline: "
                + (", ".join(f"{k}={s}" for k, s in b.loud_checks) or "no review/hold findings"),
            ]
        return "\n".join(lines)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def loud_findings(ledger: Ledger, as_of: datetime, policy: Policy) -> list[tuple[str, str]]:
    """The baseline's rule checks over a ledger, in memory (nothing is written):
    (check_key, severity) for every finding above info, sorted and de-duplicated."""
    assert policy.parsed is not None
    drafts = [
        f
        for key, version in check_set(policy.parsed.check_set)
        for f in run_check(key, version, ledger, as_of)
    ]
    return sorted({(f.check_key, f.severity) for f in drafts if f.severity != "info"})


def baseline_loud(
    conn: Conn, borrower_id: str, as_of: datetime, policy: Policy
) -> list[tuple[str, str]]:
    return loud_findings(load_ledger(conn, borrower_id), as_of, policy)


def data_span(ledger: Ledger) -> tuple[date, date]:
    """First and last day with any order, refund or bank row."""
    days = [o.occurred_at.date() for o in ledger.orders]
    days += [r.occurred_at.date() for r in ledger.refunds]
    days += [t.posted_on for t in ledger.bank_txns]
    return min(days), max(days)


def end_of_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), UTC)


def verify_story(spec: BorrowerSpec, loud: list[tuple[str, str]]) -> None:
    """A clean borrower must raise no review/hold finding; a planted one must trip its checks."""
    if not spec.plants:
        if loud:
            raise SeedError(f"{spec.name} was meant to be clean but the baseline raised {loud}")
        return
    fired = {k for k, _ in loud}
    missing = [p for p in spec.plants if EXPECTED_CHECK[p] not in fired]
    if missing:
        raise SeedError(
            f"{spec.name}: planted {missing} but the baseline did not raise "
            f"{[EXPECTED_CHECK[p] for p in missing]} (raised {loud})"
        )


@dataclass(frozen=True)
class Prepared:
    spec: BorrowerSpec
    shifted: Path  # the shifted order lines
    out: Path  # the generated synthetic directory
    offset: int  # days the lines were shifted


def choose_end_on(
    requested: date,
    specs: tuple[BorrowerSpec, ...],
    prepare: Callable[[BorrowerSpec, date], Prepared],
    policy: Policy,
    log: Callable[[str], None],
) -> tuple[date, dict[str, Prepared]]:
    """The latest end_on at or before `requested` (whole weeks back, at most MAX_BACKOFF_WEEKS)
    where every clean borrower really is clean. Returns it with the clean borrowers' prepared
    files. Checked on the generated files before anything is written to the database."""
    clean = [sp for sp in specs if not sp.plants]
    last_error: SeedError | None = None
    for weeks_back in range(MAX_BACKOFF_WEEKS + 1):
        end = requested - timedelta(days=7 * weeks_back)
        ready: dict[str, Prepared] = {}
        try:
            for sp in clean:
                pr = prepare(sp, end)
                ledger = read_ledger_dir(pr.out, pr.shifted)
                as_of = end_of_day(data_span(ledger)[1])
                verify_story(sp, loud_findings(ledger, as_of, policy))
                ready[sp.name] = pr
        except SeedError as e:
            last_error = e
            log(f"  not clean with data ending on or before {end}: {e}")
            continue
        if weeks_back:
            log(f"  moved the data {weeks_back} week(s) back, to end on or before {end}")
        return end, ready
    assert last_error is not None
    raise SeedError(f"no clean data within {MAX_BACKOFF_WEEKS} weeks of {requested}: {last_error}")


def _create_tenant(url: str, tenant_name: str) -> str:
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (tenant_name,)
        ).fetchone()
    assert row
    return str(row[0])


def _create_users(conn: Conn, tenant_id: str) -> None:
    for subject, role, display in USERS:
        conn.execute(
            "INSERT INTO app_user (tenant_id, subject, email, display_name, role) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, subject, f"{subject}@demo.test", display, role),
        )


def _case_status(url: str, tenant_id: str, case_id: str) -> str:
    with tenant_session(url, tenant_id) as conn:
        row = conn.execute("SELECT status FROM case_file WHERE id = %s", (case_id,)).fetchone()
    assert row
    return str(row[0])


def _failure_note(url: str, tenant_id: str, case_id: str) -> str:
    with tenant_session(url, tenant_id) as conn:
        row = conn.execute(
            "SELECT detail->>'error' FROM audit_log WHERE object_id = %s "
            "AND action = 'investigation_failed' ORDER BY at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    return str(row[0]) if row else "no failure recorded"


def drive_case(
    settings: Settings, analyst: Principal, case_id: str, stage: str, log: Callable[[str], None]
) -> None:
    """Take a case to `stage` through the public workbench services, one transaction per step,
    as the assigned analyst, exactly as the UI does."""
    url, tenant = settings.database_url, analyst.tenant_id
    with tenant_session(url, tenant) as conn:
        services.propose_plan(conn, settings, analyst, case_id)
    with tenant_session(url, tenant) as conn:
        services.approve_plan(conn, analyst, case_id)
    with tenant_session(url, tenant) as conn:
        job = services.start_investigation(conn, settings, analyst, case_id)
    log("  investigating (live model call)...")
    job.run()  # swallows its own failures and restores the prior status
    status = _case_status(url, tenant, case_id)
    if status != "review":
        raise SeedError(
            f"investigation of case {case_id} ended in '{status}': "
            f"{_failure_note(url, tenant, case_id)}"
        )
    if stage == "signed":
        with tenant_session(url, tenant) as conn:
            services.sign_case(conn, settings, analyst, case_id)


def seed_demo(
    *,
    name: str = "demo",
    end_on: date | None = None,
    store: Path = Path(".evidence"),
    work: Path = Path("data/demo"),
    lines: Path = Path("data/processed/uci_lines.csv"),
    run_agent: bool = False,
    database_url: str | None = None,
    specs: tuple[BorrowerSpec, ...] = SPECS,
    n_partitions: int = N_PARTITIONS,
    base_url: str = DEFAULT_BASE_URL,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> SeedSummary:
    if run_agent:
        # The model vendor is named only in agent/llm.py; here we just insist on a real backend
        # and let its own config check speak (missing model id, region, ...).
        if os.environ.get("TANGIBLE_MODEL_BACKEND", "none").strip().lower() in ("", "none"):
            raise SeedError(
                "--run-agent needs a live model: set TANGIBLE_MODEL_BACKEND and its settings "
                "(see the demo notes in the Makefile)"
            )
        try:
            client_from_env()
        except ModelConfigError as e:
            raise SeedError(f"--run-agent: {e}") from None
    if not lines.exists():
        raise SeedError(f"order lines not found: {lines} (try --lines PATH to uci_lines.csv)")
    for spec in specs:
        bad = sorted(set(spec.plants) - PLANTS)
        if bad:
            raise SeedError(f"{spec.name}: unknown plant(s) {bad}")
        if not 0 <= spec.partition < n_partitions:
            raise SeedError(f"{spec.name}: partition {spec.partition} not in 0..{n_partitions - 1}")
    requested = end_on or default_end_on()
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    tenant_name = f"{name}-{stamp}"
    settings = dataclasses.replace(Settings.from_env(), evidence_dir=store)
    if database_url:
        settings = dataclasses.replace(settings, database_url=database_url)
    url = settings.database_url
    policy = load_policy(settings.policy_path)

    # 1. files: partition, shift to end_on, generate. All of it deterministic.
    run_dir = work / tenant_name
    log(f"partitioning {lines} into {n_partitions} merchants...")
    parts = partition_lines(lines, run_dir / "parts", n_partitions)

    def prepare(spec: BorrowerSpec, end: date) -> Prepared:
        slug = _slug(spec.name)
        shifted = run_dir / "shifted" / slug / "order_lines.csv"
        offset = shift_lines(parts[spec.partition], shifted, end_on=end)
        out = run_dir / "synthetic" / slug
        generate(shifted, out, seed=spec.seed, plant=frozenset(spec.plants))
        return Prepared(spec, shifted, out, offset)

    effective, ready = choose_end_on(requested, specs, prepare, policy, log)
    prepared = [ready.get(sp.name) or prepare(sp, effective) for sp in specs]
    for pr in prepared:
        log(f"  {pr.spec.name}: shifted {pr.offset} days, generated")

    # 2. tenant, users, borrowers (ingested), baseline checks on each story
    tenant_id = _create_tenant(url, tenant_name)
    evidence = LocalEvidenceStore(store)
    with tenant_session(url, tenant_id) as conn:
        _create_users(conn, tenant_id)
    staged: list[tuple[BorrowerSpec, str, date, date, int, datetime, list[tuple[str, str]]]] = []
    for pr in prepared:
        spec = pr.spec
        with tenant_session(url, tenant_id) as conn:
            borrower_id = create_borrower(conn, tenant_id, spec.name)
            ingest_synthetic_dir(conn, evidence, tenant_id, borrower_id, pr.out, pr.shifted)
            first, last = data_span(load_ledger(conn, borrower_id))
            as_of = end_of_day(last)
            loud = baseline_loud(conn, borrower_id, as_of, policy)
        try:
            verify_story(spec, loud)
        except SeedError as e:
            raise SeedError(f"{e} (tenant {tenant_id} was left partly seeded)") from None
        staged.append((spec, borrower_id, first, last, pr.offset, as_of, loud))
        log(f"  ingested {spec.name}: baseline {loud or 'clean'}")

    # 3. cases, all assigned to the analyst, created by the credit admin like the UI does
    admin_p = lookup_principal(url, tenant_id, ADMIN)
    analyst_p = lookup_principal(url, tenant_id, ANALYST)
    seeded: list[SeededBorrower] = []
    for spec, borrower_id, first, last, offset, as_of, loud in staged:
        with tenant_session(url, tenant_id) as conn:
            case = services.create_new_case(
                conn, admin_p, borrower_id=borrower_id, as_of=as_of,
                loan_amount=spec.loan_amount, assigned_analyst=ANALYST,
            )  # fmt: skip
        case_id = str(case["id"])
        if run_agent and spec.stage != "open":
            log(f"{spec.name}: driving case to '{spec.stage}'")
            drive_case(settings, analyst_p, case_id, spec.stage, log)
        stage = _case_status(url, tenant_id, case_id)
        seeded.append(
            SeededBorrower(
                spec.name,
                borrower_id,
                case_id,
                stage,
                spec.plants,
                first,
                last,
                offset,
                spec.loan_amount,
                tuple(loud),
            )  # fmt: skip
        )
    login = f"{base_url}/dev/login?tenant={tenant_id}"
    return SeedSummary(tenant_id, tenant_name, login, tuple(seeded), effective, requested)
