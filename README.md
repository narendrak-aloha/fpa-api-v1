# FP&A Re-Forecast Copilot — Backend

## Start the backend

Keep `api/` and `ui/` next to each other. Complete the
[prerequisites](docs/COMMANDS.md#prerequisites), then run these commands from `api/`:

```bash
make env                 # Create the local configuration file.
make docker-local-run    # Build, set up the databases and start the application.
```

Wait for `==> API on http://localhost:8000`. First startup takes a few minutes.
Model credentials are optional for setup; see [provider setup](docs/USAGE.md)
to enable plain-English questions.

## Start the frontend

In a second terminal:

```bash
cd ../ui                 # Open the frontend repository.
npm ci                   # Install the frontend dependencies.
npm run dev              # Start the frontend at http://localhost:8080.
```

Open http://localhost:8080 and sign in with `test@planner.com` / `Fpa!12345`.

## Useful commands

Run these from `api/`:

```bash
make docker-local-logs   # Follow the backend logs.
make docker-local-stop   # Stop the backend services and keep saved data.
make test                # Run all backend tests with the services running.
make help                # List the available Make commands and descriptions.
```

Ctrl+C stops the foreground backend or frontend started above.

## Documentation

- [All Make commands, briefly explained](docs/COMMANDS.md)
- [Using the app and setting up model providers](docs/USAGE.md)
- [Architecture and API reference](docs/ARCHITECTURE.md)
- [Backend component diagram](docs/component_diagram.md)
- [Approval and workflow states](docs/STATE_FLOWS.md)
- [Recompute and recovery](docs/RECOMPUTE.md)
- [Design decisions, limitations and verification](docs/DELIVERY.md)
- [Database and migrations](db/README.md)
- [Assignment and supplied data](data/README.md)
- [Temporal workflow requirements](data/re_forecast_temporal_workflow_requirements.md)
- [Query language](src/fpa_project/dsl/README.md)
- [Agent team](src/fpa_project/agent_team/README.md)
- [Frontend README](../ui/README.md)
