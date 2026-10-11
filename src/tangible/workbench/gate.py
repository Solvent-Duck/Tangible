"""A shared-password gate (HTTP Basic) in front of the whole app, for hosted dev-auth demos.

Dev sign-in lets anyone pick a tenant and user, so a publicly reachable demo needs something in
front of it. This is that something: one password from TANGIBLE_ACCESS_PASSWORD, any username.
It is not user authentication; production deployments use OIDC instead (and the gate refuses to
run alongside it, since both would want the Authorization header).
"""

import base64
import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

OPEN_PATHS = frozenset({"/healthz"})  # the platform health check has no credentials
_REALM = b'Basic realm="Tangible demo", charset="UTF-8"'


def _password(header: bytes) -> bytes | None:
    scheme, _, value = header.partition(b" ")
    if scheme.lower() != b"basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except ValueError:
        return None
    _, sep, password = decoded.partition(b":")
    return password if sep else None


class PasswordGate:
    def __init__(self, app: ASGIApp, password: str) -> None:
        if not password:
            raise ValueError("the access password must not be empty")
        self.app = app
        self.password = password.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"")
        given = _password(header)
        if given is not None and hmac.compare_digest(given, self.password):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", _REALM),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Password required.\n"})
