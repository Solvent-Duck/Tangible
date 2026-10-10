"""Connector interface and shared HTTP plumbing (timeouts, retries with backoff, redaction)."""

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from tangible.secrets import redact

DEFAULT_TIMEOUT = 30.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ConnectorAccount:
    """One external account (a Plaid item, a Rutter connection) for a borrower. The token is not
    here: `secret_ref` names it in the secret store."""

    id: str
    tenant_id: str
    borrower_id: str
    connection_id: str
    provider: str
    secret_ref: str
    external_account_id: str | None = None


@dataclass(frozen=True)
class Record:
    """One provider record, tagged with what it is. `data` is the provider's JSON object."""

    kind: str
    data: dict[str, Any]


@dataclass
class PullResult:
    """`raw` is the evidence: a JSON envelope holding every response body byte-for-byte.
    `next_cursor` maps stream -> cursor to persist after the pull is recorded."""

    raw: bytes
    records: list[Record]
    next_cursor: dict[str, str] = field(default_factory=dict)


class Connector(Protocol):
    provider: str

    def pull(self, account: ConnectorAccount, since: Mapping[str, str]) -> PullResult:
        """Fetch everything new since the per-stream cursors in `since` (empty = from scratch)."""
        ...


class ConnectorError(Exception):
    """A pull failed. The message is already redacted: safe to log and to store."""


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    secrets: Iterable[str] = (),
    retries: int = 3,
    backoff: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying 429/5xx and transport errors with exponential backoff (honouring
    a numeric Retry-After). Returns a 2xx response; anything else raises ConnectorError whose
    message omits the query string, headers and body (they carry credentials)."""
    known = tuple(secrets)
    where = f"{method} {httpx.URL(url).path}"
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if last:
                raise ConnectorError(
                    redact(f"{where}: {type(e).__name__} after {attempt + 1} attempts", known)
                ) from None
            sleep(backoff * 2**attempt)
            continue
        if resp.is_success:
            return resp
        if resp.status_code in RETRY_STATUSES and not last:
            sleep(_delay(resp, backoff * 2**attempt))
            continue
        raise ConnectorError(redact(f"{where}: HTTP {resp.status_code}{_error_code(resp)}", known))
    raise AssertionError("unreachable")  # pragma: no cover


def _delay(resp: httpx.Response, default: float) -> float:
    try:
        return min(float(resp.headers["Retry-After"]), 60.0)
    except (KeyError, ValueError):
        return default


def _error_code(resp: httpx.Response) -> str:
    try:
        code = resp.json().get("error_code")
    except (ValueError, AttributeError):
        return ""
    return f" ({str(code)[:60]})" if code else ""


def envelope(provider: str, streams: Mapping[str, list[str]]) -> bytes:
    """`{"provider": ..., "streams": {name: [<response body>, ...]}}` with each response body
    embedded exactly as received (the caller has already checked it is valid JSON)."""
    parts = ",".join(f"{json.dumps(name)}:[{','.join(pages)}]" for name, pages in streams.items())
    return f'{{"provider":{json.dumps(provider)},"streams":{{{parts}}}}}'.encode()


def json_object(resp: httpx.Response, where: str) -> dict[str, Any]:
    try:
        body = json.loads(resp.text)
    except ValueError:
        raise ConnectorError(f"{where}: response is not JSON") from None
    if not isinstance(body, dict):
        raise ConnectorError(f"{where}: response is not a JSON object")
    return body
