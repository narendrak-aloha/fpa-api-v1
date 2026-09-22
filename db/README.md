# Postgres governance store

The database is intentionally separate from the ClickHouse cube. ClickHouse
holds large actual/plan facts; Postgres holds the trusted control plane: model
registry, effective-dated drivers, plan state, scenario overrides, approvals,
FX assumptions, variance evidence, disclosure metadata, and audit history.

Database-level protections include locked-plan write guards, non-empty driver
derivation traces, amount = quantity × unit price, segregated approval, stored
covenant gating, and append-only audit events. Application code still owns
formula parsing, workflow orchestration, hash-chain verification, and the
ClickHouse publication transaction.

The schema is managed with **Alembic** migrations generated from SQLAlchemy
models, and the seed data is loaded from **YAML**. Everything lives in the
Postgres schema `fpa_governance`.

## Layout

Alembic and the seed loader run inside the `fpa_app-1` container; the same
commands work on the host against the published ports.

```text
db/
  alembic.ini              Alembic config (URL, numbered file names)
  models.py                SQLAlchemy models: the source for --autogenerate
  seed.yaml                Seed data
  seed.py                  Idempotent YAML loader: python -m db.seed
  migrations/
    env.py                 Schema scoping, numbered revision ids, empty-diff guard
    script.py.mako         Template for new migration files
    versions/
      001_create_schema_and_access_control.py
      002_create_reference_dimensions.py
      003_create_planning_model_registry.py
      004_create_plan_versions_and_scenarios.py
      005_create_plan_approval.py
      006_create_variance_reporting.py
      007_create_audit_and_disclosure_log.py
      ...                    new migrations land here as 008_, 009_, ...
  runtime_roles.sql        Least-privilege role sketch, run by hand (not applied automatically)
```

## Setup

Everything in Docker, where Alembic and the seeders run inside `fpa_app-1`:

```bash
make docker-local-run      # start the stack; the app container migrates and seeds both stores
make docker-seed-db        # re-run both seeders by hand
make docker-reinit         # drop the governance schema and the cube, then rebuild and seed
```

Or from the host, against the same containers:

```bash
uv sync --frozen --extra dev     # every dependency, from uv.lock
scripts/bootstrap.sh             # docker compose up -d + both seeders
```

`scripts/bootstrap.sh` runs `docker compose up -d` and then the two seed
scripts. To do it by hand, or to run only one of them:

```bash
docker compose -f docker/docker-compose.yml up -d postgres   # published on localhost:5431
scripts/seed_postgres.sh          # alembic upgrade head + python -m db.seed
scripts/seed_clickhouse.sh        # loads the cube only when it is empty
RESEED_CUBE=1 scripts/seed_clickhouse.sh   # drop and rebuild the cube

# or the underlying commands, from the project root
.venv/bin/alembic -c db/alembic.ini upgrade head
.venv/bin/python -m db.seed
```

Both scripts wait until their database accepts connections and can be re-run
at any time: migrations skip steps already applied, the YAML load skips existing rows,
and the cube is skipped when it already holds data.

The connection defaults to `postgresql+psycopg://postgres:fpa@localhost:5431/fpa`
(set in `alembic.ini`). Set `FPA_GOVERNANCE_DB_URL` to point both Alembic and
the seed loader somewhere else.

## Migrations

Tables are created in categories, one migration each, in dependency order:

| Revision | Name | Tables and database objects |
|---|---|---|
| 001 | create schema and access control | `pgcrypto` extension, `fpa_governance` schema, `app_user`, `role`, `user_role` |
| 002 | create reference dimensions | `dim_company`, `dim_account`, `dim_cost_center`, `ledger_vintage` |
| 003 | create planning model registry | `planning_model`, `planning_dimension`, `planning_measure`, `plan_driver` + `driver_effective_idx` |
| 004 | create plan versions and scenarios | `plan_state_transition`, `plan_version`, `scenario_set` (+ `one_base_scenario_per_plan`), `scenario_driver_override`, `plan_fx_rate`, `plan_version_line` (+ `plan_line_lookup_idx`); function `reject_locked_plan_write` with triggers `plan_version_lock_guard` and `plan_line_lock_guard` |
| 005 | create plan approval | `plan_approval`; function `validate_plan_approval` with trigger `plan_approval_guard` |
| 006 | create variance reporting | `variance_report`, `variance_report_line` |
| 007 | create audit and disclosure log | `llm_disclosure_log`, `audit_event` (+ `audit_entity_idx`); function `audit_event_append_only` with trigger `audit_event_no_update` |

The Alembic version table is `fpa_governance.alembic_version`.

Common commands (from the project root):

```bash
alembic -c db/alembic.ini upgrade head        # apply all migrations
alembic -c db/alembic.ini current             # show the applied revision, e.g. 007 (head)
alembic -c db/alembic.ini history             # list revisions
alembic -c db/alembic.ini downgrade 006       # step back to a revision
alembic -c db/alembic.ini downgrade base      # remove everything
alembic -c db/alembic.ini check               # fail if models.py and the database differ
```

### Adding a migration

1. Change or add a model in `db/models.py`. Every model needs a primary key.
2. Generate the migration:
   ```bash
   make docker-make-migrations -m "add plan comment"   # in Docker (m="..." also works)
   alembic -c db/alembic.ini revision --autogenerate -m "add plan comment"   # or on the host
   ```
3. Review the generated file, then apply it:
   ```bash
   make docker-migrate          # or: alembic -c db/alembic.ini upgrade head
   make docker-migrate-status   # current revision, pending changes, history
   make docker-migrate-down     # roll back one (rev=006 targets a revision)
   ```

`make docker-make-migrations` upgrades to head first, so autogenerate compares
against an up-to-date database. Restarting the stack, or running
`make docker-local-run`, applies anything pending.

Revision ids are **sequential numbers, not random hashes**. `env.py` takes the
highest numeric revision and adds one, so the next files are
`008_add_plan_comment.py`, `009_...`, each with `revision = "008"` and
`down_revision = "007"`. This applies to both `revision --autogenerate` and a
plain `revision -m "..."` (hand-written migration), because `alembic.ini` sets
`revision_environment = true`. Passing `--rev-id` explicitly overrides the
number.

If `--autogenerate` finds no model changes, no file is created. The database
must be at head before autogenerating.

Autogenerate does not detect triggers, functions, or check-constraint changes.
Write those with `op.execute(...)` / `op.create_check_constraint(...)` in the
migration, as 004, 005 and 007 do.

### Model conventions

- All models share `Base.metadata` with `schema="fpa_governance"`.
- Constraint names follow a naming convention: `pk_<table>`,
  `fk_<table>_<column>_<referred_table>`, `uq_<table>_<column>`,
  `ck_<table>_<name>`; multi-column unique constraints and indexes are named
  explicitly. Keep migrations and models on the same names so `alembic check`
  reports no differences.

## Seed data

`seed.yaml` holds the data as **one list entry per
row**. Every entry starts with `table`, the target `<schema>.<table>`, followed
by that row's columns. Entries are grouped by table with a comment header:

```yaml
# dim_company (20 rows)
- table: fpa_governance.dim_company
  company_code: RTUS1
  company_name: RealTech US Operations 1
  country_code: US
  region: AMER
  functional_currency: USD
- table: fpa_governance.dim_company
  company_code: RTUS2
  ...

# plan_fx_rate (108 rows)
- table: fpa_governance.plan_fx_rate
  plan_version_code: PV-2026-0001
  period_month: 2026-01-01
  from_currency: CAD
  to_currency: USD
  rate: '1.36'
```

The loader groups entries by `table` and inserts tables in foreign-key order, so
entries may appear in any order in the file. An entry without `table`, or with a
table that has no model, stops the load with its record number.

Other keys are column names, with three exceptions resolved by the loader:

- `model_code` in `planning_dimension`, `planning_measure`, `plan_driver` and
  `plan_version` is looked up to `planning_model.model_id`.
- `plan_version_code` in `scenario_set` and `plan_fx_rate` is looked up to
  `plan_version.plan_version_id`.
- `audit_event.event_hash` is computed when not given.

Generated ids (`model_id`, `driver_id`, `plan_version_id`, …) and columns with
database defaults (`created_at`, `active`, `status`, …) are omitted. Codes that
look numeric (account codes, `'0'`) are quoted so they stay text; numeric
columns such as `rate` are quoted to keep exact decimals.

Row counts: `role` 5, `app_user` 4, `user_role` 7, `dim_company` 20,
`dim_account` 25, `dim_cost_center` 54, `ledger_vintage` 2, `planning_model` 1,
`planning_dimension` 19, `planning_measure` 8, `plan_driver` 6,
`plan_state_transition` 6, `plan_version` 1, `scenario_set` 3,
`plan_fx_rate` 108, `audit_event` 1.

To add data, add an entry with `table: fpa_governance.<table>` and the row's
columns. Total: 270 rows.

```bash
python -m db.seed                       # load db/seed.yaml
python -m db.seed --file other.yaml     # load another file with the same structure
```

The loader inserts with `ON CONFLICT DO NOTHING` in one transaction, so it can
be re-run safely; it prints the rows inserted per table. On an empty database:

```text
role +5, app_user +4, user_role +7, dim_company +20, dim_account +25,
dim_cost_center +54, ledger_vintage +2, planning_model +1,
planning_dimension +19, planning_measure +8, plan_driver +6,
plan_state_transition +6, plan_version +1, scenario_set +3,
plan_fx_rate +108, audit_event +1
```

The audit `event_hash` is `sha256("actor|entity_type|entity_id|action|<compact payload JSON>")`
over the stored payload, so it can be recomputed from the stored row.
