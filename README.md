# FP&A Re-Forecast Copilot — Backend

## Demo video

[Watch the demo](https://drive.google.com/file/d/1sOo-8inC3GBFHp1oLkq7XPunaYJHrVQX/view?usp=sharing)

## Setup from scratch

Install Git, Make, Docker with Compose, and Node.js 20.19+ or 22.12+ with npm.
Start Docker, then clone the public repositories into sibling directories:

```bash
mkdir fpa-assignment
cd fpa-assignment
git clone https://github.com/narendrak-aloha/fpa-api-v1.git api
git clone https://github.com/narendrak-aloha/fpa-ui-v1.git ui
cd api
```

Before starting, place the assignment-supplied `seed_fpa.py` in
`api/data/seed_fpa.py` if it is missing from your checkout.

## Start the backend

Keep `api/` and `ui/` next to each other. Complete the
[prerequisites](docs/COMMANDS.md#prerequisites), then run these commands from `api/`:

```bash
make env                 # Create the local configuration file.
make docker-local-run    # Build, set up the databases and start the application.
```

Wait for `==> API on http://localhost:8000`. First startup takes a few minutes.
For plain-English questions, paste your Anthropic key into `ANTHROPIC_API_KEY`
or your Gemini key into `GOOGLE_API_KEY` in `.env`, then select that provider
in the app. You can also use your Claude or Codex subscription with a local
CLI login.
See [provider setup](docs/USAGE.md) for login instructions.

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

## Key decisions and tradeoffs

| Decision | Reason and tradeoff |
|---|---|
| Enforce core guarantees in Postgres | Checks and triggers protect locked plans, prevent self-approval and preserve the audit chain. Formula validation and query scope stay in application code because they require parsing and caller context. |
| Use Temporal for recompute and Agno for conversation | Temporal handles retries, recovery and human approval waits; Agno interprets questions and drafts proposals. This adds orchestration complexity but keeps durable writes separate from model decisions. |
| Compile FinOpsExpr into parameterised SQL | A controlled language validates measures, types and company scope before execution. It requires maintaining a custom parser and compiler. |
| Coordinate an agent team | The leader checks specialist outputs, at the cost of additional model calls, latency and tokens. |
| Choose an explicit bridge convention | Price uses actual quantity and volume uses plan price, assigning their interaction to price. Another convention changes the split, so the chosen method is documented and tested. |

See [design decisions](docs/DELIVERY.md#the-decisions-that-were-arguable) for details.

## With two more weeks

1. Improve plan covenant checks and add more covenant rules to strengthen
   validation and make plan approval and publication more secure.
2. Add an adversarial Agno evaluation suite to CI with a JSON report.

## Tests and verification

| Check | Command | What it verifies |
|---|---|---|
| Backend suite with the services running | `make test` | Backend behavior and integration with the seeded stack. |
| Unit and Temporal replay suite | `uv run --frozen pytest -m 'not integration'` | Unit behavior, workflow tests and recorded-history replay. |
| Frontend tests, from `ui/` | `node --test tests/*.test.js` | Calculation and rendering assertions. |
| Frontend build, from `ui/` | `npm run build` | Production build. |
| Audit chain, with the backend running | `make audit-verify` | Audit hash-chain integrity. |

For host-side backend tests, install uv and Python 3.12, then prepare the environment:

```bash
uv sync --frozen --extra dev --python 3.12
```

See the [recovery walkthrough](docs/RECOMPUTE.md) for worker restart and failure
demonstrations.

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
