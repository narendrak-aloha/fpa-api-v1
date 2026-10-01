# FP&A Re-Forecast Copilot — API

## Run

Requires Docker with Compose. Keep the separate `api/` and `ui/` repositories
in sibling directories.

```bash
cp .env.example .env
# Optional: configure model credentials/settings in .env (see docs/USAGE.md).
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

Or use `make env` followed by `make docker-local-run`. Startup applies migrations,
loads sample data, imports plan lines and settles the seeded plan. Wait for
`==> API on http://localhost:8000`. Stop the foreground stack with Ctrl+C;
`make docker-local-stop` stops services while keeping data volumes.

In another terminal, start the frontend (Node.js 20.19+ or 22.12+):

```bash
cd ../ui
npm ci
npm run dev
```

Open http://localhost:8080. FastAPI is on http://localhost:8000 and Temporal's
UI is on http://localhost:8233. Demo login: `test@planner.com` / `Fpa!12345`.
Queries beginning with `SELECT` run without a model login or API key.

## Documentation

- [Usage, provider setup and re-forecast walkthrough](docs/USAGE.md)
- [Architecture and API reference](docs/ARCHITECTURE.md)
- [Backend component diagram](docs/component_diagram.md)
- [State flows and human approval gates](docs/STATE_FLOWS.md)
- [Durable recompute and recovery](docs/RECOMPUTE.md)
- [Design decisions, limitations and verification](docs/DELIVERY.md)
- [Governance database and migrations](db/README.md)
- [Assignment brief and supplied data](data/README.md)
- [Temporal workflow requirements](data/re_forecast_temporal_workflow_requirements.md)
- [FinOpsExpr compiler](src/fpa_project/dsl/README.md)
- [Agent team and provider adapters](src/fpa_project/agent_team/README.md)
- [Frontend startup](../ui/README.md)
