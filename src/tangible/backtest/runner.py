"""Run a cohort through the real pipeline, one case per loan, as of each loan's decision date.

Per loan: create a borrower in a fresh tenant, ingest its data directory, open a case with
as_of = decision_date 23:59:59 UTC, run the baseline (metrics + rule checks), optionally run an
injected agent step, freeze the assessment (created_by 'backtest') and replay it.

Agent runs: with `agent` set, each of `agent_runs` runs gets its own case (same as_of) so that
band-to-band spread is measurable; run 0 is the primary case whose band, limit, metrics and
findings are reported. Without an agent there is one case and spread is "not run".

Time-leak guard (before each freeze): no row the lender is allowed to see may have
observed_at > as_of, and the number of rows the time rule hid is recorded.
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from tangible.assessment import freeze, replay, run_baseline
from tangible.backtest.cohort import Loan
from tangible.db.cases import Case, create_case
from tangible.db.findings import load_findings
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import Conn, admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.ledger.types import Ledger
from tangible.policy import Policy, load_policy

CREATED_BY = "backtest"


@dataclass(frozen=True)
class AgentRunInfo:
    cost_usd: Decimal
    steps: int
    finding_ids: tuple[str, ...] = ()


class AgentStep(Protocol):
    def __call__(self, conn: Conn, case: Case, policy: Policy) -> AgentRunInfo: ...


@dataclass(frozen=True)
class FindingRec:
    check_key: str
    severity: str
    origin: str


@dataclass(frozen=True)
class LeakCheck:
    total_rows: int
    visible_rows: int  # rows in ledger.known(as_of)
    hidden_rows: int  # raw rows with observed_at > as_of (hidden by the time rule)
    violations: int  # visible rows with observed_at > as_of; must be 0


@dataclass(frozen=True)
class AgentRunRec:
    band: str | None
    limit: Decimal | None
    cost_usd: Decimal
    steps: int
    replay_match: bool


@dataclass(frozen=True)
class LoanResult:
    loan: Loan
    band: str | None
    limit: Decimal | None
    metrics: dict[str, Decimal | None]
    findings: tuple[FindingRec, ...]
    replay_match: bool
    replay_diffs: tuple[str, ...]
    leak: LeakCheck
    case_id: str
    assessment_id: str
    agent_runs: tuple[AgentRunRec, ...] = ()


@dataclass(frozen=True)
class BacktestResult:
    tenant_id: str
    policy_version: str
    policy_hash: str
    loans: tuple[LoanResult, ...]
    agent_requested_runs: int = 0  # 0 = agent not run


def as_of_for(loan: Loan) -> datetime:
    return datetime.combine(loan.decision_date, time(23, 59, 59), UTC)


def leak_check(ledger: Ledger, as_of: datetime) -> LeakCheck:
    """Count what the time rule hides and verify that nothing observed later is visible."""
    known = ledger.known(as_of)
    total = visible = hidden = violations = 0
    for fld in dataclasses.fields(ledger):
        raw: tuple[Any, ...] = getattr(ledger, fld.name)
        seen: tuple[Any, ...] = getattr(known, fld.name)
        total += len(raw)
        visible += len(seen)
        hidden += sum(1 for r in raw if r.observed_at > as_of)
        violations += sum(1 for r in seen if r.observed_at > as_of)
    return LeakCheck(total, visible, hidden, violations)


def _create_tenant(conninfo: str, name: str) -> str:
    with admin_connect(conninfo) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
    assert row
    return str(row[0])


def _metric_values(conn: Conn, assessment_id: str) -> dict[str, Decimal | None]:
    rows = conn.execute(
        "SELECT mv.metric_key, mv.value FROM assessment_item ai "
        "JOIN metric_value mv ON mv.id = ai.ref_id "
        "WHERE ai.assessment_id = %s AND ai.kind = 'metric' ORDER BY mv.metric_key",
        (assessment_id,),
    ).fetchall()
    return {str(k): v for k, v in rows}


@dataclass
class _Run:
    case: Case
    band: str | None
    limit: Decimal | None
    assessment_id: str
    match: bool
    diffs: list[str]
    leak: LeakCheck
    info: AgentRunInfo | None = None
    findings: list[FindingRec] = field(default_factory=list)
    metrics: dict[str, Decimal | None] = field(default_factory=dict)


def _one_case(
    conninfo: str,
    tenant: str,
    borrower: str,
    loan: Loan,
    policy: Policy,
    policy_path: Path,
    agent: AgentStep | None,
) -> _Run:
    as_of = as_of_for(loan)
    info: AgentRunInfo | None = None
    with tenant_session(conninfo, tenant) as conn:
        case = create_case(conn, tenant, borrower, as_of, loan_amount=loan.loan_amount)
        run_baseline(conn, case, policy)
        if agent is not None:
            info = agent(conn, case, policy)
    with tenant_session(conninfo, tenant) as conn:
        leak = leak_check(load_ledger(conn, borrower), as_of)
        if leak.violations:
            raise AssertionError(
                f"time-rule leak for {loan.borrower_key}: {leak.violations} visible row(s) "
                f"observed after {as_of.isoformat()}"
            )
    with tenant_session(conninfo, tenant) as conn:
        frozen = freeze(conn, case, policy, created_by=CREATED_BY)
    with tenant_session(conninfo, tenant) as conn:
        rep = replay(conn, frozen.assessment_id, policy_path)
        findings = [
            FindingRec(f.draft.check_key, f.draft.severity, f.origin)
            for f in load_findings(conn, case_id=case.id)
        ]
        metrics = _metric_values(conn, frozen.assessment_id)
    return _Run(
        case, frozen.result.band, frozen.result.limit, frozen.assessment_id, rep.match,
        list(rep.diffs), leak, info, findings, metrics,
    )  # fmt: skip


def run_backtest(
    conninfo: str,
    tenant_name: str,
    cohort: list[Loan],
    policy_path: Path,
    store_dir: Path,
    *,
    agent: AgentStep | None = None,
    agent_runs: int = 1,
    progress: Callable[[int, int, str], None] | None = None,
) -> BacktestResult:
    if agent is not None and agent_runs < 1:
        raise ValueError("agent_runs must be >= 1 when an agent is given")
    policy = load_policy(Path(policy_path))
    store = LocalEvidenceStore(Path(store_dir))
    tenant = _create_tenant(conninfo, tenant_name)
    results: list[LoanResult] = []
    for n, loan in enumerate(cohort, start=1):
        if progress:
            progress(n, len(cohort), loan.borrower_key)
        with tenant_session(conninfo, tenant) as conn:
            borrower = create_borrower(conn, tenant, loan.borrower_key)
            ingest_synthetic_dir(conn, store, tenant, borrower, loan.data_dir, loan.lines_csv)
        n_runs = agent_runs if agent is not None else 1
        runs = [
            _one_case(conninfo, tenant, borrower, loan, policy, Path(policy_path), agent)
            for _ in range(n_runs)
        ]
        primary = runs[0]
        agent_recs = tuple(
            AgentRunRec(r.band, r.limit, r.info.cost_usd, r.info.steps, r.match)
            for r in runs
            if r.info is not None
        )
        results.append(
            LoanResult(
                loan,
                primary.band,
                primary.limit,
                primary.metrics,
                tuple(primary.findings),
                all(r.match for r in runs),
                tuple(d for r in runs for d in r.diffs),
                primary.leak,
                primary.case.id,
                primary.assessment_id,
                agent_recs,
            )  # fmt: skip
        )
    assert policy.parsed
    return BacktestResult(
        tenant, policy.version, policy.sha256, tuple(results),
        agent_runs if agent is not None else 0,
    )  # fmt: skip
