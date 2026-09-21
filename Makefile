# Local Docker stack: ClickHouse, Postgres, Temporal, the recompute worker,
# the Commitment Service and the fpa_app-1 app container.
# --env-file: without it compose reads docker/.env, not the repo root.
ENV_FILE := .env
COMPOSE  := docker compose --env-file $(ENV_FILE) -f docker/docker-compose.yml
APP     := fpa-dev
WORKER  := worker
SCHEMA  := fpa_governance
PLAN    ?= PV-2026-0001
# Dev bearer tokens from db/seed.yaml: tok-planner, tok-controller, tok-cfo, tok-analyst-pl.
TOKEN   ?= tok-planner
API     := curl -sS -H 'content-type: application/json' -H "Authorization: Bearer $(TOKEN)"
# Who decides a parked run. AS is also make's built-in assembler variable
# (value "as"), so only an AS given on the command line or in the environment counts.
DECIDER := $(if $(filter command line environment,$(origin AS)),$(AS),tok-cfo)

.PHONY: help env env-check docker-local-run docker-local-run-d docker-local-stop docker-local-logs docker-seed-db docker-reinit docker-shell \
        docker-make-migrations docker-migrate docker-migrate-down docker-migrate-status \
        worker-logs worker-kill worker-kill-halfway recompute-check worker-restart plan-state plan-create plan-lock audit-verify audit-tamper audit-untamper bridge \
        reforecast submit approve reject lock cancel progress reforecast-e2e \
        commitment-fail commitment-ok commitment-ledger replay-record test test-unit

$(ENV_FILE):
	@cp .env.example $(ENV_FILE) && echo "created $(ENV_FILE) from .env.example — edit it to add API keys"

env: $(ENV_FILE) ## Create .env from .env.example if it does not exist yet (API keys)
	@echo "$(ENV_FILE):"
	@grep -vE '^\s*(#|$$)' $(ENV_FILE) | sed 's/^/  /' || true

env-check: $(ENV_FILE) ## Show the values compose will actually use (after .env and shell overrides)
	@$(COMPOSE) config | sed -n '/fpa-dev:/,/volumes:/p' | grep -E '^\s{6}[A-Z_]+:' | sed 's/^ */  /'

help:
	@grep -E '^[a-z0-9-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-20s %s\n", $$1, $$2}'

# Attached: the first run spends ~12s seeding, which `up -d` made look like a hang.
# entrypoint.sh owns migrations and seeding; nothing here repeats them.
docker-local-run: $(ENV_FILE) ## Build and start every service in the foreground (Ctrl+C stops the stack)
	$(COMPOSE) up --build

docker-local-run-d: $(ENV_FILE) ## Same, but in the background, then follow the app log (Ctrl+C leaves it running)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps
	@echo
	@$(COMPOSE) logs -f --tail=40 $(APP)

docker-local-stop: $(ENV_FILE) ## Stop every service, keeping the data volumes
	$(COMPOSE) stop

docker-local-logs: $(ENV_FILE) ## Follow the app container's logs
	$(COMPOSE) logs -f $(APP)

docker-shell: $(ENV_FILE) ## Open a shell inside fpa_app-1
	$(COMPOSE) exec $(APP) bash

# make treats -m's words as extra goals, so collect them here; the catch-all
# below keeps make quiet about them.
ifneq (,$(filter docker-make-migrations,$(MAKECMDGOALS)))
MIGRATION_MSG := $(or $(m),$(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS)))
%:
	@:
endif

docker-make-migrations: $(ENV_FILE) ## Generate a migration from db/models.py (use: make docker-make-migrations -m "add plan comment")
	@test -n "$(MIGRATION_MSG)" || { echo 'usage: make docker-make-migrations -m "short message"'; exit 2; }
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini revision --autogenerate -m "$(MIGRATION_MSG)"
	@echo "Review the new file in db/migrations/versions, then: make docker-migrate"

docker-migrate: $(ENV_FILE) ## Apply all pending migrations (alembic upgrade head)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head

docker-migrate-down: $(ENV_FILE) ## Roll back one migration (use: make docker-migrate-down rev=006 to target a revision)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini downgrade $(or $(rev),-1)

docker-migrate-status: $(ENV_FILE) ## Show the applied revision, pending changes and history
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini current
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini check
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini history

# The entrypoint already waited and migrated; seed only.
docker-seed-db: $(ENV_FILE) ## Re-run both seeders by hand (the app already seeds on first start)
	$(COMPOSE) exec $(APP) python -m db.seed
	$(COMPOSE) exec $(APP) scripts/seed_clickhouse.sh
	$(COMPOSE) exec $(APP) python -m db.import_plan_lines

# psql takes host, user, password and database from the container's PG* variables.
# RESEED_CUBE=1 makes the seeder drop the cube before rebuilding it.
docker-reinit: $(ENV_FILE) ## Drop the governance schema and the cube, then rebuild and seed both
	$(COMPOSE) exec $(APP) psql -c "DROP SCHEMA IF EXISTS $(SCHEMA) CASCADE"
	$(COMPOSE) exec $(APP) psql -c "DROP SCHEMA IF EXISTS commitment_service CASCADE"
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	$(COMPOSE) exec $(APP) python -m db.seed
	$(COMPOSE) exec -e RESEED_CUBE=1 $(APP) scripts/seed_clickhouse.sh
	$(COMPOSE) exec $(APP) python -m db.import_plan_lines

# --------------------------------------------------------------------------
# Durable recompute. PLAN=<code> overrides the plan version (default PV-2026-0001).
# --------------------------------------------------------------------------
worker-logs: ## Follow the recompute worker's log
	$(COMPOSE) logs -f $(WORKER)

worker-kill: ## Kill the worker mid-run (the durability demo; the run must survive)
	$(COMPOSE) kill $(WORKER)
	@echo "worker killed. Bring it back with: make worker-restart"

worker-kill-halfway: ## Kill the worker once the running re-forecast is part-way through its dirty set (AT=0.5)
	@PLAN=$(PLAN) AT=$(or $(AT),0.5) TOKEN=$(TOKEN) scripts/kill_worker_halfway.sh

recompute-check: ## After a resume: staged rows raw vs FINAL, and draft lines vs distinct grain, for the latest revision
	@REV=$$($(COMPOSE) exec -T $(APP) psql -tAc "SELECT max(p.revision) FROM $(SCHEMA).plan_publication p JOIN $(SCHEMA).plan_version v ON v.plan_version_id = p.plan_version_id WHERE v.plan_version_code = '$(PLAN)'"); \
	echo "revision $$REV"; \
	$(COMPOSE) exec -T clickhouse clickhouse-client --password fpa -q "SELECT scenario_id, count() AS raw, uniqExact(company, period_month, account, dim_signature_hash) AS distinct_keys FROM fpa_cube.fact_plan_line_staged WHERE plan_version = '$(PLAN)' AND revision = $$REV GROUP BY scenario_id ORDER BY scenario_id FORMAT PrettyCompact"; \
	$(COMPOSE) exec -T $(APP) psql -c "SELECT v.plan_version_code, l.scenario_code, count(*) AS lines, count(DISTINCT (l.company_code, l.period_month, l.account_code, l.dim_signature_hash)) AS distinct_grain FROM $(SCHEMA).plan_version_line l JOIN $(SCHEMA).plan_version v USING (plan_version_id) WHERE v.plan_version_code = '$(PLAN)-R'||$$REV GROUP BY 1, 2"

worker-restart: ## Start the worker again; a parked or half-done run picks up where it was
	$(COMPOSE) up -d $(WORKER)
	@$(COMPOSE) logs -f --tail=20 $(WORKER)

plan-state: ## Show the plan version's state and where it may go next
	@$(API) localhost:8000/api/v1/plan-versions/$(PLAN) | python3 -m json.tool

plan-create: ## Author a new DRAFT plan version as the planner (PLAN=PV-2026-0002)
	@$(API) -X POST localhost:8000/api/v1/plan-versions -d '{"plan_version_code":"$(PLAN)"}' | python3 -m json.tool

plan-lock: ## Take a DRAFT plan through IN_REVIEW (planner), APPROVED (controller) and LOCKED (cfo)
	@$(API) -X POST localhost:8000/api/v1/plan-versions/$(PLAN)/transition -d '{"to_state":"IN_REVIEW"}' | python3 -m json.tool
	@$(API) localhost:8000/api/v1/plan-versions/$(PLAN) | \
	  python3 -c 'import sys,json; p=json.load(sys.stdin); print(json.dumps({"expected_version":p["row_version"],"covenant_ok":True,"note":"covenant reviewed"}))' | \
	  curl -sS -H 'content-type: application/json' -H "Authorization: Bearer tok-controller" -X PUT \
	  localhost:8000/api/v1/plan-versions/$(PLAN)/covenant --data-binary @- | python3 -m json.tool
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer tok-controller" -X POST \
	  localhost:8000/api/v1/plan-versions/$(PLAN)/transition -d '{"to_state":"APPROVED","note":"covenant reviewed"}' | python3 -m json.tool
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer tok-cfo" -X POST \
	  localhost:8000/api/v1/plan-versions/$(PLAN)/transition -d '{"to_state":"LOCKED"}' | python3 -m json.tool

audit-verify: ## Recompute the audit hash chain; fails loudly on any altered row
	@$(API) localhost:8000/api/v1/audit/verify | python3 -m json.tool

audit-tamper: ## Alter one historical audit row by hand (trigger off, UPDATE, trigger on) so audit-verify fails
	$(COMPOSE) exec -T $(APP) psql -c "ALTER TABLE $(SCHEMA).audit_event DISABLE TRIGGER audit_event_no_update" \
	  -c "UPDATE $(SCHEMA).audit_event SET payload = payload || '{\"tampered\": true}' WHERE audit_event_id = (SELECT min(audit_event_id) FROM $(SCHEMA).audit_event)" \
	  -c "ALTER TABLE $(SCHEMA).audit_event ENABLE TRIGGER audit_event_no_update"
	@echo "now: make audit-verify"

audit-untamper: ## Put the tampered row back
	$(COMPOSE) exec -T $(APP) psql -c "ALTER TABLE $(SCHEMA).audit_event DISABLE TRIGGER audit_event_no_update" \
	  -c "UPDATE $(SCHEMA).audit_event SET payload = payload - 'tampered' WHERE audit_event_id = (SELECT min(audit_event_id) FROM $(SCHEMA).audit_event)" \
	  -c "ALTER TABLE $(SCHEMA).audit_event ENABLE TRIGGER audit_event_no_update"

bridge: ## Run the Poland Q2 bridge and persist the report (DSL= overrides)
	@$(API) -X POST localhost:8000/api/v1/bridge -d "{\"dsl\":\"$(or $(DSL),SELECT services_revenue BY practice, grade WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE)\"}" | python3 -m json.tool

reforecast: ## Shock a driver and start a re-forecast as TOKEN's user (DRIVER=, FROM=, TO=)
	@$(API) -X POST localhost:8000/api/v1/reforecast \
	  -d '{"plan_version_code":"$(PLAN)","driver_code":"$(or $(DRIVER),utilisation)","from_value":$(or $(FROM),0.75),"to_value":$(or $(TO),0.70)}' | python3 -m json.tool

progress: ## Show the running workflow's phase and counters
	@$(API) localhost:8000/api/v1/reforecast/$(PLAN)/progress | python3 -m json.tool

submit: ## Submit the recomputed draft as the planner; the system checks covenants (breach rejects it, pass -> IN_REVIEW)
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer $(or $(AS),tok-planner)" -X POST \
	  localhost:8000/api/v1/reforecast/$(PLAN)/submit -d '{"comment":"$(or $(NOTE),reviewed the recomputed lines)"}' | python3 -m json.tool

approve: ## Approve the submitted plan as a controller (IN_REVIEW -> APPROVED; neither locks nor publishes)
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer $(or $(AS),tok-controller)" -X POST \
	  localhost:8000/api/v1/reforecast/$(PLAN)/decision -d '{"approved":true,"comment":"$(or $(NOTE),approved)"}' | python3 -m json.tool

reject: ## Reject the submitted plan as a controller
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer $(or $(AS),tok-controller)" -X POST \
	  localhost:8000/api/v1/reforecast/$(PLAN)/decision -d '{"approved":false,"comment":"$(or $(NOTE),rejected)"}' | python3 -m json.tool

lock: ## Lock the approved plan as the CFO (APPROVED -> LOCKED); the system then publishes it
	@curl -sS -H 'content-type: application/json' -H "Authorization: Bearer $(DECIDER)" -X POST \
	  localhost:8000/api/v1/reforecast/$(PLAN)/lock -d '{"approved":true,"comment":"$(or $(NOTE),locked)"}' | python3 -m json.tool

reforecast-e2e: ## Whole re-forecast with no UI: shock, submit, covenant, approve, lock, publish, then the ledger (FAIL=1, DECISION=reject, DRIVER=, FROM=, TO=)
	@PLAN=$(PLAN) DRIVER=$(or $(DRIVER),utilisation) FROM=$(or $(FROM),0.75) TO=$(or $(TO),0.70) \
	  DECISION=$(or $(DECISION),approve) FAIL=$(or $(FAIL),0) scripts/reforecast_e2e.sh

cancel: ## Cancel the running re-forecast
	@$(API) -X POST localhost:8000/api/v1/reforecast/$(PLAN)/cancel | python3 -m json.tool

commitment-fail: ## Make the Commitment Service fail every call (RATE=, MODE=error|timeout|mixed)
	@curl -sS -X POST localhost:8100/admin/failure-rate -H 'content-type: application/json' \
	  -d '{"rate":$(or $(RATE),1.0),"mode":"$(or $(MODE),error)"}' | python3 -m json.tool

commitment-ok: ## Put the Commitment Service back to a zero failure rate
	@curl -sS -X POST localhost:8100/admin/failure-rate -H 'content-type: application/json' \
	  -d '{"rate":0.0}' | python3 -m json.tool

commitment-ledger: ## Show what the Commitment Service currently holds
	@curl -sS "localhost:8100/commitments?plan_version=$(PLAN)" | python3 -m json.tool

replay-record: ## Re-record the workflow histories the replay test runs against
	$(COMPOSE) exec $(APP) python -m tests.record_history

test: ## Run every test, including the ones that need the stack
	$(COMPOSE) exec $(APP) python -m pytest

test-unit: ## Run only the tests that need nothing running
	python -m pytest -m "not integration"
