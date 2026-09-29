# Component diagram: the backend

What the backend is made of, which part talks to which, and where each boundary
sits. [ARCHITECTURE.md](ARCHITECTURE.md) gives the design reasoning; this file
shows the component boundaries.

One rule explains most of the shape: **the model proposes, the compiler
decides, the database enforces.** Every arrow below either carries a proposal
or crosses a boundary that checks one.

---

## 1. The whole backend

Three processes of our own, three backing services. The three share one image
and one codebase; what each becomes is decided by `FPA_ROLE` and the command it
is started with.

```mermaid
flowchart TB
    UI["Browser<br/><i>fpa-ui-v2</i>"]

    subgraph APP["fpa_app-1 · FastAPI (app.py)"]
        AUTH["Authentication<br/><i>governance.authenticate</i>"]
        READ["Read endpoints<br/><i>query, bridge, citations</i>"]
        WRITE["Write endpoints<br/><i>plan versions, re-forecast</i>"]
    end

    subgraph AGENT["Agent team · agent_team/"]
        ORCH["FPAOrchestrator<br/><i>planner.py — plain code</i>"]
        TEAM["FPATeam + 3 members<br/><i>team.py — the model</i>"]
        GUARD["Guardrails<br/><i>masking, hooks, security</i>"]
    end

    subgraph CORE["Query and governance"]
        DSL["DSL compiler<br/><i>dsl/compiler.py</i>"]
        GOV["Governance<br/><i>governance.py</i>"]
        BRIDGE["Bridge service<br/><i>bridge_service.py</i>"]
    end

    subgraph WORKER["fpa_worker-1 · Temporal worker"]
        WF["PlanRecomputeWorkflow<br/><i>workflows.py</i>"]
        ACT["23 activities<br/><i>activities.py</i>"]
        ENG["Recompute arithmetic<br/><i>engine.py — pure</i>"]
    end

    COMMIT["fpa_commitment-1<br/>Commitment service<br/><i>its own process + schema</i>"]
    LLM["Model provider<br/><i>Claude CLI / API / Gemini</i>"]

    PG[("Postgres<br/><i>governance, audit</i>")]
    CH[("ClickHouse<br/><i>the cube</i>")]
    TMP[("Temporal<br/><i>workflow history</i>")]

    UI -->|HTTPS + bearer token| APP
    READ --> ORCH
    ORCH --> TEAM
    ORCH --> GUARD
    TEAM -.->|prompts, masked| LLM
    ORCH -->|the DSL the model wrote| DSL
    READ --> BRIDGE
    BRIDGE --> DSL
    WRITE --> GOV
    AUTH --> GOV
    DSL --> CH
    GOV --> PG
    BRIDGE --> PG
    WRITE -->|start, signal, query| TMP
    TMP <--> WF
    WF --> ACT
    ACT --> ENG
    ACT --> PG
    ACT --> CH
    ACT -->|HTTP, idempotency key| COMMIT
    COMMIT --> PG
```

**What the picture is saying.** The browser only ever reaches `app.py`. The
model only ever reaches the cube through the compiler. The worker is the only
thing that writes recomputed plan lines, and it reaches the downstream
commitment service over HTTP rather than sharing a transaction with it — so a
failure there is a visible failure, not a silent rollback.

---

## 2. The read path

A question comes in; an answer with evidence goes out. Nothing changes.

```mermaid
flowchart LR
    Q["POST /api/v1/query"] --> SC["Build UserScope<br/>companies + countries<br/><i>from the token</i>"]
    SC --> MASK["mask_for_llm<br/><i>masking.py</i>"]
    MASK --> TEAM["Agent team<br/><i>writes DSL only</i>"]
    TEAM --> CGUARD{"Country<br/>authorised?"}
    CGUARD -->|no| REFUSE["OUT_OF_SCOPE<br/><i>nothing compiled or run</i>"]
    CGUARD -->|yes| COMP["compile_query<br/><i>scope filter injected here</i>"]
    COMP --> EXEC["ClickHouse"]
    EXEC --> ARITH["ArithmeticVerificationPostHook<br/><i>every number must be in a row</i>"]
    ARITH --> COV["coverage line<br/><i>assembled, not described</i>"]
    COV --> OUT["AgentFPAResponse<br/>answer + DSL + SQL + rows"]
    OUT --> HIST["ask_history.record"]
```

Three checks sit on this path, and none of them is the model's to make:

| Check | Where | What it stops |
|---|---|---|
| Entity scope | `compiler.py`, inside the SQL | A model widening its own access |
| Country authorisation | `planner.py`, before compiling | Answering `0.00` for a country you cannot see |
| Arithmetic | `hooks.py`, after execution | A number that is not in any returned row |

---

## 3. The write path

An assumption changes, the plan is recomputed, three systems move together.

```mermaid
sequenceDiagram
    participant P as Planner
    participant API as app.py
    participant T as Temporal
    participant W as Worker
    participant PG as Postgres
    participant CH as ClickHouse
    participant C as Commitment

    P->>API: ask in words
    API->>API: agent drafts a re-forecast (PROPOSED)
    P->>API: confirm the draft
    API->>T: start PlanRecomputeWorkflow
    T->>W: dispatch
    W->>CH: resolve the dirty set, partition it
    W->>CH: recompute, write staged rows
    W->>PG: write draft lines + traces
    W-->>P: park at AWAITING_SUBMISSION
    P->>API: submit
    API->>T: signal
    W->>PG: covenant check
    Note over W,PG: controller approves, CFO locks
    W->>CH: publish into fact_plan_line
    W->>C: commit (idempotency key)
    W->>PG: audit event
```

The worker can die at any point here. The state lives in Temporal's history,
not in the process, so a restart resumes from the last completed partition
rather than starting over.

---

## 4. Inside the agent team

The part most worth understanding, because it is where the trust boundary is.

```mermaid
flowchart TB
    subgraph PLAIN["Plain code — decides"]
        ORCH["FPAOrchestrator"]
        TOOLS["FPATools<br/><i>the only tools a model has</i>"]
        HOOKS["Hooks: masking, arithmetic, scope claims"]
    end

    subgraph MODEL["The model — proposes"]
        LEAD["FPATeam<br/><i>leader, no read tool</i>"]
        QA["QueryAgent<br/><i>plain lookups</i>"]
        VA["VarianceAgent<br/><i>bridge / plan vs actual</i>"]
        PA["PlanningAgent<br/><i>driver + re-forecast drafts</i>"]
    end

    ORCH --> LEAD
    LEAD --> QA
    LEAD --> VA
    LEAD --> PA
    QA --> TOOLS
    VA --> TOOLS
    PA --> TOOLS
    TOOLS --> ORCH
    HOOKS -.->|wrap every call| TOOLS
    ORCH -->|executes, the model never does| OUT["compiled SQL"]
```

The leader holds **no read tool**, so a question it cannot answer alone has to
be delegated — which is what puts a named member in the trace.
`propose_reforecast` is only registered when the caller is a human planner, so
for anyone else the tool does not exist and the model cannot attempt it.

---

## 5. Component reference

| Component | Module | Depends on | Owns |
|---|---|---|---|
| API | `app.py` | everything below | HTTP, auth dependencies, request shapes |
| Agent orchestrator | `agent_team/planner.py` | tools, hooks, compiler | What runs, what is refused, retries |
| Agent team | `agent_team/team.py` | model provider | Turning intent into DSL |
| Agent tools | `agent_team/tools.py` | compiler, schema | The only surface a model can call |
| Guardrails | `agent_team/{masking,hooks,security}.py` | — | PII, injection, arithmetic, scope claims |
| DSL compiler | `dsl/compiler.py` | schema, ClickHouse | **The security boundary.** Parameterised SQL, scope filter, vintage pinning, row budget |
| Bridge | `bridge_service.py` | compiler, `dsl/bridge.py`, Postgres | Variance decomposition, reports, citations |
| Governance | `governance.py` | Postgres | Identity, roles, plan state machine, audit chain |
| Countries | `countries.py` | — | Code ↔ name, and what a query asks for |
| Re-forecast client | `recompute/client.py` | Temporal | Start, signal, query, cancel |
| Workflow | `recompute/workflows.py` | activities | Order of events; no I/O, no clock, no randomness |
| Activities | `recompute/activities.py` | Postgres, ClickHouse, commitment | Every read and write, retried individually |
| Recompute engine | `recompute/engine.py` | — | The arithmetic, pure and testable |
| Commitment | `commitment/service.py` | its own Postgres schema | The downstream counterparty |

---

## 6. Where the boundaries are

Four lines in this system are worth naming, because crossing one is always a
deliberate act:

1. **Browser → API.** A bearer token, hashed and looked up. Scope comes from
   the token; a request body can narrow it and never widen it.
2. **Orchestrator → model.** Everything sent is masked first. Whatever comes
   back is a *proposal*, checked before it is used.
3. **Compiler → ClickHouse.** The only place SQL is produced. Literals are
   bound as parameters, the scope filter is added here, and the read is pinned
   to a ledger vintage.
4. **Worker → commitment service.** HTTP, with an idempotency key, to a
   separate process and schema. No shared transaction, so a downstream failure
   cannot be hidden by a rollback.
