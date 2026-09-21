#!/usr/bin/env bash
# Start the stack and load both databases. Safe to re-run.
#   scripts/bootstrap.sh
set -euo pipefail

cd "$(dirname "$0")/.."
echo "==> syncing .venv from uv.lock"
uv sync --frozen --extra dev

echo "==> docker compose up -d"
docker compose -f docker/docker-compose.yml up -d

scripts/seed_postgres.sh
scripts/seed_clickhouse.sh
"${PYTHON:-.venv/bin/python}" -m db.import_plan_lines

echo
echo "ClickHouse  http://localhost:8123   Postgres  localhost:5431   Temporal UI  http://localhost:8233"
