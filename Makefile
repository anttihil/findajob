.PHONY: setup build start dev test lint check verify package-smoke docker-build docker-up docker-down

setup:
	uv sync
	npm ci

build:
	npm run build

start: build
	uv run findajob start --port 8010

DEV_PORT ?= 5173
DEV_API_PORT ?= 8011

dev:
	python3 scripts/dev.py --port "$(DEV_PORT)" --api-port "$(DEV_API_PORT)"

test:
	uv run pytest -q
	npm test

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	npm run lint
	npm run format:check
	npm run typecheck

verify:
	python3 scripts/verify.py

check: verify

package-smoke: build
	python3 scripts/package_smoke.py

docker-build:
	docker compose build

docker-up:
	mkdir -p data resumes
	touch data/jobs.db graphs.db config.local.yaml
	docker compose up -d

docker-down:
	docker compose down
