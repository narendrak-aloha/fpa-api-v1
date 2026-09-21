#!/usr/bin/env bash
# Apply governance migrations and load db/seed.yaml. Safe to re-run.
#   scripts/seed_postgres.sh
# FPA_GOVERNANCE_DB_URL overrides the target; otherwise db/config.py resolves it.
set -euo pipefail

cd "$(dirname "$0")/.."
# Default to the interpreter `uv sync` builds, so an activated venv is not required.
if [ -x .venv/bin/python ]; then DEFAULT_PY=.venv/bin/python; else DEFAULT_PY=python3; fi
PY="${PYTHON:-$DEFAULT_PY}"
export FPA_GOVERNANCE_DB_URL="${FPA_GOVERNANCE_DB_URL:-$("$PY" -c 'from db.config import database_url; print(database_url())')}"

echo "==> waiting for Postgres at ${FPA_GOVERNANCE_DB_URL##*@}"
for _ in $(seq 1 60); do
    "$PY" - <<'PYEOF' && break || sleep 1
import os, sys
import psycopg
url = os.environ["FPA_GOVERNANCE_DB_URL"].replace("postgresql+psycopg", "postgresql")
try:
    psycopg.connect(url, connect_timeout=2).close()
except Exception:
    sys.exit(1)
PYEOF
done

echo "==> applying migrations"
"$PY" -m alembic -c db/alembic.ini upgrade head

echo "==> loading db/seed.yaml"
"$PY" -m db.seed

echo "==> done: $("$PY" -m alembic -c db/alembic.ini current 2>/dev/null | tail -1)"
