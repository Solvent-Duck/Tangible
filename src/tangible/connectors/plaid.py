"""Plaid connector: /transactions/sync and /accounts/balance/get.

VERIFY AGAINST CURRENT DOCS BEFORE LIVE USE. Written from memory of Plaid's public docs; never run
against a real or sandbox Item. Assumptions:

  base URL       https://production.plaid.com (sandbox: https://sandbox.plaid.com)
  auth           JSON body fields client_id, secret, access_token on every POST
  headers        Plaid-Version: 2020-09-14, Content-Type: application/json
  POST /transactions/sync   body {cursor?, count}; response {added[], modified[], removed[],
                 next_cursor, has_more}. Loop until has_more is false; the final next_cursor is
                 what we persist. A TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION error is not
                 handled (the pull fails and is retried from the old cursor).
  transaction    transaction_id, account_id, amount (POSITIVE = money OUT of the account,
                 negative = money in), iso_currency_code, date (YYYY-MM-DD), name,
                 merchant_name, counterparties[{name}], pending
  removed item   {transaction_id, account_id}
  POST /accounts/balance/get  response {accounts: [{account_id, balances: {available, current,
                 iso_currency_code}, name, mask}]}  (`current` is used as the ledger balance)
"""

import time
from collections.abc import Callable, Mapping
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

BASE_URL = "https://production.plaid.com"
API_VERSION = "2020-09-14"
TX_STREAM = "transactions"
BAL_STREAM = "balances"


class PlaidConnector:
    provider = "plaid"

    def __init__(
        self,
        client: httpx.Client,
        secrets: SecretStore,
        client_id_ref: str,
        client_secret_ref: str,
        base_url: str = BASE_URL,
        page_size: int = 500,
        max_pages: int = 1000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.secrets = secrets
        self.client_id_ref = client_id_ref
        self.client_secret_ref = client_secret_ref
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.max_pages = max_pages
        self.sleep = sleep

    def _post(
        self, path: str, auth: dict[str, str], extra: dict[str, Any], known: tuple[str, ...]
    ) -> tuple[dict[str, Any], str]:
        resp = request_with_retry(
            self.client,
            "POST",
            self.base_url + path,
            json={**auth, **extra},
            headers={"Plaid-Version": API_VERSION},
            secrets=known,
            sleep=self.sleep,
        )
        return json_object(resp, f"POST {path}"), resp.text

    def pull(self, account: ConnectorAccount, since: Mapping[str, str]) -> PullResult:
        token = self.secrets.get(account.secret_ref)
        cid, csecret = (
            self.secrets.get(self.client_id_ref),
            self.secrets.get(self.client_secret_ref),
        )
        auth = {"client_id": cid, "secret": csecret, "access_token": token}
        known = (token, csecret)
        records: list[Record] = []
        tx_pages: list[str] = []
        cursor = since.get(TX_STREAM)
        for _ in range(self.max_pages):
            extra: dict[str, Any] = {"count": self.page_size}
            if cursor:
                extra["cursor"] = cursor
            body, text = self._post("/transactions/sync", auth, extra, known)
            tx_pages.append(text)
            for list_key, kind in (
                ("added", "txn_added"),
                ("modified", "txn_modified"),
                ("removed", "txn_removed"),
            ):
                for item in body.get(list_key) or []:
                    if not isinstance(item, dict) or "transaction_id" not in item:
                        raise ConnectorError("POST /transactions/sync: record without id")
                    records.append(Record(kind, item))
            nxt = body.get("next_cursor")
            if not isinstance(nxt, str) or not nxt:
                raise ConnectorError("POST /transactions/sync: response has no next_cursor")
            cursor = nxt
            if not body.get("has_more"):
                break
        else:
            raise ConnectorError(f"POST /transactions/sync: more than {self.max_pages} pages")

        bal_body, bal_text = self._post("/accounts/balance/get", auth, {}, known)
        for acct in bal_body.get("accounts") or []:
            if not isinstance(acct, dict) or "account_id" not in acct or "balances" not in acct:
                raise ConnectorError("POST /accounts/balance/get: malformed account")
            records.append(Record("balance", acct))

        return PullResult(
            envelope(self.provider, {TX_STREAM: tx_pages, BAL_STREAM: [bal_text]}),
            records,
            {TX_STREAM: cursor},
        )
