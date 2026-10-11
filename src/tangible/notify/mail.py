"""Outgoing email. Non-production: every message is written to the `outbound_email` outbox, and
the lender UI shows it. `SmtpMailer` additionally delivers it over SMTP (e.g. a local Mailpit at
smtp://127.0.0.1:1025) so the email can be seen in a real client.

The outbox body holds the invite link. That is acceptable for a non-production outbox (the row is
tenant-scoped under RLS); a production transport would redact the body once delivered.
"""

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tangible.db.session import Conn

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
    trim_blocks=True,
    lstrip_blocks=True,
)
FROM_ADDR = "Tangible <no-reply@tangible.test>"


@dataclass(frozen=True)
class Email:
    to_addr: str
    subject: str
    body_text: str
    body_html: str


def render(template: str, **ctx: Any) -> tuple[str, str]:
    """(text, html) for `<template>.txt` and `<template>.html`."""
    return (
        _env.get_template(f"{template}.txt").render(**ctx),
        _env.get_template(f"{template}.html").render(**ctx),
    )


class Mailer(Protocol):
    transport: str

    def deliver(self, email: Email) -> None:
        """Hand the message to the transport. Raises on failure."""


class OutboxMailer:
    """Delivers nowhere: the outbox row is the delivery."""

    transport = "outbox"

    def deliver(self, email: Email) -> None:
        return None


class SmtpMailer:
    transport = "smtp"

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "smtp" or not parsed.hostname:
            raise ValueError("TANGIBLE_SMTP_URL must look like smtp://host:port")
        self.host = parsed.hostname
        self.port = parsed.port or 25

    def deliver(self, email: Email) -> None:
        msg = EmailMessage()
        msg["From"] = FROM_ADDR
        msg["To"] = email.to_addr
        msg["Subject"] = email.subject
        msg.set_content(email.body_text)
        msg.add_alternative(email.body_html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=10) as s:
            s.send_message(msg)


def mailer_for(smtp_url: str | None) -> Mailer:
    return SmtpMailer(smtp_url) if smtp_url else OutboxMailer()


def send(
    conn: Conn, mailer: Mailer, tenant_id: str, email: Email, *, invite_id: str | None = None
) -> str:
    """Record the email in the outbox and deliver it. A delivery failure is recorded on the row,
    not raised: the outbox copy (and its link) is still there for the lender to share."""
    error: str | None = None
    try:
        mailer.deliver(email)
    except Exception as e:  # noqa: BLE001 - any transport failure is recorded, not fatal
        error = f"{type(e).__name__}: {e}"
    row = conn.execute(
        "INSERT INTO outbound_email (tenant_id, invite_id, to_addr, subject, body_text, body_html, "
        "transport, delivered_at, error) VALUES (%s, %s, %s, %s, %s, %s, %s, "
        "CASE WHEN %s THEN clock_timestamp() END, %s) RETURNING id",
        (tenant_id, invite_id, email.to_addr, email.subject, email.body_text, email.body_html,
         mailer.transport, error is None, error),
    ).fetchone()  # fmt: skip
    assert row
    return str(row[0])
