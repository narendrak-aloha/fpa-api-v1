# Backend Make commands

Run commands from the `api/` directory. Normal setup only needs `make env`
and `make docker-local-run`; migrations and sample data are handled automatically.

## Prerequisites

Install Make and Docker with Compose, and start Docker before running backend
commands. For the frontend, install Node.js 20.19+ or 22.12+ and npm.
Host-side commands such as `make test-unit` need Python and the project dependencies
installed with `uv sync --frozen --extra dev` (and `uv run make test-unit` to use that environment).

## Command reference

| Command | What it does |
|---|---|
| `make help` | List available commands and descriptions. |
| `make env` | Create `.env` if missing and show its settings. |
| `make env-check` | Show resolved backend environment settings. |
| `make docker-local-run` | Build and start services; Ctrl+C stops them. |
| `make docker-local-run-d` | Start in the background and follow logs; Ctrl+C only stops log viewing. |
| `make docker-local-stop` | Stop services, keeping saved data. |
| `make docker-local-logs` | Follow backend logs. |
| `make docker-shell` | Open a shell in the backend service. |
| `make docker-make-migrations` | Generate a migration: add `m="short description"`. |
| `make docker-migrate` | Apply pending database migrations. |
| `make docker-migrate-down` | Roll back one migration; `rev=006` selects a revision. |
| `make docker-migrate-status` | Show database revision, schema differences and migration history. |
| `make docker-seed-db` | Reload missing sample data and import plan lines. |
| `make docker-reinit` | Delete both application schemas and cube data, then rebuild and seed. |
| `make worker-logs` | Follow recompute worker logs. |
| `make worker-kill` | Kill the worker to test recovery. |
| `make worker-kill-halfway` | Kill the worker partway through recompute; `AT=0.5` sets halfway. |
| `make recompute-check` | Check staged rows and draft line counts after recovery. |
| `make worker-restart` | Restart the worker to resume work and follow its logs. |
| `make plan-state` | Show the selected plan’s state. |
| `make plan-create` | Create a draft plan; use `PLAN=PV-2026-0002` for a new code. |
| `make plan-lock` | Take an original draft through review, approval and locking using demo roles. |
| `make audit-verify` | Verify the audit hash chain. |
| `make audit-tamper` | Alter a historical audit row for the tamper-detection demo. |
| `make audit-untamper` | Remove the demo tampering field. |
| `make bridge` | Run and save the Poland Q2 variance bridge. |
| `make reforecast` | Start a driver re-forecast; `DRIVER=`, `FROM=` and `TO=` set the change. |
| `make progress` | Show workflow phase and counters. |
| `make submit` | Submit as planner and trigger covenant checks. |
| `make approve` | Approve as controller; publication waits for CFO locking. |
| `make reject` | Reject as controller. |
| `make lock` | Lock as CFO and allow publication. |
| `make reforecast-e2e` | Run the command-line re-forecast demo through publication and the ledger. |
| `make cancel` | Cancel the running re-forecast. |
| `make commitment-fail` | Enable Commitment Service failures for recovery testing. |
| `make commitment-ok` | Disable simulated Commitment Service failures. |
| `make commitment-ledger` | Show budget commitments for the selected plan. |
| `make replay-record` | Re-record Temporal histories used by replay tests. |
| `make test` | Run every backend test with the services running. |
| `make test-unit` | Run tests excluding marked database integration tests; Temporal starts a test server. |

Commands that refer to a plan use `PV-2026-0001` by default; add `PLAN=<code>`
to choose another. The approval/demo commands use seeded demo roles.
Migration, reset, tamper and failure commands are for development and demos,
and are not needed for normal startup.

See [usage](USAGE.md) for the workflow sequence, [recovery](RECOMPUTE.md)
for restart demos, and [database documentation](../db/README.md) for migrations.
