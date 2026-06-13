# syntax=docker/dockerfile:1.7

# ---- builder ----------------------------------------------------------------
# Resolves the uv-managed virtualenv into /app/.venv. uv + the lockfile + the
# build-context pyproject files only live in this stage; the runtime image
# never sees them.
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Build context is the monorepo root so the workspace lockfile is available.
COPY pyproject.toml uv.lock ./
COPY apps/wyrdfold-api/pyproject.toml ./apps/wyrdfold-api/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package wyrdfold-api

# ---- runtime ----------------------------------------------------------------
FROM python:3.11-slim AS runtime

# pandoc is shelled out to by the DOCX render pipeline; the rest of the
# runtime stays bare (no uv, no apt cache, no monorepo lockfiles).
RUN apt-get update \
    && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /sbin/nologin app

WORKDIR /app

# Copy the resolved venv (built in the builder stage) + the app source.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app apps/wyrdfold-api/app ./apps/wyrdfold-api/app

USER app

WORKDIR /app/apps/wyrdfold-api

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8001\")}/health', timeout=3).status == 200 else 1)"

# Default trusts proxy headers from any upstream — correct for Railway
# (their LB fronts every request). Self-hosters behind a known proxy
# should pin to its IPs by setting FORWARDED_ALLOW_IPS in their env. #30 F4.
ENV FORWARDED_ALLOW_IPS="*"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
