#!/usr/bin/env bash
# Kill the recompute worker once a running re-forecast is part-way through its
# dirty set, so the durability demo proves a resume from the middle rather
# than a restart from nothing.
#
#   PLAN=PV-2026-0001 AT=0.5 scripts/kill_worker_halfway.sh
#
# Polls the live progress query (the workflow's own counters) and kills the
# worker as soon as processed_rows >= AT x dirty_rows. A finished earlier run
# (phase DONE) still answers the query, so DONE means "not started yet".
# Start this right after the shock is accepted. Then: make worker-restart,
# wait for the run to park, and make recompute-check to see no row was doubled.
set -euo pipefail

cd "$(dirname "$0")/.."
PLAN="${PLAN:-PV-2026-0001}"
AT="${AT:-0.5}"
TOKEN="${TOKEN:-tok-planner}"
COMPOSE="docker compose --env-file .env -f docker/docker-compose.yml"
URL="localhost:8000/api/v1/reforecast/${PLAN}/progress"

echo "==> waiting for ${PLAN}'s run to pass ${AT} of its dirty set"
for _ in $(seq 1 1200); do
    body="$(curl -sS -H "Authorization: Bearer ${TOKEN}" "$URL" || true)"
    verdict="$(printf '%s' "$body" | python3 -c '
import json, sys
at = float(sys.argv[1])
try:
    p = json.load(sys.stdin)
except ValueError:
    print("wait"); sys.exit()
if "phase" not in p:
    print("wait"); sys.exit()
dirty, done = p.get("dirty_rows") or 0, p.get("processed_rows") or 0
phase = p["phase"]
if phase in ("SAVING_DRAFT", "AWAITING_SUBMISSION", "COVENANT_CHECK", "AWAITING_APPROVAL", "AWAITING_LOCK", "PUBLISHING", "COMMITTING", "VARIANCE"):
    print(f"late {phase} {done}/{dirty}")
elif phase == "RECOMPUTING" and dirty and done >= at * dirty and done < dirty:
    print(f"kill {done}/{dirty}")
else:
    print(f"wait {phase} {done}/{dirty}")
' "$AT")"
    case "$verdict" in
        kill*)
            $COMPOSE kill worker >/dev/null
            echo "==> worker killed at ${verdict#kill } rows (RECOMPUTING)"
            echo "    now: make worker-restart   (the log shows 'resuming at offset N' for the partitions it was in)"
            echo "    then, once the run parks: make recompute-check"
            exit 0 ;;
        late*)
            echo "==> too late to kill mid-run: the run is already at ${verdict#late }"
            echo "    use a larger dirty set (an unscoped shock) or a smaller FPA_PARTITION_SIZE"
            exit 1 ;;
    esac
    sleep 0.3
done
echo "==> gave up waiting for the run to start"
exit 1
