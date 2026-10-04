# Two stages per mds/13-deployment.md: the builder resolves dependencies into a venv,
# the runtime copies that venv and nothing else from the builder.

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv /opt/venv

WORKDIR /build

# Dependencies install before any application code, so editing app/ rebuilds only the
# last two layers. The requirement list is derived from pyproject.toml rather than
# duplicated into a second file: the exact pins keep one home and cannot drift.
COPY pyproject.toml ./
RUN python -c "import pathlib, tomllib; pathlib.Path('requirements.txt').write_text(chr(10).join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))" \
 && pip install --require-virtualenv -r requirements.txt


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# chmod in a RUN rather than `COPY --chmod`, which the legacy builder rejects: the
# local Colima daemon has no BuildKit, and the image must build on both.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
COPY app ./app

USER appuser
EXPOSE ${PORT}

# Liveness, not readiness: a healthcheck touching the database would restart a healthy
# process during a database blip. /readyz is the platform's traffic gate instead.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["PORT"] + "/healthz", timeout=2)'

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
