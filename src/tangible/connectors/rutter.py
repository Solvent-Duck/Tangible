"""Rutter unified commerce/accounting API connector.

VERIFY AGAINST CURRENT DOCS BEFORE LIVE USE. Everything below is written from memory of Rutter's
public docs and has never run against a real or sandbox account. Assumptions:

  base URL       https://production.rutterapi.com/versioned  (sandbox host differs)
  auth           HTTP Basic (client_id:client_secret) plus the connection's `access_token` as a
                 query parameter on every request
  pagination     `cursor` query param in, `next_cursor` in the response body (null/absent on the
                 last page), `limit` for page size
  incremental    `updated_at_min` query param (ISO 8601, epoch ms is also accepted by Rutter in
                 some endpoints); our per-stream cursor is the max `updated_at` seen
  endpoints      GET /orders            -> {"orders": [...], "next_cursor"}
                 GET /payouts           -> {"payouts": [...], "next_cursor"}
                 GET /accounting/invoices (receivables) -> {"invoices": [...], "next_cursor"}
                 GET /accounting/bills    (payables)    -> {"bills": [...], "next_cursor"}
  order fields   id, order_number, currency_code, subtotal, total_discount, total_tax,
                 total_amount, created_at, updated_at, customer{id, email}, refunds[]
  refund fields  (embedded in the order) id, amount, created_at, reason
  payout fields  id, amount, total_fees, currency_code, status, expected_arrival_date,
                 paid_at / settled_on, created_at, updated_at
  invoice/bill   id, total_amount, issue_date, due_date, paid_on, contact{name} /
                 customer{name} / vendor{name}, status, created_at, updated_at
"""

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx

from tangible.connectors.base import (
    ConnectorAccount,
    ConnectorError,
    PullResult,
    Record,
    envelope,
    json_object,
    request_with_retry,
)
from tangible.secrets import SecretStore

BASE_URL = "https://production.rutterapi.com/versioned"

# stream -> (path, response list key, record kind)
STREAMS: dict[str, tuple[str, str, str]] = {
    "orders": ("/orders", "orders", "order"),
    "payouts": ("/payouts", "payouts", "payout"),
    "invoices": ("/accounting/invoices", "invoices", "invoice_receivable"),
    "bills": ("/accounting/bills", "bills", "invoice_payable"),
}
DEFAULT_STREAMS = ("orders", "payouts")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class RutterConnector:
    provider = "rutter"

    def __init__(
        self,
        client: httpx.Client,
        secrets: SecretStore,
        client_id_ref: str,
        client_secret_ref: str,
        base_url: str = BASE_URL,
        streams: tuple[str, ...] = DEFAULT_STREAMS,
        page_size: int = 100,
        max_pages: int = 1000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        unknown = set(streams) - STREAMS.keys()
        if unknown:
            raise ValueError(f"unknown Rutter streams: {sorted(unknown)}")
        self.client = client
        self.secrets = secrets
        self.client_id_ref = client_id_ref
        self.client_secret_ref = client_secret_ref
        self.base_url = base_url.rstrip("/")
        self.streams = streams
        self.page_size = page_size
        self.max_pages = max_pages
        self.sleep = sleep

    def pull(self, account: ConnectorAccount, since: Mapping[str, str]) -> PullResult:
        token = self.secrets.get(account.secret_ref)
        cid, csecret = (
            self.secrets.get(self.client_id_ref),
            self.secrets.get(self.client_secret_ref),
        )
        known = (token, csecret)
        pages: dict[str, list[str]] = {}
        records: list[Record] = []
        cursors: dict[str, str] = {}
        for stream in self.streams:
            path, key, kind = STREAMS[stream]
            watermark = since.get(stream)
            newest = watermark
            pages[stream] = []
            page_cursor: str | None = None
            for _ in range(self.max_pages):
                params: dict[str, Any] = {"access_token": token, "limit": self.page_size}
                if watermark:
                    params["updated_at_min"] = watermark
                if page_cursor:
                    params["cursor"] = page_cursor
                resp = request_with_retry(
                    self.client,
                    "GET",
                    self.base_url + path,
                    params=params,
                    auth=(cid, csecret),
                    secrets=known,
                    sleep=self.sleep,
                )
                body = json_object(resp, f"GET {path}")
                pages[stream].append(resp.text)
                items = body.get(key)
                if not isinstance(items, list):
                    raise ConnectorError(f"GET {path}: response has no {key!r} list")
                for item in items:
                    if not isinstance(item, dict) or "id" not in item:
                        raise ConnectorError(f"GET {path}: record without an id")
                    records.append(Record(kind, item))
                    if kind == "order":
                        # refunds are nested in their order; link them explicitly
                        records.extend(
                            Record(
                                "refund",
                                {
                                    **r,
                                    "order_id": item["id"],
                                    "order_created_at": item.get("created_at"),
                                },
                            )
                            for r in item.get("refunds") or []
                        )
                    upd = item.get("updated_at")
                    if isinstance(upd, str) and (newest is None or _ts(upd) > _ts(newest)):
                        newest = upd
                page_cursor = body.get("next_cursor") or None
                if not page_cursor:
                    break
            else:
                raise ConnectorError(f"GET {path}: more than {self.max_pages} pages")
            if newest:
                cursors[stream] = newest
        return PullResult(envelope(self.provider, pages), records, cursors)
