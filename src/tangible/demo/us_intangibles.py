"""US demo borrowers for the intangibles slice, and a scripted (model-free) signal pre-run.

Three fictional US borrowers whose public-web fixtures live in web/demo_fixtures/us:

- Juniper & Rye Bakery LLC (Portland, OR): lifted by the loyalty route. Its reviews talk about
  coming back for years; the ledger needs repeat_customer_share >= 0.35 for the route to fire.
- Copperline Hardware Supply Inc (Columbus, OH): lifted by the stock route (stock_cover >= 1,
  little aged stock, days of inventory <= 120), from its inventory ledger.
- Northgate Phone Repair LLC (Tucson, AZ): a bought-review pattern (940 reviews on a shop that
  opened in late 2025; review_volume_vs_orders fires) and a same-name decoy listing in Spokane,
  WA, whose review excerpt tries to talk the agent into claiming it. match_entity scores the decoy
  0.4, so record_signal refuses it.

`stage_public_signals` stands in for the search sub-agents (a parallel track) so the demo needs
no model: it writes the application row, then runs place_search -> match_entity -> record_signal
and rdap_lookup through the ordinary logged tools. Every number still comes from a tool. The
provider's clock is pinned to the case's as_of: in a live case the pulls happen after the case
is opened and, by the time rule, only count once the case is re-based to a later as_of.

Not wired into `tangible-demo seed` (its borrowers are generated from UK order lines); call
this on a case of your own. See docs/intangibles-plan.md, "v1 slice".
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tangible.agent.tools import ToolContext, call_tool, open_context
from tangible.db.cases import Case
from tangible.db.session import Conn
from tangible.db.signals import insert_application
from tangible.ledger.types import Application, ImportLine
from tangible.policy import Policy
from tangible.web.signals import PLACE_SOURCES, demo_signal_provider


@dataclass(frozen=True)
class UsBorrower:
    key: str
    story: str  # loyalty | stock | bought_reviews
    legal_name: str
    dba: str | None
    address: str
    state: str
    phone: str
    website: str
    naics: str
    requested_amount: Decimal
    owner_names: tuple[str, ...]
    # Stated imports (application v3, systemic-risk plan): None = not stated, () = none.
    import_mix: tuple[ImportLine, ...] | None = None

    @property
    def domain(self) -> str:
        return self.website.split("://", 1)[-1].strip("/").removeprefix("www.")

    def application(self, observed_at: datetime) -> Application:
        return Application(
            source_ref=f"application-{self.key}",
            stated_revenue_90d=None,
            own_account_names=(),
            owner_names=self.owner_names,
            observed_at=observed_at,
            legal_name=self.legal_name,
            dba=self.dba,
            address=self.address,
            phone=self.phone,
            website=self.website,
            state=self.state,
            naics=self.naics,
            requested_amount=self.requested_amount,
            import_mix=self.import_mix,
        )


US_BORROWERS: tuple[UsBorrower, ...] = (
    UsBorrower(
        "juniper_rye", "loyalty", "Juniper & Rye Bakery LLC", "Juniper & Rye",
        "2231 SE Division St, Portland", "OR", "(503) 555-0142", "https://juniperandrye.example",
        "311811", Decimal("60000"), ("Maya Lindqvist",),
        import_mix=(),  # a bakery buying local flour: states no imports
    ),
    UsBorrower(
        "copperline", "stock", "Copperline Hardware Supply Inc", None,
        "1450 Cleveland Ave, Columbus", "OH", "(614) 555-0187",
        "https://copperlinehardware.example", "444130", Decimal("80000"), ("Dev Ramaswamy",),
    ),
    UsBorrower(
        "northgate", "bought_reviews", "Northgate Phone Repair LLC", None,
        "4410 E Speedway Blvd, Tucson", "AZ", "(520) 555-0178",
        "https://northgatephonerepair.example", "811210", Decimal("40000"), ("Rico Valdez",),
        # Screens, batteries and parts from China: exposed to the demo tariff on HS 85.
        import_mix=(ImportLine("85", "CN", Decimal("0.7")),),
    ),
)  # fmt: skip

BY_KEY = {b.key: b for b in US_BORROWERS}


@dataclass(frozen=True)
class StagedCall:
    tool: str
    call_id: str
    args: dict[str, Any]
    error: str | None
    result: Any


def stage_public_signals(
    conn: Conn,
    case: Case,
    policy: Policy,
    workdir: Path,
    borrower: UsBorrower,
    *,
    fixtures: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> list[StagedCall]:
    """Write the borrower's application (observed a day before as_of) and run the scripted signal
    pre-run. Returns every tool call made, in order (each is also a tool_call row)."""
    insert_application(
        conn, case.tenant_id, case.borrower_id, borrower.application(case.as_of - timedelta(days=1))
    )
    provider = demo_signal_provider(fixtures, clock or (lambda: case.as_of))
    ctx = open_context(conn, case, policy, workdir, signal_provider=provider)
    calls: list[StagedCall] = []

    def call(tool: str, args: dict[str, Any]) -> StagedCall:
        r = call_tool(ctx, tool, args)
        c = StagedCall(tool, r.call_id, args, r.error, r.result)
        calls.append(c)
        log(f"  {tool} {args} -> {'error: ' + r.error if r.error else 'ok'}")
        return c

    def match_and_record(source_call: StagedCall) -> None:
        if source_call.error is not None:
            return
        for cand in source_call.result.get("candidates", []):
            m = call(
                "match_entity",
                {"source_call_id": source_call.call_id, "source_ref": cand["source_ref"]},
            )
            if m.error is None:
                # Below-threshold matches are refused here: that refusal is part of the story.
                call("record_signal", {"match_id": m.result["match_id"]})

    name = borrower.dba or borrower.legal_name
    for source in PLACE_SOURCES:
        match_and_record(call("place_search", {"source": source, "query": name}))
    match_and_record(call("rdap_lookup", {"domain": borrower.domain}))
    return calls


def stage_context(conn: Conn, case: Case, policy: Policy, workdir: Path) -> ToolContext:
    """A tool context with the US demo signal provider (for follow-up calls in a demo)."""
    return open_context(
        conn, case, policy, workdir, signal_provider=demo_signal_provider(clock=lambda: case.as_of)
    )
