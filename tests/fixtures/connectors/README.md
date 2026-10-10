# Connector fixtures

These JSON files are HAND-BUILT in each provider's documented response shape (as best recalled
from the public docs). They are NOT captured from a Plaid or Rutter sandbox, so passing tests
against them says nothing about the live APIs. See the "VERIFY AGAINST CURRENT DOCS" blocks at
the top of `connectors/plaid.py` and `connectors/rutter.py`.

All ids, names and amounts are invented.

- `plaid_sync_initial_1/2.json`: first `/transactions/sync` (two pages, `has_more`); page 1 holds a
  pending transaction that must be skipped.
- `plaid_sync_update.json`: a later sync with a modified and a removed transaction.
- `plaid_balance.json`: `/accounts/balance/get`.
- `rutter_orders_1/2.json`: `/orders`, two pages via `next_cursor`; order `ord-1` carries a refund.
- `rutter_payouts.json`, `rutter_invoices.json` (receivable), `rutter_bills.json` (payable).
