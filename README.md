# Tangible

Operational due diligence for lenders. The agent investigates, the tools compute, the policy decides.
Full plan: [docs/build-plan.md](docs/build-plan.md).

## Quick start

```bash
make install   # uv sync
make db-up     # Postgres 16 in Docker
make migrate
make check     # ruff, mypy, pytest
```

## Layout

| Path | What |
| --- | --- |
| `src/tangible/db/migrations/` | Plain SQL migrations (schema, RLS, immutability triggers). Applied by `tangible-migrate`. |
| `src/tangible/ledger/` | Ledger row types and `known_as_of`, the single place the `observed_at` time rule lives. |
| `src/tangible/metrics/` | Versioned pure metric functions + registry. |
| `src/tangible/checks/` | Versioned integrity checks + registry; they emit findings. |
| `src/tangible/policy/` | Policy loader (version + hash). Evaluator still to build. |
| `src/tangible/agent/` | `llm.py` is the only vendor-aware module; `tools/` holds the agent's logged tools. |
| `policies/default.yaml` | Default lender policy (starting-guess thresholds). |

## Notes

- Ledger table `order` is named `orders` (reserved word). Every ledger table carries `source_ref` so a revised record is a new row, not an overwrite.
- App code must run as role `tangible_app` and `SET app.tenant_id` per connection; RLS is forced on every tenant table.
- Not yet built: connectors (Rutter/Codat, Plaid), S3/Object Lock, Bedrock client, policy evaluator, the remaining metrics and checks, the agent loop, the backtest harness.
