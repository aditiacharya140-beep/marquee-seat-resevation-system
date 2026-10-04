#!/usr/bin/env bash
# bash, not sh or zsh: zsh does not word-split unquoted variables (LEARN-004), and the
# `[[` and arithmetic forms below are bash.
set -euo pipefail

# An explicit command inspects the image without starting the server, which is what
# `docker run --rm <image> ls -A /app` in SEAT-005 and the CI image-contents check do.
if (($# > 0)); then
    exec "$@"
fi

# Migrations run before the server, so a deploy cannot forget them (RISK-005), and a
# failure here fails the boot: a server up against a schema it does not match is worse
# than no server. The config lives inside app/ because that is all the image carries
# (RISK-011).
alembic -c "${ALEMBIC_CONFIG:-app/alembic.ini}" upgrade head

# exec, so uvicorn is PID 1 and receives SIGTERM directly rather than through a shell
# that would not forward it. --no-access-log: AccessLogMiddleware owns that line.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --no-access-log
