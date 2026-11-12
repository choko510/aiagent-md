# aiagent-md backend images (Trusted Control VM). One build, one target per process; each process gets only its
# own package set, its own non-root UID and a read-only root filesystem (compose sets read_only/cap_drop).
# Base pinned by digest (python:3.13-slim, pulled 2026-11-10). Re-pin deliberately; never use a floating tag.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.3
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
COPY backend/packages ./packages
# Bundle a fixed, verified upstream Skill at build time; never fetch or execute it during a Run.
COPY backend/tools/sync_yomiyasu.py ./tools/sync_yomiyasu.py
RUN python ./tools/sync_yomiyasu.py
ARG PACKAGES
# --locked: the lockfile is authoritative; a drift fails the build.
RUN uv export --locked --no-dev --no-hashes --no-editable $(for p in ${PACKAGES}; do printf -- "--package %s " "$p"; done) \
      --format requirements-txt -o /tmp/req.txt && \
    python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --no-deps -r /tmp/req.txt

FROM ${PYTHON_IMAGE} AS runtime-base
RUN groupadd -g 10001 aiagent && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin aiagent
COPY --from=build /opt/venv /opt/venv
COPY backend/migrations /opt/aiagent/migrations
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AIAGENT_PROFILE=production \
    AIAGENT_MIGRATIONS_DIR=/opt/aiagent/migrations
USER 10001:10001
