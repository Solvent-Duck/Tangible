.PHONY: install db-up db-down migrate test lint fmt typecheck check

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
