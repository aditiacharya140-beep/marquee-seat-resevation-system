PY      := .venv/bin/python
PYTEST  := .venv/bin/pytest
RUFF    := .venv/bin/ruff
MYPY    := .venv/bin/mypy
PORT    ?= 8000

.PHONY: help lint format types test unit integration concurrency run verify migrate burst

help:
	@printf '%-14s %s\n' \
	  lint        'ruff check' \
	  format      'ruff format and import fix' \
	  types       'mypy strict over app/' \
	  test        'full pytest suite' \
	  unit        'unit tests only' \
	  integration 'integration tests only' \
	  concurrency 'concurrency tests only' \
	  run         'uvicorn with reload on $$PORT' \
	  burst       'the on-sale stampede: make burst URL=<base url>' \
	  verify      'local toolchain and service check'

lint:
	$(RUFF) check .

format:
	$(RUFF) check --fix .
	$(RUFF) format .

types:
	$(MYPY) app

test:
	$(PYTEST)

unit:
	$(PYTEST) tests/unit

integration:
	$(PYTEST) tests/integration

concurrency:
	$(PYTEST) -m concurrency tests/concurrency

run:
	$(PY) -m uvicorn app.main:app --reload --port $(PORT) --no-access-log

migrate:
	$(PY) -m alembic -c app/alembic.ini upgrade head

# make burst URL=https://… — needs ADMIN_EMAIL and ADMIN_PASSWORD, or a local .env
burst:
	./burst.sh $(URL)

verify:
	./scripts/verify-env.sh
