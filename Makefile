.PHONY: setup build start test lint check docker-build docker-up docker-down

setup:
	uv sync
	npm ci

build:
	npm run build

start: build
	uv run careerradar start --port 8010

test:
	uv run pytest -q
	npm test

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	npm run lint
	npm run typecheck

check: lint test

docker-build:
	docker compose build

docker-up:
	touch jobs.db graphs.db
	docker compose up -d

docker-down:
	docker compose down

