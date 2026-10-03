#!/usr/bin/env bash
# Verifies the local toolchain matches what the service and container expect.
# Bash, not zsh: zsh does not word-split unquoted variables (see LEARN-004).
set -uo pipefail

PG_BIN="/opt/homebrew/opt/postgresql@16/bin"
export PATH="$PG_BIN:$PATH"
: "${PGHOST:=localhost}" "${PGPORT:=5432}" "${PGUSER:=seatres}" "${PGPASSWORD:=seatres}"
export PGHOST PGPORT PGUSER PGPASSWORD

fail=0
check() {
  local label="$1"; shift
  if out=$("$@" 2>&1); then
    printf '  ok    %-22s %s\n' "$label" "$(head -1 <<<"$out")"
  else
    printf '  FAIL  %-22s %s\n' "$label" "$(head -1 <<<"$out")"
    fail=1
  fi
}

echo "toolchain"
check python .venv/bin/python --version
check postgres postgres --version
check psql psql --version
check docker docker --version
check compose docker compose version

echo "services"
check "postgres accepting" pg_isready -q
check "docker daemon" docker info --format '{{.ServerVersion}}'

echo "databases"
for db in seatres seatres_test; do
  check "db $db" psql -d "$db" -tAc 'SELECT current_database()'
done
check "max_connections" psql -d postgres -tAc 'SHOW max_connections'

echo "python imports"
check "runtime deps" .venv/bin/python -c \
  'import fastapi,pydantic,asyncpg,jwt,alembic,prometheus_client,orjson;from argon2 import PasswordHasher;print("all importable")'

echo "config"
if [[ -f .env ]]; then
  printf '  ok    %-22s %s\n' ".env present" "$(grep -c = .env) settings"
else
  printf '  FAIL  %-22s %s\n' ".env present" "missing — copy .env.example"
  fail=1
fi

echo
if (( fail )); then echo "VERDICT: FAIL"; exit 1; fi
echo "VERDICT: PASS"
