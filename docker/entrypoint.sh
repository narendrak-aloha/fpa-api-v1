#!/usr/bin/env bash
# Wait for Postgres, migrate, seed, then run the given command.
# Seeding runs every start but only works once: db.seed upserts, and
# seed_clickhouse.sh skips a non-empty cube. FPA_SKIP_SEED=1 skips both.
#
# FPA_ROLE picks what this container does on the way in. Only `app` migrates
# and seeds: the worker and the commitment service share the image but must
# not race the app to run the same migrations.
set -euo pipefail

FPA_ROLE="${FPA_ROLE:-app}"

# Host claude CLI, for the claude-code provider.
if [ -d /opt/claude/versions ]; then
    latest="$(ls -d /opt/claude/versions/* 2>/dev/null | sort -V | tail -1)"
    [ -n "$latest" ] && ln -sf "$latest" /usr/local/bin/claude && echo "==> claude CLI $(basename "$latest") available"
fi

echo "==> waiting for Postgres"
until pg_isready -h "${PGHOST:-postgres}" -p "${PGPORT:-5432}" -U "${PGUSER:-postgres}" -q; do sleep 1; done

if [ "$FPA_ROLE" != "app" ]; then
    # The app container owns the schema. Everything else waits for it to exist
    # rather than building it, so two containers never migrate at once.
    echo "==> role=$FPA_ROLE, waiting for the governance schema"
    until psql -tAc "SELECT to_regclass('fpa_governance.plan_publication') IS NOT NULL" | grep -q '^t$'; do
        sleep 1
    done
    echo "==> schema is ready"
    exec "$@"
fi

echo "==> alembic upgrade head"
alembic -c db/alembic.ini upgrade head
alembic -c db/alembic.ini current

if [ "${FPA_SKIP_SEED:-0}" = "1" ]; then
    echo "==> FPA_SKIP_SEED=1, not seeding"
else
    echo "==> seeding governance store from db/seed.yaml"
    python -m db.seed
    # ~1M rows the first time; a no-op after.
    scripts/seed_clickhouse.sh
    # The seeded plan's base lines, with traces, into the governance store.
    # A no-op once imported, or once the version is past IN_REVIEW.
    python -m db.import_plan_lines
    # Then through its three gates to LOCKED, because a re-forecast needs its
    # source plan settled. Runs after the import: a LOCKED version's lines
    # cannot be written. A no-op once it is settled.
    python -m db.settle_plan
fi

echo "==> API on http://localhost:8000"
exec "$@"
