.PHONY: install db-up db-down migrate test lint fmt typecheck check demo

install:
	uv sync

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

migrate:
	uv run tangible-migrate

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff check --fix . && uv run ruff format .

typecheck:
	uv run mypy

check: lint typecheck test

# Fresh demo tenant on synthetic data dated to now (needs `make db-up` and
# data/processed/uci_lines.csv). Prints the dev-login URL. No model calls.
# Live run (a separate step; about $1.50 per staged case on Bedrock):
#   TANGIBLE_MODEL_BACKEND=bedrock TANGIBLE_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
#   TANGIBLE_BEDROCK_REGION=us-east-2 TANGIBLE_PRICE_IN_PER_MTOK=3.30 TANGIBLE_PRICE_OUT_PER_MTOK=16.50 \
#   AWS_PROFILE=tangible uv run --with "botocore[crt]" tangible-demo seed --run-agent
# Then serve it with the `workbench-demo` launch config (port 8001, same env, dev auth).
# The agent runs on Strands Agents by default; TANGIBLE_AGENT_ENGINE=classic uses the old loop.
demo: migrate
	uv run tangible-demo seed
