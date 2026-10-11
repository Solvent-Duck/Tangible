"""Onboarding business logic: lender invites, the borrower's side, and the bridge to diligence.

Same conventions as `workbench.services`: every function takes a tenant-scoped connection and the
authenticated `Principal` (or, before a tenant is known, a token or an `Identity`), enforces
authorization itself and raises the workbench domain errors. Return values are JSON-safe dicts.

Invites and their events are append-only; an invite's status is derived from its events. A
borrower account sees only its own borrower's onboarding, never a case. Borrower-entered text is
untrusted data: it is stored and displayed, never interpreted.

"Connecting" a source is simulated in this build: the borrower walks through a consent step and a
`connection` row is written (source, scopes, granted now). No data is pulled; the real connectors
(`tangible.connectors`) take over from that row when they are wired to the UI.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from tangible.db.repo import create_borrower
from tangible.db.session import Conn, tenant_session
from tangible.db.signals import insert_application
from tangible.documents.upload import store_upload
from tangible.evidence.store import LocalEvidenceStore
from tangible.ledger.types import Application, ImportLine
from tangible.notify import mail
from tangible.onboarding import catalog
from tangible.workbench import audit
from tangible.workbench.auth import LENDER_ROLES, Identity, Principal
from tangible.workbench.services import (
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    _clock,
    _j,
    _jd,
    create_new_case,
)
from tangible.workbench.settings import Settings

INVITE_DAYS = 14
MAX_TEXT = 2000
MAX_FIELD = 200
# What a simulated connection is granted, per catalog item.
SCOPES: dict[str, tuple[str, ...]] = {
    "bank": ("transactions", "balances"),
    "accounting": ("invoices", "bills", "ledger"),
    "sales": ("orders", "refunds", "payouts"),
    "inventory_system": ("inventory",),
    "customers": ("orders", "customers"),
}
PROGRESS_EVENTS = ("source_connected", "document_added", "link_added", "answer_added")
PROFILE_FIELDS = (
    "legal_name", "dba", "website", "phone", "address", "state", "naics", "year_founded",
    "stated_revenue_90d",
)  # fmt: skip
STAGES = (
    "invited", "opened", "account_created", "connecting", "submitted", "in_diligence", "signed",
)  # fmt: skip


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _lender(p: Principal) -> None:
    if p.role not in LENDER_ROLES:
        raise Forbidden("only lender staff can do this")


def _admin(p: Principal) -> None:
    if p.role != "credit_admin":
        raise Forbidden("only a credit admin can do this")


def _borrower(p: Principal) -> str:
    if p.role != "borrower" or not p.borrower_id:
        raise Forbidden("only the invited business can do this")
    return p.borrower_id


def _text(value: Any, name: str, limit: int = MAX_FIELD, *, required: bool = False) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        if required:
            raise Invalid(f"{name} is required")
        return None
    if len(text) > limit:
        raise Invalid(f"{name} is longer than {limit} characters")
    return text


def _email(value: Any) -> str:
    text = _text(value, "email", 254, required=True)
    assert text
    local, at, domain = text.partition("@")
    if not at or not local or "." not in domain or any(c.isspace() for c in text):
        raise Invalid("email is not a valid address")
    return text


def _money(value: Any, name: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise Invalid(f"{name} must be a number") from None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1e12"):
        raise Invalid(f"{name} must be a positive amount")
    return amount.quantize(Decimal("0.01"))


def _url(value: Any, name: str = "link") -> str:
    text = _text(value, name, 500, required=True)
    assert text
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        raise Invalid(f"{name} must be a web address")
    return text


def _event(
    conn: Conn, tenant_id: str, invite_id: str, kind: str, actor: str | None,
    item_id: str | None = None, detail: dict[str, Any] | None = None,
) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO invite_event (tenant_id, invite_id, kind, item_id, detail, actor) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
        (tenant_id, invite_id, kind, item_id, json.dumps(detail or {}, default=str), actor),
    )


def _tenant_name(conn: Conn) -> str:
    row = conn.execute("SELECT name FROM tenant LIMIT 1").fetchone()
    return str(row[0]) if row else "Your lender"


def _money_text(amount: Decimal | None) -> str | None:
    return f"${amount:,.0f}" if amount is not None else None


# ---------------------------------------------------------------- invite rows and status


@dataclass(frozen=True)
class InviteRow:
    id: str
    tenant_id: str
    borrower_id: str
    email: str
    contact_name: str | None
    business_name: str
    loan_amount: Decimal | None
    message: str | None
    requests: dict[str, Any]
    catalog_version: str
    replaces_id: str | None
    created_by: str
    created_at: datetime
    expires_at: datetime


_INVITE_COLS = (
    "id, tenant_id, borrower_id, email, contact_name, business_name, loan_amount, message, "
    "requests, catalog_version, replaces_id, created_by, created_at, expires_at"
)


def _invite_from(row: tuple[Any, ...]) -> InviteRow:
    return InviteRow(
        str(row[0]), str(row[1]), str(row[2]), row[3], row[4], row[5], row[6], row[7], row[8],
        row[9], str(row[10]) if row[10] else None, row[11], row[12], row[13],
    )  # fmt: skip


def _uuid(value: str, what: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise NotFound(f"{what} {value} not found") from None


def _load_invite(conn: Conn, invite_id: str) -> InviteRow:
    invite_id = _uuid(invite_id, "invite")
    row = conn.execute(f"SELECT {_INVITE_COLS} FROM invite WHERE id = %s", (invite_id,)).fetchone()
    if row is None:
        raise NotFound(f"invite {invite_id} not found")
    return _invite_from(row)


def _events(conn: Conn, invite_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in invite_ids}
    if not invite_ids:
        return out
    for iid, kind, item_id, detail, actor, at in conn.execute(
        "SELECT invite_id, kind, item_id, detail, actor, at FROM invite_event "
        "WHERE invite_id = ANY(%s::uuid[]) ORDER BY at, id",
        (invite_ids,),
    ).fetchall():
        out[str(iid)].append(
            {"kind": kind, "item_id": item_id, "detail": detail, "actor": actor, "at": at}
        )
    return out


def _state(inv: InviteRow, events: list[dict[str, Any]], now: datetime) -> str:
    """The invite's own lifecycle: replaced, revoked, expired or live (an accepted invite does
    not expire; the borrower is already in)."""
    kinds = {e["kind"] for e in events}
    if "resent" in kinds:
        return "replaced"
    if "revoked" in kinds:
        return "revoked"
    if "accepted" not in kinds and now > inv.expires_at:
        return "expired"
    return "live"


def _stage(events: list[dict[str, Any]], case_status: str | None) -> str:
    """How far the borrower has come: invited -> ... -> signed. Derived, never stored."""
    if case_status == "signed":
        return "signed"
    kinds = {e["kind"] for e in events}
    if case_status is not None or "case_opened" in kinds:
        return "in_diligence"
    if "submitted" in kinds:
        return "submitted"
    if kinds & {"profile_saved", *PROGRESS_EVENTS}:
        return "connecting"
    if "accepted" in kinds:
        return "account_created"
    if "opened" in kinds:
        return "opened"
    return "invited"


def _latest_profile(conn: Conn, borrower_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(PROFILE_FIELDS)}, import_mix, observed_at FROM borrower_profile "
        "WHERE borrower_id = %s ORDER BY observed_at DESC, id DESC LIMIT 1",
        (borrower_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (*PROFILE_FIELDS, "import_mix", "observed_at")
    out: dict[str, Any] = _j(dict(zip(keys, row, strict=True)))
    return out


def _coverage(
    inv: InviteRow, events: list[dict[str, Any]], profile: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Each requested item with whether it has been answered, and how."""
    answered: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e["kind"] in PROGRESS_EVENTS and e["item_id"]:
            answered.setdefault(e["item_id"], []).append(
                {"kind": e["kind"], "detail": e["detail"], "at": _j(e["at"])}
            )
    out: list[dict[str, Any]] = []
    for item in catalog.requested_items(inv.requests):
        responses = answered.get(item["id"], [])
        done = bool(responses)
        if not done and profile:
            if item["id"] == "entity":
                done = bool(profile.get("legal_name") and profile.get("state"))
            elif item["id"] == "year_founded":
                done = profile.get("year_founded") is not None
            elif item["id"] == "website":
                done = bool(profile.get("website"))
            elif item["id"] == "import_mix":
                done = profile.get("import_mix") is not None
        out.append({**item, "done": done, "responses": responses})
    return out


def _case_for_borrower(conn: Conn, borrower_id: str) -> tuple[str, str] | None:
    """(case id, status) of the borrower's newest case; 'signed' if it has an assessment."""
    row = conn.execute(
        "SELECT c.id, CASE WHEN EXISTS (SELECT 1 FROM assessment a WHERE a.case_id = c.id) "
        "THEN 'signed' ELSE c.status END FROM case_file c WHERE c.borrower_id = %s "
        "ORDER BY c.created_at DESC, c.id DESC LIMIT 1",
        (borrower_id,),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _summary(
    conn: Conn, inv: InviteRow, events: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    profile = _latest_profile(conn, inv.borrower_id)
    cov = _coverage(inv, events, profile)
    case = _case_for_borrower(conn, inv.borrower_id)
    last = events[-1] if events else None
    out: dict[str, Any] = _j(
        {
            "id": inv.id,
            "borrower_id": inv.borrower_id,
            "business_name": inv.business_name,
            "email": inv.email,
            "contact_name": inv.contact_name,
            "loan_amount": inv.loan_amount,
            "created_by": inv.created_by,
            "created_at": inv.created_at,
            "expires_at": inv.expires_at,
            "state": _state(inv, events, now),
            "stage": _stage(events, case[1] if case else None),
            "requested": len(cov),
            "answered": sum(1 for c in cov if c["done"]),
            "groups": sorted({c["group"] for c in cov}),
            "case_id": case[0] if case else None,
            "case_status": case[1] if case else None,
            "last_activity": last["at"] if last else inv.created_at,
            "last_event": last["kind"] if last else None,
        }
    )
    return out


# ---------------------------------------------------------------- lender side


def get_catalog() -> dict[str, Any]:
    return catalog.catalog_view()


def _render_invite(
    inv_name: str, lender: str, contact: str | None, amount: Decimal | None, message: str | None,
    requests: dict[str, Any], link: str, expires_at: datetime,
) -> tuple[str, str, str]:  # fmt: skip
    groups: list[dict[str, Any]] = []
    labels = {g.id: g.label for g in catalog.GROUPS} | {"custom": f"Also requested by {lender}"}
    for item in catalog.requested_items(requests):
        if not groups or groups[-1]["id"] != item["group"]:
            groups.append({"id": item["group"], "label": labels[item["group"]], "entries": []})
        groups[-1]["entries"].append(item)
    subject = f"{lender} invited {inv_name} to share business information"
    text, html = mail.render(
        "invite", subject=subject, lender_name=lender, business_name=inv_name,
        contact_name=contact, loan_amount=_money_text(amount), message=message, groups=groups,
        link=link, expires_on=expires_at.strftime("%-d %B %Y"),
    )  # fmt: skip
    return subject, text, html


def preview_invite_email(conn: Conn, p: Principal, body: dict[str, Any]) -> dict[str, Any]:
    """The email the composer would send, rendered with a placeholder link. Nothing is stored."""
    _admin(p)
    try:
        requests = catalog.resolve_requests(list(body.get("items") or []), body.get("custom"))
    except catalog.CatalogError as e:
        raise Invalid(str(e)) from None
    subject, text, html = _render_invite(
        _text(body.get("business_name"), "business name") or "Your business",
        _tenant_name(conn), _text(body.get("contact_name"), "contact name"),
        _money(body.get("loan_amount"), "loan amount"),
        _text(body.get("message"), "message", MAX_TEXT), requests,
        "https://app.tangible.test/invite/…", _clock(conn) + timedelta(days=INVITE_DAYS),
    )  # fmt: skip
    return {"subject": subject, "body_text": text, "body_html": html}


def _issue(
    conn: Conn, settings: Settings, p: Principal, *, borrower_id: str, email: str,
    contact: str | None, business: str, amount: Decimal | None, message: str | None,
    requests: dict[str, Any], replaces: str | None,
) -> dict[str, Any]:  # fmt: skip
    token = new_token()
    now = _clock(conn)
    expires = now + timedelta(days=settings.invite_days)
    row = conn.execute(
        "INSERT INTO invite (tenant_id, borrower_id, email, contact_name, business_name, "
        "loan_amount, message, requests, catalog_version, token_hash, replaces_id, created_by, "
        "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (p.tenant_id, borrower_id, email, contact, business, amount, message,
         json.dumps(requests), catalog.CURRENT_VERSION, hash_token(token), replaces, p.subject,
         expires),
    ).fetchone()  # fmt: skip
    assert row
    invite_id = str(row[0])
    link = f"{settings.public_url.rstrip('/')}/invite/{token}"
    subject, text, html = _render_invite(
        business, _tenant_name(conn), contact, amount, message, requests, link, expires
    )
    email_id = mail.send(
        conn, mail.mailer_for(settings.smtp_url), p.tenant_id,
        mail.Email(email, subject, text, html), invite_id=invite_id,
    )  # fmt: skip
    _event(conn, p.tenant_id, invite_id, "sent", p.subject, detail={"email_id": email_id})
    audit.record(conn, p, "invite", "invite", invite_id, {"borrower_id": borrower_id})
    return {"id": invite_id, "borrower_id": borrower_id, "email_id": email_id, "link": link}


def create_invite(
    conn: Conn, settings: Settings, p: Principal, body: dict[str, Any]
) -> dict[str, Any]:
    """Invite a business: create its borrower record (or reuse `borrower_id`), store the invite,
    and send the email. The response carries the link once, for the lender to copy."""
    _admin(p)
    email = _email(body.get("email"))
    business = _text(body.get("business_name"), "business name", required=True)
    assert business
    try:
        requests = catalog.resolve_requests(list(body.get("items") or []), body.get("custom"))
    except catalog.CatalogError as e:
        raise Invalid(str(e)) from None
    borrower_id = body.get("borrower_id")
    if borrower_id:
        borrower_id = _uuid(borrower_id, "borrower")
        if conn.execute("SELECT 1 FROM borrower WHERE id = %s", (borrower_id,)).fetchone() is None:
            raise NotFound(f"borrower {borrower_id} not found")
    else:
        borrower_id = create_borrower(conn, p.tenant_id, business)
    return _issue(
        conn, settings, p, borrower_id=str(borrower_id), email=email,
        contact=_text(body.get("contact_name"), "contact name"), business=business,
        amount=_money(body.get("loan_amount"), "loan amount"),
        message=_text(body.get("message"), "message", MAX_TEXT), requests=requests, replaces=None,
    )  # fmt: skip


def list_invites(conn: Conn, p: Principal) -> list[dict[str, Any]]:
    """Every invite that has not been replaced by a re-send, newest first."""
    _lender(p)
    rows = conn.execute(
        f"SELECT {_INVITE_COLS} FROM invite i WHERE NOT EXISTS "
        "(SELECT 1 FROM invite n WHERE n.replaces_id = i.id) ORDER BY created_at DESC, id"
    ).fetchall()
    invites = [_invite_from(r) for r in rows]
    events = _events(conn, [i.id for i in invites])
    now = _clock(conn)
    return [_summary(conn, i, events[i.id], now) for i in invites]


def invite_detail(conn: Conn, p: Principal, invite_id: str) -> dict[str, Any]:
    _lender(p)
    inv = _load_invite(conn, invite_id)
    events = _events(conn, [inv.id])[inv.id]
    now = _clock(conn)
    out = _summary(conn, inv, events, now)
    profile = _latest_profile(conn, inv.borrower_id)
    email = conn.execute(
        "SELECT id, to_addr, subject, body_text, body_html, transport, created_at, delivered_at, "
        "error FROM outbound_email WHERE invite_id = %s ORDER BY created_at DESC LIMIT 1",
        (inv.id,),
    ).fetchone()
    out.update(
        _j(
            {
                "message": inv.message,
                "catalog_version": inv.catalog_version,
                "replaces_id": inv.replaces_id,
                "items": _coverage(inv, events, profile),
                "profile": profile,
                "events": events,
                "sent_email": dict(
                    zip(
                        (
                            "id",
                            "to_addr",
                            "subject",
                            "body_text",
                            "body_html",
                            "transport",
                            "created_at",
                            "delivered_at",
                            "error",
                        ),
                        email,
                        strict=True,
                    )  # fmt: skip
                )
                if email
                else None,
                "connections": [
                    {"source": s, "scopes": sc, "granted_at": g, "revoked_at": r}
                    for s, sc, g, r in conn.execute(
                        "SELECT source, scopes, granted_at, revoked_at FROM connection "
                        "WHERE borrower_id = %s ORDER BY granted_at",
                        (inv.borrower_id,),
                    ).fetchall()
                ],
            }
        )
    )
    return out


def _live_invite(conn: Conn, invite_id: str) -> InviteRow:
    inv = _load_invite(conn, invite_id)
    state = _state(inv, _events(conn, [inv.id])[inv.id], _clock(conn))
    if state in ("replaced", "revoked"):
        raise Conflict(f"this invite was {state}")
    return inv


def resend_invite(conn: Conn, settings: Settings, p: Principal, invite_id: str) -> dict[str, Any]:
    """A fresh link: a new invite (same requests) that replaces this one. The old link stops
    working; anything the borrower already did stays with the borrower."""
    _admin(p)
    old = _live_invite(conn, invite_id)
    out = _issue(
        conn, settings, p, borrower_id=old.borrower_id, email=old.email, contact=old.contact_name,
        business=old.business_name, amount=old.loan_amount, message=old.message,
        requests=old.requests, replaces=old.id,
    )  # fmt: skip
    _event(conn, p.tenant_id, old.id, "resent", p.subject, detail={"new_invite_id": out["id"]})
    return out


def revoke_invite(conn: Conn, p: Principal, invite_id: str, reason: str = "") -> dict[str, Any]:
    _admin(p)
    inv = _live_invite(conn, invite_id)
    _event(conn, p.tenant_id, inv.id, "revoked", p.subject, detail={"reason": reason.strip()})
    audit.record(conn, p, "revoke", "invite", inv.id, {"reason": reason.strip()})
    return {"id": inv.id, "state": "revoked"}


def open_case(
    conn: Conn, p: Principal, invite_id: str, assigned_analyst: str | None = None
) -> dict[str, Any]:
    """Start diligence on a borrower who has submitted. The case is dated now, so everything the
    borrower connected or uploaded up to this moment is visible to it (the time rule holds)."""
    _admin(p)
    inv = _load_invite(conn, invite_id)
    events = _events(conn, [inv.id])[inv.id]
    if "submitted" not in {e["kind"] for e in events}:
        raise Conflict("the business has not submitted yet")
    if _case_for_borrower(conn, inv.borrower_id) is not None:
        raise Conflict("a case already exists for this business")
    case = create_new_case(
        conn, p, borrower_id=inv.borrower_id, as_of=_clock(conn), loan_amount=inv.loan_amount,
        assigned_analyst=assigned_analyst or None,
    )  # fmt: skip
    _event(conn, p.tenant_id, inv.id, "case_opened", p.subject, detail={"case_id": case["id"]})
    return case


def dashboard(conn: Conn, p: Principal) -> dict[str, Any]:
    """KPIs, the pipeline (one row per live invite) and recent activity."""
    _lender(p)
    pipeline = list_invites(conn, p)
    now = _clock(conn)
    live = [r for r in pipeline if r["state"] == "live"]
    by_stage = {s: sum(1 for r in live if r["stage"] == s) for s in STAGES}
    signed_30d = conn.execute(
        "SELECT count(*) FROM assessment WHERE created_at >= %s", (now - timedelta(days=30),)
    ).fetchone()
    in_diligence = conn.execute(
        "SELECT count(*) FROM case_file c WHERE c.status NOT IN ('signed', 'closed') "
        "AND NOT EXISTS (SELECT 1 FROM assessment a WHERE a.case_id = c.id)"
    ).fetchone()
    activity = conn.execute(
        "SELECT e.kind, e.item_id, e.actor, e.at, i.id, i.business_name, e.detail "
        "FROM invite_event e JOIN invite i ON i.id = e.invite_id "
        "ORDER BY e.at DESC, e.id DESC LIMIT 25"
    ).fetchall()
    return _jd(
        {
            "lender_name": _tenant_name(conn),
            "kpis": {
                "invited": len(live),
                "awaiting_borrower": by_stage["invited"]
                + by_stage["opened"]
                + by_stage["account_created"]
                + by_stage["connecting"],
                "ready_for_diligence": by_stage["submitted"],
                "in_diligence": in_diligence[0] if in_diligence else 0,
                "signed_30d": signed_30d[0] if signed_30d else 0,
            },
            "by_stage": by_stage,
            "pipeline": pipeline,
            "activity": [
                {
                    "kind": k,
                    "item_id": it,
                    "actor": a,
                    "at": at,
                    "invite_id": iid,
                    "business_name": b,
                    "detail": d,
                }
                for k, it, a, at, iid, b, d in activity
            ],  # fmt: skip
        }
    )


def list_outbox(conn: Conn, p: Principal) -> list[dict[str, Any]]:
    _lender(p)
    rows = conn.execute(
        "SELECT id, invite_id, to_addr, subject, transport, created_at, delivered_at, error "
        "FROM outbound_email ORDER BY created_at DESC, id LIMIT 200"
    ).fetchall()
    keys = ("id", "invite_id", "to_addr", "subject", "transport", "created_at", "delivered_at",
            "error")  # fmt: skip
    return [_j(dict(zip(keys, r, strict=True))) for r in rows]


def get_email(conn: Conn, p: Principal, email_id: str) -> dict[str, Any]:
    _lender(p)
    row = conn.execute(
        "SELECT id, invite_id, to_addr, subject, body_text, body_html, transport, created_at, "
        "delivered_at, error FROM outbound_email WHERE id = %s",
        (_uuid(email_id, "email"),),
    ).fetchone()
    if row is None:
        raise NotFound(f"email {email_id} not found")
    keys = ("id", "invite_id", "to_addr", "subject", "body_text", "body_html", "transport",
            "created_at", "delivered_at", "error")  # fmt: skip
    out: dict[str, Any] = _j(dict(zip(keys, row, strict=True)))
    return out


# ---------------------------------------------------------------- invite link (no tenant yet)


def resolve_token(conninfo: str, token: str) -> tuple[str, str]:
    """(invite id, tenant id) for a raw invite token, through the SECURITY DEFINER lookup."""
    if not token or len(token) > 200:
        raise NotFound("invite not found")
    with tenant_session(conninfo, "") as conn:
        row = conn.execute("SELECT * FROM invite_by_token(%s)", (hash_token(token),)).fetchone()
    if row is None:
        raise NotFound("invite not found")
    return str(row[0]), str(row[1])


def preview_invite(conninfo: str, token: str) -> dict[str, Any]:
    """What the invite page shows before sign-up: who is asking, for which business, and what.
    Public (the token is the credential), so it returns nothing else. The first view records an
    'opened' event."""
    invite_id, tenant_id = resolve_token(conninfo, token)
    with tenant_session(conninfo, tenant_id) as conn:
        inv = _load_invite(conn, invite_id)
        events = _events(conn, [inv.id])[inv.id]
        state = _state(inv, events, _clock(conn))
        if state == "live" and not any(e["kind"] == "opened" for e in events):
            _event(conn, tenant_id, inv.id, "opened", None)
        return _jd(
            {
                "state": state,
                "lender_name": _tenant_name(conn),
                "business_name": inv.business_name,
                "contact_name": inv.contact_name,
                "email": inv.email,
                "loan_amount": inv.loan_amount,
                "message": inv.message,
                "expires_at": inv.expires_at,
                "items": catalog.requested_items(inv.requests),
                "groups": [{"id": g.id, "label": g.label} for g in catalog.GROUPS],
            }
        )


def accept_invite(conninfo: str, token: str, identity: Identity) -> dict[str, Any]:
    """Turn a signed-up identity into the borrower's user in the lender's tenant. Idempotent for
    the same subject. A subject that is already staff in this tenant cannot also be the borrower,
    and a borrower user belongs to exactly one borrower."""
    invite_id, tenant_id = resolve_token(conninfo, token)
    with tenant_session(conninfo, tenant_id) as conn:
        inv = _load_invite(conn, invite_id)
        events = _events(conn, [inv.id])[inv.id]
        state = _state(inv, events, _clock(conn))
        if state != "live":
            raise Conflict(f"this invite is {state}; ask the lender for a new one")
        row = conn.execute(
            "SELECT role, borrower_id FROM app_user WHERE subject = %s", (identity.subject,)
        ).fetchone()
        if row is not None:
            if row[0] != "borrower" or str(row[1]) != inv.borrower_id:
                raise Conflict("this account already belongs to someone else in this lender")
        else:
            conn.execute(
                "INSERT INTO app_user (tenant_id, subject, email, display_name, role, "
                "borrower_id) VALUES (%s, %s, %s, %s, 'borrower', %s)",
                (tenant_id, identity.subject, identity.email or inv.email,
                 inv.contact_name, inv.borrower_id),
            )  # fmt: skip
        if not any(e["kind"] == "accepted" and e["actor"] == identity.subject for e in events):
            _event(conn, tenant_id, inv.id, "accepted", identity.subject,
                   detail={"email": identity.email})  # fmt: skip
        return {"tenant_id": tenant_id, "borrower_id": inv.borrower_id, "invite_id": inv.id}


# ---------------------------------------------------------------- borrower side


def _current_invite(conn: Conn, borrower_id: str) -> InviteRow:
    """The borrower's newest invite that has not been replaced or revoked."""
    rows = conn.execute(
        f"SELECT {_INVITE_COLS} FROM invite i WHERE borrower_id = %s AND NOT EXISTS "
        "(SELECT 1 FROM invite n WHERE n.replaces_id = i.id) ORDER BY created_at DESC, id DESC",
        (borrower_id,),
    ).fetchall()
    now = _clock(conn)
    for r in rows:
        inv = _invite_from(r)
        if _state(inv, _events(conn, [inv.id])[inv.id], now) != "revoked":
            return inv
    raise NotFound("there is no open invite for this business")


def onboarding_state(conn: Conn, p: Principal) -> dict[str, Any]:
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    events = _events(conn, [inv.id])[inv.id]
    profile = _latest_profile(conn, borrower_id)
    items = _coverage(inv, events, profile)
    submitted = next((e for e in events if e["kind"] == "submitted"), None)
    return _jd(
        {
            "invite_id": inv.id,
            "lender_name": _tenant_name(conn),
            "business_name": inv.business_name,
            "loan_amount": inv.loan_amount,
            "message": inv.message,
            "profile": profile,
            "items": items,
            "groups": [{"id": g.id, "label": g.label, "blurb": g.blurb} for g in catalog.GROUPS],
            "submitted_at": submitted["at"] if submitted else None,
            "can_submit": _missing_for_submit(profile, items) == [],
            "missing_for_submit": _missing_for_submit(profile, items),
        }
    )


def _missing_for_submit(profile: dict[str, Any] | None, items: list[dict[str, Any]]) -> list[str]:
    """Submission needs the business profile and at least one way to see the bank: a connected
    bank or uploaded statements. Everything else is shown to the lender as coverage."""
    missing: list[str] = []
    if not profile or not profile.get("legal_name"):
        missing.append("business profile")
    done = {i["id"] for i in items if i["done"]}
    if not done & {"bank", "bank_statements"}:
        missing.append("bank connection or bank statements")
    return missing


MAX_IMPORT_LINES = 20


def _import_mix(raw: Any) -> list[ImportLine] | None:
    """The stated import mix: None (not stated), [] (imports nothing) or up to 20 lines of
    {hs_chapter: two digits, country: ISO alpha-2, share_of_cogs: 0..1}, summing to at most 1."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_IMPORT_LINES:
        raise Invalid(f"imports must be a list of at most {MAX_IMPORT_LINES} lines")
    lines: list[ImportLine] = []
    for n, x in enumerate(raw, start=1):
        if not isinstance(x, dict):
            raise Invalid(f"import line {n} is not valid")
        hs = str(x.get("hs_chapter", "")).strip().zfill(2)
        country = str(x.get("country", "")).strip().upper()
        if not (len(hs) == 2 and hs.isdigit() and hs != "00"):
            raise Invalid(f"import line {n}: the HS chapter must be two digits (01-99)")
        if not (len(country) == 2 and country.isalpha() and country.isascii()):
            raise Invalid(f"import line {n}: the country must be a two-letter code")
        try:
            share = Decimal(str(x.get("share_of_cogs", "")).strip())
        except InvalidOperation:
            raise Invalid(f"import line {n}: the share must be a number") from None
        if not share.is_finite() or not Decimal(0) < share <= 1:
            raise Invalid(f"import line {n}: the share must be above 0 and at most 100%")
        lines.append(ImportLine(hs, country, share.quantize(Decimal("0.0001"))))
    if sum((x.share_of_cogs for x in lines), Decimal(0)) > 1:
        raise Invalid("import shares add up to more than 100% of cost of goods")
    return lines


def save_profile(conn: Conn, p: Principal, body: dict[str, Any]) -> dict[str, Any]:
    """A new profile row (a correction never overwrites). Untrusted text, length-limited.

    Also appends an `application` ledger row (source_ref `onboarding:<borrower>`), observed now:
    the identity fields drive public-signal matching and the import mix drives the tariff stress.
    A later save is a revision with the same source_ref, so a case sees what was stated by its
    as_of date (the time rule)."""
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    year = body.get("year_founded")
    year_founded: int | None = None
    if year not in (None, ""):
        try:
            year_founded = int(str(year).strip())
        except ValueError:
            raise Invalid("year founded must be a year") from None
        if not 1800 <= year_founded <= _clock(conn).year:
            raise Invalid("year founded is out of range")
    state = _text(body.get("state"), "state", 2)
    values: dict[str, Any] = {
        "legal_name": _text(body.get("legal_name"), "legal name", required=True),
        "dba": _text(body.get("dba"), "DBA"),
        "website": _url(body["website"], "website") if body.get("website") else None,
        "phone": _text(body.get("phone"), "phone", 40),
        "address": _text(body.get("address"), "address", 300),
        "state": state.upper() if state else None,
        "naics": _text(body.get("naics"), "NAICS code", 6),
        "year_founded": year_founded,
        "stated_revenue_90d": _money(body.get("stated_revenue_90d"), "revenue"),
    }
    imports = _import_mix(body.get("import_mix"))
    mix_json = (
        None
        if imports is None
        else json.dumps(
            [
                {
                    "hs_chapter": x.hs_chapter,
                    "country": x.country,
                    "share_of_cogs": str(x.share_of_cogs),
                }
                for x in imports
            ]
        )  # fmt: skip
    )
    observed = _clock(conn)
    conn.execute(
        f"INSERT INTO borrower_profile (tenant_id, borrower_id, {', '.join(PROFILE_FIELDS)}, "
        f"import_mix, observed_at, submitted_by) VALUES (%s, %s, "
        f"{', '.join(['%s'] * len(PROFILE_FIELDS))}, %s::jsonb, %s, %s)",
        (p.tenant_id, borrower_id, *(values[f] for f in PROFILE_FIELDS), mix_json, observed,
         p.subject),
    )  # fmt: skip
    insert_application(
        conn, p.tenant_id, borrower_id,
        Application(
            source_ref=f"onboarding:{borrower_id}",
            stated_revenue_90d=values["stated_revenue_90d"],
            own_account_names=(),
            owner_names=(),
            observed_at=observed,
            legal_name=values["legal_name"],
            dba=values["dba"],
            address=values["address"],
            phone=values["phone"],
            website=values["website"],
            state=values["state"],
            naics=values["naics"],
            requested_amount=inv.loan_amount,
            import_mix=tuple(imports) if imports is not None else None,
        ),
    )  # fmt: skip
    _event(conn, p.tenant_id, inv.id, "profile_saved", p.subject)
    return onboarding_state(conn, p)


def _requested(inv: InviteRow, item_id: str, *kinds: str) -> dict[str, Any]:
    for item in catalog.requested_items(inv.requests):
        if item["id"] == item_id:
            if item["kind"] not in kinds:
                raise Invalid(f"{item['label']} is not answered this way")
            return item
    raise NotFound(f"{item_id!r} was not requested")


def _not_submitted(conn: Conn, inv: InviteRow) -> None:
    if any(e["kind"] == "submitted" for e in _events(conn, [inv.id])[inv.id]):
        raise Conflict("already submitted; ask the lender to send a new invite to add more")


def connect_source(conn: Conn, p: Principal, item_id: str, provider: str) -> dict[str, Any]:
    """Simulated connection: record consent as a `connection` row (granted now, read-only scopes).
    No data is pulled in this build."""
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    _not_submitted(conn, inv)
    item = _requested(inv, item_id, "connector")
    if provider not in item["providers"]:
        raise Invalid(f"{provider!r} is not one of {', '.join(item['providers'])}")
    scopes = list(SCOPES.get(item_id, ()))
    now = _clock(conn)
    row = conn.execute(
        "INSERT INTO connection (tenant_id, borrower_id, source, granted_at, scopes) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (p.tenant_id, borrower_id, provider, now, scopes),
    ).fetchone()
    assert row
    _event(conn, p.tenant_id, inv.id, "source_connected", p.subject, item_id,
           {"provider": provider, "connection_id": str(row[0]), "scopes": scopes,
            "simulated": True})  # fmt: skip
    return onboarding_state(conn, p)


def add_document(
    conn: Conn, settings: Settings, p: Principal, item_id: str, filename: str, data: bytes
) -> dict[str, Any]:
    """Store an upload as write-once evidence for the borrower (no case yet). It is observed now,
    so a case opened later sees it; it is read when diligence starts."""
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    _not_submitted(conn, inv)
    _requested(inv, item_id, "upload")
    if not data:
        raise Invalid("the file is empty")
    if len(data) > settings.max_upload_bytes:
        raise Invalid(f"the file is larger than {settings.max_upload_bytes} bytes")
    pull_id = store_upload(
        conn, LocalEvidenceStore(settings.evidence_dir), p.tenant_id, borrower_id,
        filename or "upload", data,
    )  # fmt: skip
    detail = {"pull_id": pull_id, "filename": (filename or "upload")[:120], "bytes": len(data)}
    _event(conn, p.tenant_id, inv.id, "document_added", p.subject, item_id, detail)
    return onboarding_state(conn, p)


def add_answer(conn: Conn, p: Principal, item_id: str, value: str) -> dict[str, Any]:
    """A link (for link items) or a short text answer (for lender-defined field items)."""
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    _not_submitted(conn, inv)
    item = _requested(inv, item_id, "link", "field")
    if item["kind"] == "link":
        _event(conn, p.tenant_id, inv.id, "link_added", p.subject, item_id, {"url": _url(value)})
    elif item["group"] == "custom":
        answer = _text(value, "answer", MAX_TEXT, required=True)
        _event(conn, p.tenant_id, inv.id, "answer_added", p.subject, item_id, {"text": answer})
    else:
        raise Invalid(f"{item['label']} is answered in the business profile")
    return onboarding_state(conn, p)


def submit(conn: Conn, p: Principal) -> dict[str, Any]:
    borrower_id = _borrower(p)
    inv = _current_invite(conn, borrower_id)
    _not_submitted(conn, inv)
    state = onboarding_state(conn, p)
    if state["missing_for_submit"]:
        raise Conflict("still needed: " + ", ".join(state["missing_for_submit"]))
    _event(conn, p.tenant_id, inv.id, "submitted", p.subject,
           detail={"answered": sum(1 for i in state["items"] if i["done"]),
                   "requested": len(state["items"])})  # fmt: skip
    return onboarding_state(conn, p)
