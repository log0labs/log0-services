# Python ai-service - multi-stage via uv (replaces Java jar build for this service only).

FROM python:3.13-slim-bookworm AS base
# Install uv from PyPI (avoids ghcr.io auth/rate-limit issues on some Docker setups).
RUN pip install --no-cache-dir uv

# git: the code tools clone tenants' repositories (3.4); ca-certificates for https
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional: the docker client the test sandbox uses (talks to a dedicated runner daemon through DOCKER_HOST).
ARG INSTALL_DOCKER_CLI=false
RUN if [ "$INSTALL_DOCKER_CLI" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends curl \
        && curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-27.3.1.tgz" \
           | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
        && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first (layer cache when only app/ changes)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8085

COPY alembic.ini ./
COPY alembic ./alembic

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
