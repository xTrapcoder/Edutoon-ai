.DEFAULT_GOAL := help
.PHONY: help install up down reset migrate migrate-create migrate-down history seed dev worker test lint format typecheck contracts contracts-check

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install JS and Python dependencies
	pnpm install
	cd apps/api && uv sync

up: ## Start the Docker stack and wait for all healthchecks
	docker compose up -d --wait

down: ## Stop the Docker stack
	docker compose down

reset: ## Recreate the Docker stack from scratch (drops volumes)
	docker compose down -v
	$(MAKE) up

migrate: ## Apply all pending database migrations (upgrade to head)
	cd apps/api && uv run alembic upgrade head

migrate-create: ## Create a new migration: make migrate-create name="short description"
	@test -n "$(name)" || { echo 'usage: make migrate-create name="short description"'; exit 1; }
	cd apps/api && uv run alembic revision -m "$(name)"

migrate-down: ## Roll back one migration (downgrade -1)
	cd apps/api && uv run alembic downgrade -1

history: ## Show the migration history and current revision
	cd apps/api && uv run alembic history --verbose && uv run alembic current

seed: ## Seed development data (placeholder)
	@echo "seed: nothing to do yet (Phase 1)"

dev: ## Run the API (:8000), web (:3000) and job worker together with prefixed output
	@echo "Starting api (:8000), web (:3000) and worker — press Ctrl-C to stop"
	@trap 'kill 0' INT TERM EXIT; \
		( cd apps/api && uv run uvicorn edutoon.main:app --reload --port 8000 2>&1 | sed 's/^/[api] /' ) & \
		( cd apps/web && pnpm dev 2>&1 | sed 's/^/[web] /' ) & \
		( cd apps/api && uv run python -m edutoon.worker 2>&1 | sed 's/^/[worker] /' ) & \
		wait

worker: ## Run the background job worker on its own (polls jobs, dispatches to existing handlers)
	cd apps/api && uv run python -m edutoon.worker

test: ## Run the test suites
	cd apps/api && uv run pytest
	pnpm --filter @edutoon/web test

contracts: ## Regenerate the API contract (openapi.json) and the TypeScript types generated from it
	cd apps/api && uv run python scripts/export_openapi.py
	pnpm --filter @edutoon/contracts generate

contracts-check: ## Fail if the committed API contract or generated types are stale
	cd apps/api && uv run python scripts/export_openapi.py --check
	pnpm --filter @edutoon/contracts generate
	git diff --exit-code -- packages/contracts

# Python is type-checked on the package (`src`) only - that is what [tool.mypy] in
# apps/api/pyproject.toml targets. `mypy .` also strict-checks the test suite,
# whose functions are deliberately untyped, and fails on all of them.
lint: ## Lint JS/TS and Python
	pnpm lint
	cd apps/api && uv run ruff check . && uv run mypy src

format: ## Format JS/TS and Python
	pnpm format
	cd apps/api && uv run ruff format .

typecheck: ## Type-check JS/TS and Python
	pnpm typecheck
	cd apps/api && uv run mypy src
