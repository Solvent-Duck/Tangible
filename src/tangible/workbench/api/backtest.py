"""The latest backtest report (synthetic cohort), summarised. Any staff role; viewing is audited."""

from typing import Any

from fastapi import APIRouter

from tangible.backtest import view as backtest_view
from tangible.workbench import audit, services
from tangible.workbench.auth import STAFF_ROLES
from tangible.workbench.deps import ConnDep, PrincipalDep

router = APIRouter(prefix="/api")


@router.get("/backtest")
def backtest(conn: ConnDep, p: PrincipalDep) -> dict[str, Any]:
    if p.role not in STAFF_ROLES:
        raise services.Forbidden("not available to borrower accounts")
    loaded = backtest_view.load()
    audit.record(
        conn, p, "view", "backtest_report", None,
        {"path": str(loaded.path), "found": loaded.data is not None},
    )  # fmt: skip
    return {
        "summary": backtest_view.summarise(loaded.data) if loaded.data is not None else None,
        "problem": loaded.problem,
        "report_path": str(loaded.path),
        "hint": backtest_view.GENERATE_HINT,
    }
