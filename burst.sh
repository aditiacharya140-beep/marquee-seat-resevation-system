#!/usr/bin/env bash
# bash, not zsh: zsh does not word-split unquoted variables (LEARN-004).
#
#   ./burst.sh <BASE_URL> [--users N] [--seats N] [--hot N] [--concurrency N] [--accounts]
#
# The burst creates its own show, so it needs the admin credentials: ADMIN_EMAIL and
# ADMIN_PASSWORD from the environment, falling back to ./.env for a local target.
set -euo pipefail

if (($# < 1)); then
    echo "usage: $0 <BASE_URL> [burst options]" >&2
    exit 2
fi

cd "$(dirname "$0")"

if [[ -z "${ADMIN_EMAIL:-}" || -z "${ADMIN_PASSWORD:-}" ]] && [[ -f .env ]]; then
    ADMIN_EMAIL="${ADMIN_EMAIL:-$(sed -n 's/^ADMIN_EMAIL=//p' .env)}"
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(sed -n 's/^ADMIN_PASSWORD=//p' .env)}"
fi
: "${ADMIN_EMAIL:?set ADMIN_EMAIL to the deployment's admin email}"
: "${ADMIN_PASSWORD:?set ADMIN_PASSWORD to the deployment's admin password}"
export ADMIN_EMAIL ADMIN_PASSWORD

python=python3
[[ -x .venv/bin/python ]] && python=.venv/bin/python
exec "$python" burst/burst.py "$@"
