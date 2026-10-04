#!/usr/bin/env bash
# bash, not sh or zsh: zsh does not word-split unquoted variables (LEARN-004), and the
# `[[` and arithmetic forms below are bash.
set -euo pipefail

# An explicit command inspects the image without starting the server, which is what
# `docker run --rm <image> ls -A /app` in SEAT-005 and the CI image-contents check do.
if (($# > 0)); then
    exec "$@"
fi

# mds/13-deployment.md has the entrypoint run `alembic upgrade head` before the server,
# so a deploy cannot forget it (RISK-005). Alembic arrives in SEAT-008; until its config
# exists there is nothing to apply, and failing the boot over a migration path that has
# not been written yet would make a correct container look broken.
if [[ -f "${ALEMBIC_CONFIG:-alembic.ini}" ]]; then
    alembic upgrade head
else
    printf '{"ts":"%s","level":"warning","event":"migrations_skipped","request_id":null}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
fi

# exec, so uvicorn is PID 1 and receives SIGTERM directly rather than through a shell
# that would not forward it. --no-access-log: AccessLogMiddleware owns that line.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --no-access-log
