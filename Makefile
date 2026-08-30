.PHONY: setup build start test lint check

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
	uv run pyright
	npm run lint
	npm run typecheck

check: lint test
