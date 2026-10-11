"""Applications, entity matches and public signals (migration 0009). Use a `tenant_session`.

Every write here is an append: entity_match is written only by the match_entity tool and
public_signal only by record_signal. Analyst confirmation of a match is an underwriter_action
(confirm_match / reject_match) beside it; the latest such action wins.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from tangible.db.session import Conn
from tangible.ledger.evidence import EvidenceRef
from tangible.ledger.types import Application, ImportLine, PublicSignal

# entity_match v1: a match at or above this score needs no analyst confirmation.
MATCH_THRESHOLD = Decimal("0.9")

MatchReview = Literal["confirm_match", "reject_match"]


def insert_application(conn: Conn, tenant_id: str, borrower_id: str, app: Application) -> str:
    """Append one application row (a correction is a new row with the same source_ref)."""
    row = conn.execute(
        "INSERT INTO application (tenant_id, borrower_id, source_ref, stated_revenue_90d, "
        "own_account_names, owner_names, legal_name, dba, address, phone, website, state, naics, "
        "requested_amount, observed_at, evidence_pull_id, evidence_locator, import_mix) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING id",
        (
            tenant_id, borrower_id, app.source_ref, app.stated_revenue_90d,
            list(app.own_account_names), list(app.owner_names), app.legal_name, app.dba,
            app.address, app.phone, app.website, app.state, app.naics, app.requested_amount,
            app.observed_at, app.evidence.pull_id if app.evidence else None,
            app.evidence.locator if app.evidence else None, _import_mix_json(app),
        ),
    ).fetchone()  # fmt: skip
    assert row
    return str(row[0])


def load_applications(conn: Conn, borrower_id: str) -> tuple[Application, ...]:
    rows = conn.execute(
        "SELECT source_ref, stated_revenue_90d, own_account_names, owner_names, observed_at, "
        "evidence_pull_id, evidence_locator, legal_name, dba, address, phone, website, state, "
        "naics, requested_amount, import_mix FROM application WHERE borrower_id = %s "
        "ORDER BY observed_at, source_ref, id",
        (borrower_id,),
    ).fetchall()
    return tuple(
        Application(
            r[0],
            r[1],
            tuple(r[2]),
            tuple(r[3]),
            r[4],
            EvidenceRef(str(r[5]), r[6] or "application") if r[5] is not None else None,
            r[7],
            r[8],
            r[9],
            r[10],
            r[11],
            r[12],
            r[13],
            r[14],
            _import_mix(r[15]),
        )  # fmt: skip
        for r in rows
    )


def _import_mix_json(app: Application) -> str | None:
    if app.import_mix is None:
        return None
    return json.dumps(
        [
            {
                "hs_chapter": x.hs_chapter,
                "country": x.country,
                "share_of_cogs": str(x.share_of_cogs),
            }
            for x in app.import_mix
        ]
    )


def _import_mix(raw: Any) -> tuple[ImportLine, ...] | None:
    if raw is None:
        return None
    return tuple(
        ImportLine(str(x["hs_chapter"]), str(x["country"]), Decimal(str(x["share_of_cogs"])))
        for x in raw
    )


def match_ok(score: Decimal | None, review: str | None) -> bool:
    """A match counts when an analyst confirmed it, or it scored >= the threshold and no analyst
    rejected it."""
    if review == "confirm_match":
        return True
    if review == "reject_match" or score is None:
        return False
    return score >= MATCH_THRESHOLD


_LATEST_REVIEW = (
    "(SELECT ua.action FROM underwriter_action ua WHERE ua.entity_match_id = {col} "
    "AND ua.action IN ('confirm_match', 'reject_match') ORDER BY ua.at DESC, ua.id DESC LIMIT 1)"
)


def load_public_signals(conn: Conn, borrower_id: str) -> tuple[PublicSignal, ...]:
    """Every signal row of every revision, each with its match score and resolved `match_ok`."""
    review = _LATEST_REVIEW.format(col="ps.entity_match_id")
    rows = conn.execute(
        "SELECT ps.source, ps.source_ref, ps.kind, ps.payload, ps.observed_at, ps.occurred_at, "
        f"ps.url, ps.sha256, em.score, {review}, ps.evidence_pull_id "
        "FROM public_signal ps LEFT JOIN entity_match em ON em.id = ps.entity_match_id "
        "WHERE ps.borrower_id = %s ORDER BY ps.observed_at, ps.source, ps.source_ref, ps.id",
        (borrower_id,),
    ).fetchall()
    return tuple(
        PublicSignal(
            source=r[0],
            source_ref=r[1],
            kind=r[2],
            payload_json=canonical_payload(r[3]),
            observed_at=r[4],
            occurred_at=r[5],
            url=r[6],
            sha256=r[7],
            match_score=r[8],
            match_ok=match_ok(r[8], r[9]),
            evidence=EvidenceRef(str(r[10]), "signal") if r[10] is not None else None,
        )  # fmt: skip
        for r in rows
    )


def canonical_payload(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class MatchRow:
    id: str
    case_id: str
    source: str
    source_ref: str
    source_call_id: str
    candidate: dict[str, Any]
    score: Decimal
    matcher_version: int
    review: MatchReview | None

    @property
    def ok(self) -> bool:
        return match_ok(self.score, self.review)


def get_match(conn: Conn, match_id: str) -> MatchRow | None:
    review = _LATEST_REVIEW.format(col="em.id")
    r = conn.execute(
        "SELECT em.id, em.case_id, em.source, em.source_ref, em.source_call_id, em.candidate, "
        f"em.score, em.matcher_version, {review} FROM entity_match em WHERE em.id = %s",
        (match_id,),
    ).fetchone()
    if r is None:
        return None
    return MatchRow(str(r[0]), str(r[1]), r[2], r[3], str(r[4]), r[5], r[6], r[7], r[8])


def insert_match(
    conn: Conn,
    *,
    match_id: str,
    tenant_id: str,
    case_id: str,
    source: str,
    source_ref: str,
    source_call_id: str,
    candidate: dict[str, Any],
    components: dict[str, Any],
    score: Decimal,
    matcher_version: int,
    tool_call_id: str,
) -> None:
    conn.execute(
        "INSERT INTO entity_match (id, tenant_id, case_id, source, source_ref, source_call_id, "
        "candidate, components, score, matcher_version, tool_call_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
        (
            match_id, tenant_id, case_id, source, source_ref, source_call_id,
            canonical_payload(candidate), canonical_payload(components), score, matcher_version,
            tool_call_id,
        ),
    )  # fmt: skip


def review_match(
    conn: Conn, tenant_id: str, match_id: str, user_id: str, action: MatchReview, reason: str
) -> str:
    """Record an analyst's confirm_match / reject_match beside the match (append-only)."""
    row = conn.execute(
        "INSERT INTO underwriter_action (tenant_id, entity_match_id, user_id, action, reason) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, match_id, user_id, action, reason),
    ).fetchone()
    assert row
    return str(row[0])


def insert_signal(
    conn: Conn,
    *,
    signal_id: str,
    tenant_id: str,
    borrower_id: str,
    case_id: str | None,
    signal: PublicSignal,
    entity_match_id: str,
    tool_call_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO public_signal (id, tenant_id, borrower_id, case_id, source, source_ref, kind, "
        "payload, url, sha256, entity_match_id, tool_call_id, occurred_at, observed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
        (
            signal_id, tenant_id, borrower_id, case_id, signal.source, signal.source_ref,
            signal.kind, signal.payload_json, signal.url, signal.sha256, entity_match_id,
            tool_call_id, signal.occurred_at, signal.observed_at,
        ),
    )  # fmt: skip
