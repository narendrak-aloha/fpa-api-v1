#!/usr/bin/env bash
# Load the ClickHouse cube from data/seed_fpa.py, only when it is empty.
#   scripts/seed_clickhouse.sh          seed if empty
#   RESEED_CUBE=1 scripts/seed_clickhouse.sh   drop and rebuild
# Env: CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD.
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${CLICKHOUSE_HOST:-localhost}"; PORT="${CLICKHOUSE_PORT:-8123}"
USER="${CLICKHOUSE_USER:-default}"; PASSWORD="${CLICKHOUSE_PASSWORD:-fpa}"
PY="${PYTHON:-python3}"
query() { curl -sS --fail -u "${USER}:${PASSWORD}" "http://${HOST}:${PORT}/" --data-binary "$1"; }

echo "==> waiting for ClickHouse on ${HOST}:${PORT}"
for _ in $(seq 1 60); do query "SELECT 1" >/dev/null 2>&1 && break || sleep 1; done

# data/seed_fpa.py writes cube_manifest.json as its last step, after both
# fact_gl_actual and fact_plan_line have loaded (see its bottom, around the
# "wrote ..." print). Gating on that file -- not just fact_gl_actual's row
# count -- means a run interrupted between the two fact-table loads (OOM,
# container restart, etc.) is treated as incomplete and retried with --drop,
# instead of being skipped forever with fact_plan_line silently empty.
MANIFEST="data/out/cube_manifest.json"
ROWS=$(query "SELECT count() FROM fpa_cube.fact_gl_actual" 2>/dev/null || echo 0)
if [ "${RESEED_CUBE:-0}" != "1" ] && [ -f "$MANIFEST" ] && [ "${ROWS:-0}" -gt 0 ]; then
    echo "==> cube already seeded (${ROWS} actual rows, ${MANIFEST} present); skipping (RESEED_CUBE=1 to rebuild)"
    exit 0
fi

# fact_gl_actual is a ReplacingMergeTree(_version) sorted on
# (company, period_month, account, dim_signature_hash), which does not include
# _version. A vintage 2 restatement lands on the same sorting key as the
# vintage 1 row it corrects, so a background merge collapses the pair and keeps
# only vintage 2 -- the July close is then gone from disk and no AS OF query can
# recover it. The table is therefore created up front with merges pinned off,
# before a single row lands, and the seeder is run without --drop so its
# CREATE TABLE IF NOT EXISTS statements leave that setting alone. The setting
# lives in table metadata, so it survives a restart. Nothing depends on merging:
# the compiler resolves versions at read time with ORDER BY _version DESC.
echo "==> creating the schema with merges pinned off on fact_gl_actual"
query "DROP DATABASE IF EXISTS fpa_cube" >/dev/null
"$PY" - "$HOST" "$PORT" "$USER" "$PASSWORD" <<'PYDDL'
import importlib.util, pathlib, sys
import clickhouse_connect
spec = importlib.util.spec_from_file_location("seed_fpa", pathlib.Path("data/seed_fpa.py"))
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
host, port, user, password = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
client = clickhouse_connect.get_client(host=host, port=port, username=user, password=password)
for stmt in seed.ddl("fpa_cube"):
    client.command(stmt)
client.command("ALTER TABLE fpa_cube.fact_gl_actual "
               "MODIFY SETTING max_bytes_to_merge_at_max_space_in_pool = 1")
PYDDL

echo "==> seeding cube (about 12s)"
"$PY" data/seed_fpa.py --host "$HOST" --port "$PORT" --user "$USER" --password "$PASSWORD" --out data/out

LOST=$(query "SELECT count() FROM (SELECT company, period_month, account, dim_signature_hash FROM fpa_cube.fact_gl_actual GROUP BY ALL HAVING countIf(_version = 1) = 0 AND countIf(_version = 2) > 0)")
if [ "${LOST:-1}" != "0" ]; then
    echo "!!! ${LOST} restated keys have lost their vintage 1 row; AS OF reads would be wrong" >&2
    exit 1
fi

echo "==> done: $(query "SELECT count() FROM fpa_cube.fact_gl_actual") actual rows, both vintages intact"
