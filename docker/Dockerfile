# App image: dependencies only. Compose bind-mounts the repo, entrypoint included, over /app.
FROM python:3.12-slim

# Venv outside /app so the bind mount does not hide it. Dependencies are precompiled at
# build time; the mounted repo gets no .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# curl for seed_clickhouse.sh; postgresql-client for pg_isready and psql.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra dev

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
