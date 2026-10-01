# Using the FP&A copilot

Run commands below from the API repository root unless a `cd` is shown.

## Use the frontend

The backend serves no page. With Node.js 20.19+ or 22.12+, start the separate UI
repository alongside this one:

```bash
cd ../ui
npm ci
npm run dev
```

Open **http://localhost:8080**; it proxies `/api` to the API on :8000. Sign in
with a demo email and password **`Fpa!12345`**: `test@analyst.com` reads Poland,
`test@planner.com` drafts requests, `test@controller.com` reviews and approves,
and `test@cfo.com` locks plans and decides final workflow approvals. The seeded
bearer tokens remain available through **Demo accounts** and for command-line calls.

- **Quick test:** click `SELECT services_revenue BY company FOR PERIOD 2026-Q2`.
- **Plain-English questions:** pick a model and ask, e.g. *"What was services
  revenue by practice in Q2 2026?"* (30–60 s).
  - **Claude (subscription):** no API key, uses your Claude Code login (Linux).
  - **Codex (subscription):** no API key, uses your Codex ChatGPT login (Linux).
  - **Claude API key** / **Gemini:** needs the key in `.env`.

For Codex, install the locked dependencies with `uv sync --frozen --extra dev`,
then sign in with `codex -c 'cli_auth_credentials_store="file"' login` using
ChatGPT. The Python SDK bundles its compatible runtime; the `codex` CLI is
only needed for this login step. If no CLI is on your PATH, use the bundled one:

```bash
uv run python -c 'from codex_cli_bin import bundled_codex_path; import subprocess; subprocess.run([str(bundled_codex_path()), "-c", "cli_auth_credentials_store=\"file\"", "login"], check=True)'
```

Select **Codex (subscription)** in the existing model dropdown. For API callers
that omit `provider`, set `FPA_LLM_PROVIDER=codex` in `.env`; an explicit request
provider still wins. The default remains `claude-code`. `FPA_CODEX_MODEL` is an
optional model override; blank uses the SDK default. `FPA_CODEX_HOME` optionally
points to the login directory (otherwise `CODEX_HOME` or `~/.codex`). Compose
mounts that directory so the API can retain refreshed login tokens. Use a
file-backed ChatGPT login; keyring-only and API-key-only logins are not used by
this subscription provider. Existing Claude settings and credentials are unchanged.

The **Ask** tab keeps all provider choices visible. A missing API key produces
an error naming the required environment variable before a model call. Keys are
configured on the API server, never entered into the browser. Direct FinOpsExpr
queries beginning with `SELECT` run without a model provider.

In an answer, use **Show explanation** to inspect DSL, SQL, citations and member
attribution. Each result/source row has **Show** / **Hide** for its calculation:
formula, available operands and result. Click a bridge leg to see its source
rows and vintage; **Collapse** hides the source table. Recorded vintage changes
appear as a comparison table. A utilisation move from 75% to 60% is shown as
−15 percentage points and −20% relative to the starting value.

## Re-forecast a driver

**On the page (the main path).** Sign in as the planner and ask in words, for example
*"Drop Poland utilisation to 72% and re-run the second half"*. The agent team drafts a
re-forecast request (driver, value, companies, months). Confirm the draft in
Ask, then open it under the relevant plan in the **Plans** tab. Then:

1. **Planner** confirms the draft to start recompute and reviews the resulting lines.
2. **Planner** submits; the workflow checks every active covenant rule across scenarios.
   A breach closes the request without publishing or committing.
3. **Controller**, a different human, approves or rejects the passing submitted draft.
4. **CFO**, distinct from requester and approver, locks or declines.
   Locking publishes to the cube and reserves budget with the Commitment Service.

Every state and who may move it: [STATE_FLOWS.md](STATE_FLOWS.md).

**From the command line (manual path).** Move a driver directly; the plan recomputes, parks
for the planner to submit, a controller to approve and the CFO to lock, then publishes and commits. Watch it run at **http://localhost:8233**.

```bash
make plan-lock          # settle a manually created plan; Docker startup already settles the seeded plan
make reforecast         # utilisation 0.75 -> 0.70
make progress           # phase, dirty rows, processed rows, while it runs
make submit             # planner submits; the system checks covenants
make approve            # controller, once progress shows AWAITING_APPROVAL
make lock               # CFO, once it shows AWAITING_LOCK; then it publishes
```

Kill the worker with `make worker-kill` at any point and bring it back with
`make worker-restart`: the run carries on from where it was. Full walkthrough,
including the failure cases, in [RECOMPUTE.md](RECOMPUTE.md).
