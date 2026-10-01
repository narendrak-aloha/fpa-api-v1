# FinOpsExpr Agent Team

`fpa_project.agent_team` translates natural-language planning requests into a
validated `AgentPlan` containing FinOpsExpr DSL. The model proposes DSL; compiler-backed tools validate and execute it through
an injected database adapter. The model receives no arbitrary SQL, filesystem
or code execution tool. Model changes require governed human approval.

## Safety contract

- Pydantic models reject extra output fields and require typed structured output.
- `mask_for_llm()` recursively masks employee identifiers, national IDs,
  compensation, customer names, and related fields before model context is sent.
- `PlanningRegistry` validates measures, dimensions, scenarios, predicates, and
  time-function offsets against `data/schema_snapshot.json`.
- Invalid syntax or registry references return structured `ValidationIssue`
  objects and no plan.
- `AgentPlan` either carries `dsl`, or sets `out_of_scope=true` with an empty
  `dsl` for questions the cube cannot answer. A plan with neither, or a refusal
  that still carries DSL, is rejected. Out-of-scope plans return
  `OUT_OF_SCOPE` and are never compiled or executed; a refusal that contains a
  number is replaced with a fixed message.
- The plan's `assumptions` are carried into `AgentFPAResponse.assumptions`.
- Historical questions retain an independently resolved constraint. Explicit
  `AS OF` timestamps and named month closes (with an explicit or unambiguous
  question year) resolve through compiler-owned, parameterised vintage lookups.
  Missing or ambiguous closes require clarification before a model is called.
  Verified timestamps reach both the leader and delegated members. An Agno
  guardrail checks each evidence tool call and final plan, and the orchestrator
  checks again before execution: missing or changed `AS OF` cannot fall back to
  current data. The existing initial attempt plus one repair limit applies;
  no new agent tool or DSL syntax is added.
- `ArithmeticVerificationPostHook` rejects any number in the narrative that is
  not present in the returned rows, and the orchestrator retries the team at
  most twice (the initial attempt and one repair).
- The Agno adapter exposes compiler-backed query tools and draft proposal tools.
  `propose_driver` uses durable human confirmation through `proposals.py`;
  human planners receive `propose_reforecast` for a draft request. Agents cannot
  approve or apply their own proposals.
- Terminal logging is enabled for major lifecycle events. Logs contain only
  run IDs, statuses, counts, validation codes, and exception types; raw requests,
  DSL, SQL, parameters, and sensitive row values are not logged.
- External request/response envelopes are written to
  `logs/fpa_external_audit.jsonl` by default. Set `FPA_EXTERNAL_AUDIT_LOG` to
  change the path. The audit file includes the DSL and compiled SQL shape for
  traceability, but masks sensitive fields and never stores API keys.

## Usage

```python
from fpa_project.agent_team import FinOpsPlanner, PlanningRequest

planner = FinOpsPlanner()
request = PlanningRequest(request="Show services revenue by country", context={"customer_name": "Acme"})
safe_request = planner.prepare(request)
response = planner.generate(request, {
    "dsl": "SELECT services_revenue BY geo_country",
    "explanation": "Revenue grouped by country",
})
```

Agno and the provider SDKs are regular project dependencies (`uv sync --frozen`).
The core validation layer does not call them, so it also works in tests and
offline workers.

## Manual end-to-end execution

Yes. Provide an authenticated `UserScope`, a connected `clickhouse-connect`
client, and an agent-produced `AgentPlan`:

```python
import clickhouse_connect
from fpa_project.agent_team import (
    FPAOrchestrator, FPATools, PlanningRequest, UserScope, clickhouse_executor,
)

client = clickhouse_connect.get_client(host="localhost", username="...", password="...")
tools = FPATools(
    UserScope(user_id="analyst-1", allowed_companies=frozenset({"C001"})),
    executor=clickhouse_executor(client),
)
response = FPAOrchestrator(tools).finalize(
    PlanningRequest(request="Revenue by country"),
    {"dsl": "SELECT services_revenue BY geo_country FOR PERIOD 2026-Q2"},
    narrative="",
)
print(response.model_dump_json(indent=2))
```

The flow is NL/request boundary → masked context → DSL parse and registry
validation → scoped parameterized SQL compilation → ClickHouse execution →
masked result rows. The module does not create the ClickHouse connection. A
real database test requires reachable ClickHouse credentials
(`clickhouse-connect` is installed with the project). Unit tests use fake
executors; marked integration tests query the Docker stack.

## Natural-language execution with the team

`run_with_team` sends the masked request to an Agno team, takes the returned
`AgentPlan` and passes it through `finalize`. Build the team with the scoped
toolset so members can list metrics and dimensions and run queries through the
compiler:

```python
from fpa_project.agent_team import build_agno_team
from fpa_project.agent_team.claude_code_model import ClaudeCodeModel

team = build_agno_team(model=ClaudeCodeModel(), toolset=tools)
response = FPAOrchestrator(tools).run_with_team(
    team, PlanningRequest(request="What was services revenue by practice in Q2 2026?"),
)
```

`build_agno_team` creates a `coordinate`-mode team with `QueryAgent`,
`VarianceAgent` and `PlanningAgent`. Their shared instructions contain the
FinOpsExpr grammar guide (`FINOPSEXPR_GUIDE` in `team.py`), the rule that every
number in the explanation must come from `run_finops_query` rows, and the
out-of-scope rule. Build the team per request when scope differs between
callers, because the toolset holds the `UserScope`.

`AgentFPAResponse.execution_status` is one of `SUCCESS`, `VALIDATION_ERROR`,
`REJECTED_SCOPE`, `OUT_OF_SCOPE`, `AWAITING_APPROVAL`, `DRAFT`, `REFUSED`
or `REFORECAST_PROPOSED`.

## Claude subscription model (`ClaudeCodeModel`)

`claude_code_model.py` provides an Agno `Model` backed by the Claude Agent SDK.
It authenticates with the local Claude Code login (`claude`, Pro/Max
subscription) instead of an API key, so the same Agno team can run on a
subscription during local development.

Agno still owns the loop: leader, member delegation, tool execution and
`output_schema` parsing. Each `invoke` renders Agno's messages and tool schemas
into a prompt and makes one SDK call with:

- `tools=[]`, so Claude Code's built-in tools (shell, files, web) are disabled;
- `setting_sources=[]`, so local Claude Code settings are not loaded;
- a JSON-schema `output_format` whose result is either
  `{"action": "call_tools", "tool_calls": [...]}`, which is converted to Agno
  tool calls that Agno executes, or `{"action": "respond", "content": "..."}`,
  which Agno parses into `AgentPlan`.

SDK failures are raised as Agno `ModelProviderError`. Set
`FPA_CLAUDE_CODE_MODEL` to choose a model; otherwise the Claude Code default is
used. If `ANTHROPIC_API_KEY` is present in the environment the CLI uses it
instead of the subscription.

Limitations: each call starts a Claude Code process (a few seconds), tool
calling is carried through structured output rather than native tool use, the
full transcript is resent on every call, and there is no streaming. Use
`agno.models.anthropic.Claude` with an API key for shared deployments.

## Codex subscription model (`CodexModel`)

`codex_model.py` adds the official `openai-codex` Python SDK through the same
Agno `Model` interface. It inherits only the Claude adapter's transport-independent
message rendering, response conversion and sync/async invocation methods;
the Claude implementation is unchanged. Every turn uses a fresh ephemeral
Codex thread and an isolated home containing only the file-backed ChatGPT login.
The SDK's matching CLI runtime is installed by the locked Python dependency.

Agno keeps the existing prompts, delegation, governed tool execution, guardrails,
disclosure logging and `AgentPlan` parsing. Codex receives the same transcript
and tool catalog, plus a transport instruction to encode tool arguments as JSON
strings for its strict output schema; the adapter decodes these into ordinary
Agno tool calls. Native shell, web, apps, plugins and other capabilities are
disabled; a deny-all `PreToolUse` hook blocks remaining local tools, including
`apply_patch`, with read-only sandboxing and denied escalation as a second boundary.
Host settings, skills and project instructions are not loaded. SDK failures,
invalid output and timeouts use Agno's existing `ModelProviderError` path.

`FPA_CODEX_MODEL` optionally chooses the model. `FPA_CODEX_HOME` selects the
login directory; otherwise `CODEX_HOME` or `~/.codex` is used. Run
`codex -c 'cli_auth_credentials_store="file"' login` with ChatGPT. API keys
are cleared from the SDK environment and API-key-only accounts are rejected.
SDK token refreshes are copied back atomically without copying settings or
history, so the login directory must be writable. Calls have a 120-second
transport timeout. As with Claude, the full transcript is resent per call
and the streaming methods yield one completed model response.

## Calculation evidence

With `FPATools(include_calculations=True)`, supported plain ratio queries project
their numerator and denominator from the same scoped SQL query. Helper columns
are stripped from rows and recorded separately for the API's `row_calculations`.
Window queries and unsupported formulas keep their original SQL. Bridge citation
calculations use returned quantities, prices and FX rates; missing values are
disclosed instead of inferred. See [usage](../../../docs/USAGE.md).
