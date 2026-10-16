"""Shared demo login: one username and password that signs testers in as a fixed subject.

For a hosted demo with seeded data, alongside Clerk or OIDC. Off unless TANGIBLE_DEMO_LOGIN_USER,
TANGIBLE_DEMO_LOGIN_PASSWORD and TANGIBLE_DEMO_LOGIN_SECRET are all set. A correct login gets a
short-lived HS256 token (prefixed `tdemo_` so it is never mistaken for an IdP token) whose subject
is always the configured one (TANGIBLE_DEMO_LOGIN_SUBJECT). As with any other subject, tenant and
role come from app_user: link it with `tangible-demo seed --link-subject demo:credit-admin`.
"""

import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import jwt

from tangible.workbench.auth import (
    TENANT_HEADER,
    Authenticator,
    Identity,
    Principal,
    Unauthenticated,
    choose_tenant,
)

TOKEN_PREFIX = "tdemo_"
ISSUER = "tangible-demo-login"
MIN_SECRET_CHARS = 32


@dataclass(frozen=True)
class DemoLogin:
    username: str
    password: str = field(repr=False)
    secret: str = field(repr=False)
    subject: str = "demo:credit-admin"
    ttl_seconds: int = 12 * 3600

    def __post_init__(self) -> None:
        if len(self.secret) < MIN_SECRET_CHARS:
            raise ValueError(f"TANGIBLE_DEMO_LOGIN_SECRET needs {MIN_SECRET_CHARS}+ characters")

    def check(self, username: str, password: str) -> bool:
        # Compare both, always, so timing does not say which one was wrong.
        u = hmac.compare_digest(username.encode(), self.username.encode())
        p = hmac.compare_digest(password.encode(), self.password.encode())
        return u and p

    def issue(self, now: float | None = None) -> tuple[str, int]:
        iat = int(time.time() if now is None else now)
        exp = iat + self.ttl_seconds
        claims = {"iss": ISSUER, "sub": self.subject, "iat": iat, "exp": exp}
        return TOKEN_PREFIX + jwt.encode(claims, self.secret, algorithm="HS256"), exp

    def verify(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token.removeprefix(TOKEN_PREFIX), self.secret, algorithms=["HS256"],
                issuer=ISSUER, options={"require": ["exp", "iat", "iss", "sub"]},
            )  # fmt: skip
        except jwt.PyJWTError:
            raise Unauthenticated("invalid demo session") from None
        # Only the configured subject: rotating it (or the secret) ends every demo session.
        if claims.get("sub") != self.subject:
            raise Unauthenticated("invalid demo session")
        return self.subject


class DemoLoginAuthenticator:
    """Demo tokens are handled here; every other request goes to the wrapped authenticator."""

    def __init__(self, inner: Authenticator, conninfo: str, demo: DemoLogin) -> None:
        self.inner = inner
        self.conninfo = conninfo
        self.demo = demo

    def _subject(self, headers: Mapping[str, str]) -> str | None:
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token.startswith(TOKEN_PREFIX):
            return None
        return self.demo.verify(token)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        sub = self._subject(headers)
        if sub is None:
            return self.inner.authenticate(headers)
        return choose_tenant(self.conninfo, sub, headers.get(TENANT_HEADER, "").strip() or None)

    def identify(self, headers: Mapping[str, str]) -> Identity:
        sub = self._subject(headers)
        return self.inner.identify(headers) if sub is None else Identity(sub)
