"""FastAPI service: natural-language FP&A question -> Agno team -> DSL -> SQL -> ClickHouse -> cited answer.

Run:  uvicorn app:app --reload --port 8000   then open http://localhost:8000
LLM behind the Agno team (chosen per request):
      claude-code -> Claude Agent SDK using the local `claude` login (subscription, optional FPA_CLAUDE_CODE_MODEL)
      claude-api  -> ANTHROPIC_API_KEY (+ FPA_CLAUDE_MODEL);  gemini -> GOOGLE_API_KEY (+ FPA_MODEL_ID)
Env:  CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query as QueryParam
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from fpa_project.agent_team import (  # noqa: E402
    AgentFPAResponse, FPAOrchestrator, FPATools, PlanningRequest, UserScope,
    build_agno_team, clickhouse_executor,
)
from fpa_project.dsl import DSLValidationError, ParseError, SecurityContext, compile_query  # noqa: E402
from fpa_project.config import (  # noqa: E402
    claude_api_model, clickhouse as clickhouse_settings, gemini_model,
)
from fpa_project.log_config import configure as configure_logging  # noqa: E402
from fpa_project.governance import Principal, Refused, authenticate  # noqa: E402

app = FastAPI(title="FPA Query API", version="1.0.0")


def current_user(authorization: str | None = Header(default=None)) -> Principal:
    """Who is asking, from the bearer token and nothing else.

    Every governed endpoint takes this. The identity, the roles and the entity
    scope all come from the tables the token resolves to; nothing in a request
    body can name a different user or a wider scope.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="send Authorization: Bearer <token>", headers={"WWW-Authenticate": "Bearer"})
    try:
        return authenticate(authorization.split(" ", 1)[1].strip())
    except Refused as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc

def global_plan_user(request: Request, who: Principal = Depends(current_user)) -> Principal:
    """Global plans/drivers/audit require coverage of the entire model.

    Scoped analysts use compiler queries and scoped reports. Until plans have
    their own entity membership, treating a global write as an entity write
    would expose or change data outside the caller's scope.
    """
    from fpa_project.governance import require_global_scope
    try:
        require_global_scope(who)
    except Refused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if request.method not in {"GET", "HEAD"}:
        if not who.is_human or not who.has_role("planner", "controller", "cfo"):
            raise HTTPException(status_code=403, detail="a human planner, controller or CFO is required")
    return who


configure_logging()
log = logging.getLogger("fpa.api")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - started) * 1000)
    # Query details are attached by the handler; other routes log the basics only.
    detail = getattr(request.state, "log_detail", "")
    log.info(
        "%s %s -> %s %dms%s",
        request.method, request.url.path, response.status_code, duration_ms, f" {detail}" if detail else "",
    )
    return response

MANIFEST = ROOT / "data" / "out" / "cube_manifest.json"
ALL_COMPANIES = frozenset(c["company"] for c in json.loads(MANIFEST.read_text())["companies"]) if MANIFEST.exists() else frozenset()

PROVIDER_KEYS = {
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEYS", "GEMINI_API_KEYS", "LLM_API_KEYS"),
    "claude-api": ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"),
}

_client = None


def get_client():
    global _client
    if _client is None:
        import clickhouse_connect
        ch = clickhouse_settings()
        _client = clickhouse_connect.get_client(
            host=ch.host, port=ch.port, username=ch.user, password=ch.password,
        )
    return _client


def scoped_tools(scope, reforecast_desk=None):
    from fpa_project.dsl.parser import parse_query
    from fpa_project.dsl.compiler import vintage_lookup
    from fpa_project.reconciliation import reconcile
    execute = clickhouse_executor(get_client())
    cache = {}
    def drift_check(dsl):
        parsed = parse_query(dsl)
        if not parsed.as_of:
            return None
        if dsl not in cache:
            sql, params = vintage_lookup(None)
            latest = list(execute(sql, params))[0]["closed_at"].isoformat()
            cache[dsl] = reconcile(dsl, parsed.as_of, latest, SecurityContext(scope.allowed_companies, scope.max_estimated_rows), execute, scope.user_id)
        return cache[dsl]
    return FPATools(scope, executor=execute, drift_checker=drift_check, reforecast_desk=reforecast_desk)


def provider_configured(provider: str) -> bool:
    if provider == "claude-code":
        return shutil.which("claude") is not None
    return any(os.getenv(k) for k in PROVIDER_KEYS[provider])


def build_model(provider: str):
    if provider == "claude-code":
        from fpa_project.agent_team.claude_code_model import ClaudeCodeModel
        return ClaudeCodeModel()
    if provider == "claude-api":
        from agno.models.anthropic import Claude
        return Claude(id=claude_api_model())
    from agno.models.google import Gemini
    return Gemini(id=gemini_model())


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8_000)
    # Optional narrowing. It can only shrink the caller's scope, never widen it.
    companies: list[str] | None = None
    max_rows: int = Field(default=1_000_000, ge=1)
    provider: Literal["claude-code", "claude-api", "gemini"] = "claude-code"


class QueryResponse(BaseModel):
    agent_response: AgentFPAResponse
    provider: str
    mode: Literal["agno_team", "direct_dsl", "not_run"]
    sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    # This question's entry in the asker's history, when it could be kept
    ask_id: int | None = None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


@app.post("/api/v1/query", response_model=QueryResponse)
def query(req: QueryRequest, http_request: Request, who: Principal = Depends(current_user)) -> QueryResponse:
    """Answer a question, and keep it in the asker's history whatever the outcome."""
    from fpa_project import ask_history

    # Scope comes from the token, never from the model, the DSL text or the
    # request body. A body that names companies can only narrow it.
    companies = who.companies & frozenset(req.companies) if req.companies else who.companies
    response = _answer(req, http_request, who, companies)
    ask_id = ask_history.record(
        who.user_id, req.query.strip(), response.provider, response.mode,
        response.agent_response.execution_status, companies, response.model_dump(mode="json"),
    )
    return response.model_copy(update={"ask_id": ask_id})


def _answer(req: QueryRequest, http_request: Request, who: Principal, companies: frozenset[str]) -> QueryResponse:
    started = time.monotonic()
    log.info("query received | user=%s provider=%s companies=%s", who.user_id, req.provider, len(companies))
    scope = UserScope(user_id=who.user_id, allowed_companies=companies, max_estimated_rows=req.max_rows)
    text = req.query.strip()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def fail(message: str) -> QueryResponse:
        http_request.state.log_detail = f"| provider={req.provider} status=NOT_RUN error={message[:120]!r}"
        return QueryResponse(
            agent_response=AgentFPAResponse(user_query=text, execution_status="VALIDATION_ERROR", error_message=message),
            provider=req.provider, mode="not_run", duration_ms=elapsed(),
        )

    try:
        tools = scoped_tools(scope, _reforecast_desk(who))
    except Exception as exc:
        return fail(f"ClickHouse unavailable: {exc}")
    orchestrator = FPAOrchestrator(tools)
    request = PlanningRequest(request=text)

    try:
        if text.upper().startswith("SELECT"):
            # Already FinOpsExpr: skip the model, keep the same validation/execution gate.
            mode = "direct_dsl"
            result = orchestrator.finalize(request, {"dsl": text}, narrative="")
        elif not provider_configured(req.provider):
            hint = "install Claude Code and run `claude` to log in" if req.provider == "claude-code" else f"set {PROVIDER_KEYS[req.provider][0]}"
            return fail(f"{req.provider} is not configured: {hint}, or type FinOpsExpr starting with SELECT.")
        else:
            mode = "agno_team"
            # Built per request so every member's tools carry this caller's scope.
            team = build_agno_team(model=build_model(req.provider), toolset=tools)
            from fpa_project.agent_team.proposals import save_pause
            result = orchestrator.run_with_team(team, request,
                pause_handler=lambda output: save_pause(output, scope, req.provider, tools.schema))
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")

    result = result.model_copy(update={
        "cited_data_rows": [{k: _jsonable(v) for k, v in row.items()} for row in result.cited_data_rows],
    })
    if result.execution_status == "REFORECAST_PROPOSED" and result.reforecast_draft:
        try:
            result = result.model_copy(update={"reforecast_request_id": _store_reforecast(result, who, text)})
        except (Refused, ValueError) as exc:
            return fail(f"the re-forecast draft could not be stored: {exc}")
    response = QueryResponse(
        agent_response=result,
        provider=req.provider,
        mode=mode,
        columns=list(result.cited_data_rows[0]) if result.cited_data_rows else [],
    )
    if result.generated_dsl:
        try:
            compiled = compile_query(result.generated_dsl, security_context=SecurityContext(companies, req.max_rows))
            response.sql, response.params = compiled.sql, compiled.params
        except (ParseError, DSLValidationError, ValueError):
            pass
    response.duration_ms = elapsed()
    http_request.state.log_detail = (
        f"| provider={response.provider} mode={response.mode} status={result.execution_status} "
        f"rows={len(result.cited_data_rows)} dsl={result.generated_dsl!r}"
    )
    return response


def _reforecast_desk(who: Principal):
    """The re-forecast desk, for a human planner with the whole-model scope only.

    Anyone else's agent team is built without propose_reforecast at all. The
    database checks the same thing again when the draft is stored.
    """
    from fpa_project.governance import require_global_scope
    from fpa_project.reforecast_requests import ReforecastDesk

    if not who.is_human or not who.has_role("planner"):
        return None
    try:
        require_global_scope(who)
    except Refused:
        return None
    return ReforecastDesk(who)


def _store_reforecast(result: AgentFPAResponse, who: Principal, question: str) -> str:
    from fpa_project import reforecast_requests as requests

    draft = requests.Draft(**{k: v for k, v in result.reforecast_draft.items() if k in requests.Draft.__dataclass_fields__})
    evidence = {
        "dsl": result.generated_dsl,
        "rows": result.cited_data_rows[:50],
        "narrative": result.narrative_explanation,
        "produced_by": result.produced_by,
    }
    return requests.create(draft, question, evidence, who)


class ReconcileRequest(BaseModel):
    dsl: str = Field(min_length=1, max_length=8000)
    left_as_of: str
    right_as_of: str


@app.post("/api/v1/reconcile")
def reconcile_vintages(req: ReconcileRequest, who: Principal = Depends(current_user)):
    from fpa_project.reconciliation import reconcile
    try:
        return reconcile(req.dsl, req.left_as_of, req.right_as_of, SecurityContext(who.companies),
                         clickhouse_executor(get_client()), who.user_id)
    except (ValueError, DSLValidationError, ParseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/ask-history")
def ask_history_list(q: str = "", limit: int = 100, who: Principal = Depends(current_user)) -> list[dict[str, Any]]:
    """The caller's own questions, newest first. Nobody sees anyone else's."""
    from fpa_project import ask_history

    rows = ask_history.list_for(who.user_id, who.companies, q, max(1, min(limit, 500)))
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


@app.get("/api/v1/ask-history/{ask_id}")
def ask_history_item(ask_id: int, who: Principal = Depends(current_user)) -> dict[str, Any]:
    """One of the caller's questions and the answer they got; its figures are
    withheld if they were computed under companies the caller can no longer see."""
    from fpa_project import ask_history

    row = ask_history.load_for(who.user_id, ask_id, who.companies)
    if row is None:
        raise HTTPException(status_code=404, detail="no such question in your history")
    return {k: _jsonable(v) for k, v in row.items()}


@app.get("/api/v1/providers")
def providers() -> list[dict[str, Any]]:
    return [{"id": p, "configured": provider_configured(p)} for p in ("claude-code", "claude-api", "gemini")]


# --------------------------------------------------------------------------
# Who am I
# --------------------------------------------------------------------------
def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="send Authorization: Bearer <token>", headers={"WWW-Authenticate": "Bearer"})
    return authorization.split(" ", 1)[1].strip()


def session_user(authorization: str | None = Header(default=None)) -> Principal:
    """Like current_user, but a PENDING account is let through: it may ask who
    it is (to show "waiting for approval") and sign out, and nothing else."""
    try:
        return authenticate(_bearer(authorization), allow_pending=True)
    except Refused as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def superadmin_user(who: Principal = Depends(current_user)) -> Principal:
    """Account and access management. The database checks the same thing again
    on every write (migration 017); this makes the refusal a clear 403."""
    if not who.is_human or not who.has_role("superadmin"):
        raise HTTPException(status_code=403, detail="only a superadmin manages accounts and access")
    return who


@app.get("/api/v1/me")
def me(who: Principal = Depends(session_user)) -> dict[str, Any]:
    return {"user_id": who.user_id, "display_name": who.display_name, "email": who.email, "status": who.status,
            "roles": sorted(who.roles), "companies": sorted(who.companies), "is_human": who.is_human}


@app.get("/api/v1/people")
def people(who: Principal = Depends(current_user)) -> list[dict[str, str]]:
    """Id to display name, for showing who requested, approved or changed something."""
    from fpa_project import users

    return users.directory()


# --------------------------------------------------------------------------
# Accounts: signup, sign-in, sign-out, and the superadmin's access desk
# --------------------------------------------------------------------------
class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class AccessRequest(BaseModel):
    roles: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2_000)


class NoteRequest(BaseModel):
    note: str = Field(default="", max_length=2_000)


@app.post("/api/v1/auth/signup")
def signup(req: SignupRequest) -> dict[str, Any]:
    """Create an account. It starts PENDING, with no role and no company, and the
    token returned can only see that it is waiting for a superadmin."""
    from fpa_project import users

    try:
        return users.signup(req.email, req.display_name, req.password)
    except Refused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/auth/login")
def login(req: LoginRequest) -> dict[str, Any]:
    from fpa_project import users

    try:
        return users.login(req.email, req.password)
    except Refused as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/v1/auth/logout")
def logout(authorization: str | None = Header(default=None), who: Principal = Depends(session_user)) -> dict[str, str]:
    """Revoke this session. A seeded standing token has no session to revoke;
    the browser simply forgets it."""
    from fpa_project import users

    users.logout(_bearer(authorization))
    return {"status": "SIGNED_OUT"}


@app.get("/api/v1/admin/users")
def admin_users(who: Principal = Depends(superadmin_user)) -> list[dict[str, Any]]:
    from fpa_project import users

    return [{k: _jsonable(v) for k, v in row.items()} for row in users.list_users()]


@app.get("/api/v1/admin/catalog")
def admin_catalog(who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return users.catalog()


def _admin(call):
    try:
        return call()
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/admin/users/{user_id}/approve")
def admin_approve(user_id: str, req: AccessRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.approve(who.user_id, user_id, req.roles, req.companies, req.note))


@app.post("/api/v1/admin/users/{user_id}/reject")
def admin_reject(user_id: str, req: NoteRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.reject(who.user_id, user_id, req.note))


@app.put("/api/v1/admin/users/{user_id}/access")
def admin_access(user_id: str, req: AccessRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.set_access(who.user_id, user_id, req.roles, req.companies, req.note))


class RenameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


@app.put("/api/v1/admin/users/{user_id}/name")
def admin_rename(user_id: str, req: RenameRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.rename(who.user_id, user_id, req.display_name))


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@app.put("/api/v1/admin/users/{user_id}/email")
def admin_change_email(user_id: str, req: EmailRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.change_email(who.user_id, user_id, req.email))


@app.post("/api/v1/admin/users/{user_id}/disable")
def admin_disable(user_id: str, req: NoteRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.set_enabled(who.user_id, user_id, False, req.note))


@app.post("/api/v1/admin/users/{user_id}/enable")
def admin_enable(user_id: str, req: NoteRequest, who: Principal = Depends(superadmin_user)) -> dict[str, Any]:
    from fpa_project import users

    return _admin(lambda: users.set_enabled(who.user_id, user_id, True, req.note))


# --------------------------------------------------------------------------
# The variance bridge
# --------------------------------------------------------------------------
class BridgeRequest(BaseModel):
    dsl: str = Field(min_length=1, max_length=8_000, description="a FinOpsExpr query ending in COMPARE PLAN ... TO ACTUAL BRIDGE")
    companies: list[str] | None = None
    convention: Literal["volume_first", "price_first"] = "volume_first"
    materiality: float | None = Field(default=None, ge=0)
    persist: bool = True


class ReportStatusRequest(BaseModel):
    status: Literal["OPEN", "INVESTIGATING", "ESCALATED", "REVIEWED", "CLOSED"]


@app.post("/api/v1/bridge")
def bridge(req: BridgeRequest, who: Principal = Depends(current_user)) -> dict[str, Any]:
    """Run a bridge over the matched plan/actual set and persist the report.

    The DSL goes through the same compiler as every other query, with the
    caller's scope, so the bridge can only run over rows the caller may see.
    """
    from fpa_project.bridge_service import DEFAULT_MATERIALITY, BridgeError, run_bridge
    from fpa_project.dsl.bridge import Convention

    companies = who.companies & frozenset(req.companies) if req.companies else who.companies
    try:
        report = run_bridge(
            req.dsl, clickhouse_executor(get_client()), SecurityContext(companies),
            actor=who.user_id, convention=Convention(req.convention),
            materiality=Decimal(str(req.materiality)) if req.materiality is not None else DEFAULT_MATERIALITY,
            persist=req.persist,
        )
    except (ParseError, DSLValidationError, BridgeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return report.to_dict()


class VintageBridgeRequest(BaseModel):
    dsl: str = Field(min_length=1, max_length=8_000, description="a bridge query without AS OF")
    left_as_of: str = Field(description="the earlier close, e.g. 2026-07-05T18:00:00")
    right_as_of: str = Field(description="the later close, e.g. 2026-08-12T09:30:00")


@app.post("/api/v1/bridge/vintages")
def vintage_bridge(req: VintageBridgeRequest, who: Principal = Depends(current_user)) -> dict[str, Any]:
    """The same bridge at two closes: the change split into restated, reversed and new lines."""
    from fpa_project.bridge_service import BridgeError, vintage_bridge as run

    try:
        return run(req.dsl, req.left_as_of, req.right_as_of, clickhouse_executor(get_client()), SecurityContext(who.companies))
    except (ParseError, DSLValidationError, BridgeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/variance-reports/{report_id}")
def variance_report(report_id: str, who: Principal = Depends(current_user)) -> dict[str, Any]:
    from fpa_project.bridge_service import load_report

    _require_report_scope(report_id, who)
    report = load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no variance report {report_id}")
    return report


@app.get("/api/v1/variance-reports/{report_id}/citations")
def variance_report_citations(report_id: str, path: str = "", leg: str | None = None, limit: int = QueryParam(default=200, ge=1, le=200), offset: int = QueryParam(default=0, ge=0), full_rows: bool = False, who: Principal = Depends(current_user)) -> dict[str, Any]:
    """Drill-through: the cube rows behind a node, with the vintage they were read at.

    ``path`` is the node's rollup path joined with '|'; empty means the root.
    ``leg`` (price, volume, mix, fx, rate, efficiency) adds each row's
    contribution to that leg and orders the rows by it, largest first.
    """
    from fpa_project.bridge_service import BridgeError, citations_for

    _require_report_scope(report_id, who)
    node_path = [p for p in path.split("|") if p] if path else []
    try:
        rows = citations_for(report_id, node_path, limit=limit + 1, offset=offset, leg=leg)
    except BridgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    more = len(rows) > limit
    rows = rows[:limit]
    source_rows = []
    if full_rows and rows:
        if rows[0]["vintage_closed_at"] is None:
            raise HTTPException(status_code=422, detail="forecast-change citations have no actual-ledger vintage; use citation amounts")
        from fpa_project.dsl.compiler import compile_citation_rows
        compiled = compile_citation_rows(rows, rows[0]["vintage_closed_at"].isoformat(), SecurityContext(who.companies))
        source_rows = list(clickhouse_executor(get_client())(compiled.sql, compiled.params))
    return {"report_id": report_id, "path": node_path, "leg": leg, "rows": rows, "source_rows": source_rows,
            "next_offset": offset + limit if more else None}


@app.post("/api/v1/variance-reports/{report_id}/status")
def variance_report_status(report_id: str, req: ReportStatusRequest, who: Principal = Depends(current_user)) -> dict[str, Any]:
    """Move a report. An agent may investigate; the database lets only a human close."""
    from fpa_project.bridge_service import StatusRefused, set_status

    _require_report_scope(report_id, who)
    try:
        return {"report_id": report_id, **set_status(report_id, req.status, who.user_id)}
    except StatusRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_report_scope(report_id: str, who: Principal) -> None:
    from uuid import UUID
    from fpa_project.bridge_service import report_in_scope

    try:
        UUID(report_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid report ID") from exc
    if not report_in_scope(report_id, who.companies):
        raise HTTPException(status_code=404, detail="report not found in your entity scope")


# --------------------------------------------------------------------------
# Plan version governance: the state machine gate, distinct from the workflow's
# --------------------------------------------------------------------------
class CreatePlanVersionRequest(BaseModel):
    plan_version_code: str = Field(min_length=1, max_length=40)
    model_code: str = "FPA-2026"
    plan_year: int = Field(default=2026, ge=2000, le=2200)
    covenant_note: str = ""


class TransitionRequest(BaseModel):
    to_state: Literal["DRAFT", "IN_REVIEW", "APPROVED", "LOCKED", "SUPERSEDED", "REJECTED"]
    note: str = ""
    # The row_version the caller read. Two writers on one version: the
    # second is told it lost rather than silently overwriting.
    expected_version: int | None = None


class CovenantRequest(BaseModel):
    expected_version: int = Field(ge=1)
    covenant_ok: bool
    note: str = ""


class DriverRequest(BaseModel):
    expected_version: str | None = None
    model_code: str = "FPA-2026"
    driver_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    driver_name: str = Field(min_length=1)
    formula: str = Field(min_length=1, max_length=4_000)
    unit: str = "ratio"
    value_type: Literal["numeric", "percentage", "currency", "count"] = "numeric"


class PlanningModelRequest(BaseModel):
    formulas: dict[str, str]


class FxRateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    period_month: str = Field(pattern=r"^\d{4}-\d{2}-01$")
    from_currency: str = Field(min_length=3, max_length=3)
    rate: str


def _refusal(exc: Refused) -> HTTPException:
    message = str(exc)
    status = 404 if message.startswith("no plan version") or message.startswith("no planning model") else 409
    return HTTPException(status_code=status, detail=message)


@app.get("/api/v1/plan-versions")
def plan_versions(who: Principal = Depends(global_plan_user)) -> list[dict[str, Any]]:
    from fpa_project.governance import list_plan_versions

    return [{k: _jsonable(v) for k, v in row.items()} for row in list_plan_versions()]


@app.post("/api/v1/plan-versions")
def create_plan_version(req: CreatePlanVersionRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    from fpa_project.governance import create_plan_version as create

    try:
        return create(req.plan_version_code, req.model_code, req.plan_year, who.user_id, req.covenant_note)
    except Refused as exc:
        raise _refusal(exc) from exc


@app.get("/api/v1/plan-versions/{plan_version_code}")
def plan_version(plan_version_code: str, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    from fpa_project.governance import describe

    try:
        return {k: _jsonable(v) for k, v in describe(plan_version_code).items()}
    except Refused as exc:
        raise _refusal(exc) from exc


@app.post("/api/v1/plan-versions/{plan_version_code}/transition")
def transition_plan_version(plan_version_code: str, req: TransitionRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """Move a plan version through the governance state machine.

    A different gate from the workflow's approval signal, refusing for
    different reasons: this one is about roles, declared transitions and the
    database's own constraints; that one is about a run parked mid-flight.
    """
    from fpa_project.governance import transition

    try:
        return transition(plan_version_code, req.to_state, who.user_id, req.note, req.expected_version)
    except Refused as exc:
        raise _refusal(exc) from exc


@app.put("/api/v1/plan-versions/{plan_version_code}/covenant")
def set_covenant(plan_version_code: str, req: CovenantRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """A controller-only field, enforced by the database whoever calls this."""
    from fpa_project.governance import set_covenant as write

    try:
        return write(plan_version_code, req.covenant_ok, req.note, who.user_id, req.expected_version)
    except Refused as exc:
        raise _refusal(exc) from exc


@app.put("/api/v1/plan-versions/{plan_version_code}/fx-rates")
def set_fx_rate(plan_version_code: str, req: FxRateRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    from fpa_project.governance import set_plan_fx_rate

    try:
        return set_plan_fx_rate(plan_version_code, req.period_month, req.from_currency.upper(), req.rate, who.user_id, req.expected_version)
    except Refused as exc:
        raise _refusal(exc) from exc


@app.get("/api/v1/drivers")
def drivers(model_code: str = "FPA-2026", who: Principal = Depends(global_plan_user)) -> list[dict[str, Any]]:
    from fpa_project.governance import list_drivers

    return [{k: _jsonable(v) for k, v in row.items()} for row in list_drivers(model_code)]


@app.post("/api/v1/drivers")
def save_driver(req: DriverRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """Save a driver. Its formula is parsed, resolved and cycle-checked first."""
    from fpa_project.governance import save_driver as save

    try:
        return save(req.model_code, req.driver_code, req.driver_name, req.formula, who.user_id, req.unit, req.value_type, expected_version=req.expected_version)
    except Refused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/v1/planning-models/{model_code}")
def save_planning_model(model_code: str, req: PlanningModelRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """Save a whole driver library. The DAG is derived, and a cycle is refused."""
    from fpa_project.governance import save_planning_model as save

    try:
        return save(model_code, req.formulas, who.user_id)
    except Refused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/audit")
def audit(entity_type: str | None = None, entity_id: str | None = None, limit: int = 50, who: Principal = Depends(global_plan_user)) -> list[dict[str, Any]]:
    from fpa_project.governance import audit_trail

    return [{k: _jsonable(v) for k, v in row.items()} for row in audit_trail(entity_type, entity_id, min(limit, 500))]


@app.get("/api/v1/audit/verify")
def audit_verify(who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """Recompute the whole hash chain. Fails loudly on any altered row."""
    from dataclasses import asdict

    from fpa_project.governance import verify_audit_chain

    return asdict(verify_audit_chain())


# --------------------------------------------------------------------------
# Re-forecast: the durable recompute behind a driver shock
# --------------------------------------------------------------------------
class ShockRequest(BaseModel):
    plan_version_code: str = Field(default="PV-2026-0001")
    driver_code: str
    from_value: float = Field(gt=0, description="the driver's value before the change")
    to_value: float = Field(gt=0)
    scenario_codes: list[str] | None = None
    # Why the planner is making this move. Recorded in the audit log beside the
    # run, never in the workflow input: it is not part of what is computed, so
    # it must not change the idempotency key or the recorded histories.
    reason: str = Field(default="", max_length=2_000)


class DecisionRequest(BaseModel):
    approved: bool
    comment: str = ""


@app.post("/api/v1/reforecast")
async def start_reforecast(req: ShockRequest, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """Shock a driver and start the re-forecast.

    One run per plan version at a time. A second shock arriving while one is
    going does not start a competing workflow and is not dropped either: it
    goes to the running run's update handler, which folds it in or refuses it
    and says which. The requester is the token's user.
    """
    from fpa_project.recompute import client as recompute_client
    from fpa_project.recompute.models import DriverShock

    if not who.has_role("planner", "controller", "cfo"):
        raise HTTPException(status_code=403, detail=f"{who.user_id} may not start a re-forecast; that needs the planner, controller or cfo role")
    shock = DriverShock(driver_code=req.driver_code, from_value=req.from_value, to_value=req.to_value)
    try:
        started = await recompute_client.start(req.plan_version_code, [shock], who.user_id, req.scenario_codes)
    except Exception as exc:
        # A refused update arrives as WorkflowUpdateFailedError("Workflow update
        # failed"), with the validator's actual reason on .cause. The caller
        # needs the reason: a refusal nobody can read is barely better than
        # silently ignoring the shock.
        reason = getattr(getattr(exc, "cause", None), "message", None) or str(exc)
        log.warning("re-forecast start failed: %s", reason)
        raise HTTPException(status_code=409, detail=reason) from exc
    if req.reason.strip():
        _record_reason(req, who, started)
    return started


def _record_reason(req: ShockRequest, who: Principal, started: dict[str, Any]) -> None:
    """Keep the planner's rationale where the approver will see it.

    Written only once the run has accepted the shock, so a refused shock leaves
    no reason behind. A failure here does not undo a run that has started; it
    is logged, because the run itself is the thing that matters.
    """
    from fpa_project.governance import engine, record

    try:
        with engine().begin() as conn:
            record(conn, who.user_id, "reforecast", req.plan_version_code, "REASON", {
                "reason": req.reason.strip(),
                "shocks": [[req.driver_code, req.from_value, req.to_value]],
                "action": started.get("action"),
                "run_id": started.get("run_id"),
            })
    except Exception:  # noqa: BLE001 - the run has started; do not report it as failed
        log.exception("could not record the re-forecast reason for %s", req.plan_version_code)


GATE_NAMES = {"submit": "the planner's submission", "approve": "a controller's approval", "lock": "the CFO's lock"}


async def _signal_gate(plan_version_code: str, gate: str, approved: bool, who: Principal, comment: str) -> dict[str, str]:
    """Send a decision to one of the run's gates, but only while the run is parked there.

    Refusing early keeps a person from pre-deciding a gate on numbers they
    cannot see yet. Segregation of duties is not checked here: the workflow
    asks the database, which is the only place that can answer truthfully; a
    refused decision shows up under `refusals` in the progress query and the
    run stays parked.
    """
    from fpa_project.recompute import client as recompute_client

    _, phase = recompute_client.GATE_SIGNALS[gate]
    try:
        progress = await recompute_client.progress(plan_version_code)
        current = progress["phase"] if progress else None
    except Exception:  # noqa: BLE001 - no worker to answer the query; the run's own mirror still knows
        # A query needs a live worker; a signal does not. With the worker down
        # the run's mirror row says which gate it is parked at (the workflow
        # writes it on parking), so a decision is still accepted and waits in
        # the run's history for the worker to come back.
        current = _parked_gate_from_mirror(plan_version_code)
    if current is None:
        raise HTTPException(status_code=409, detail=f"no re-forecast of {plan_version_code} is running; it has finished or never started")
    if current != phase:
        raise HTTPException(status_code=409, detail=f"the run is at {current}, not waiting for {GATE_NAMES[gate]}")
    try:
        await recompute_client.decide(plan_version_code, approved, who.user_id, comment, gate=gate)
    except Exception as exc:
        raise _no_run(plan_version_code, exc) from exc
    return {"plan_version_code": plan_version_code, "gate": gate, "decision": "YES" if approved else "NO", "status": "SENT"}


def _parked_gate_from_mirror(plan_version_code: str) -> str | None:
    """The state of the open run on this plan, from recompute_run; None when no run is open."""
    from sqlalchemy import text as sql_text

    from fpa_project.governance import engine
    from fpa_project.recompute.client import workflow_id

    with engine().begin() as conn:
        return conn.execute(
            sql_text("SELECT state FROM fpa_governance.recompute_run WHERE workflow_id = :wf AND ended_at IS NULL "
                     "ORDER BY started_at DESC LIMIT 1"),
            {"wf": workflow_id(plan_version_code)},
        ).scalar()


class SubmitRequest(BaseModel):
    comment: str = Field(default="", max_length=2_000)


@app.post("/api/v1/reforecast/{plan_version_code}/submit")
async def submit_reforecast(plan_version_code: str, req: SubmitRequest, who: Principal = Depends(global_plan_user)) -> dict[str, str]:
    """The planner submits the recomputed draft for review: DRAFT -> IN_REVIEW."""
    return await _signal_gate(plan_version_code, "submit", True, who, req.comment)


@app.post("/api/v1/reforecast/{plan_version_code}/decision")
async def decide_reforecast(plan_version_code: str, req: DecisionRequest, who: Principal = Depends(global_plan_user)) -> dict[str, str]:
    """A controller approves (IN_REVIEW -> APPROVED) or rejects the submitted plan. Approving neither locks nor publishes."""
    return await _signal_gate(plan_version_code, "approve", req.approved, who, req.comment)


@app.post("/api/v1/reforecast/{plan_version_code}/lock")
async def lock_reforecast(plan_version_code: str, req: DecisionRequest, who: Principal = Depends(global_plan_user)) -> dict[str, str]:
    """The CFO locks the approved plan (APPROVED -> LOCKED), after which the system publishes it; or declines to."""
    return await _signal_gate(plan_version_code, "lock", req.approved, who, req.comment)


def _no_run(plan_version_code: str, exc: Exception) -> HTTPException:
    """A signal to a run that has finished, or never started, is a 409 that says so."""
    from temporalio.service import RPCError, RPCStatusCode

    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        return HTTPException(status_code=409, detail=f"no re-forecast of {plan_version_code} is running; it has finished or never started")
    raise exc


@app.post("/api/v1/reforecast/{plan_version_code}/cancel")
async def cancel_reforecast(plan_version_code: str, who: Principal = Depends(global_plan_user)) -> dict[str, str]:
    from fpa_project.recompute import client as recompute_client

    try:
        await recompute_client.cancel(plan_version_code)
    except Exception as exc:
        raise _no_run(plan_version_code, exc) from exc
    return {"plan_version_code": plan_version_code, "status": "CANCELLING"}


@app.get("/api/v1/reforecast/{plan_version_code}/progress")
async def reforecast_progress(plan_version_code: str, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """The live phase and counters, straight from the running workflow.

    A successor (PV-…-R2) has no run of its own: it is drafted by the run on
    the version it supersedes. Asked for a successor, this answers with that
    run, but only when the run's target really is this successor, and says
    which plan the run is keyed on so decisions and cancels go there.
    """
    from sqlalchemy import text as sql_text

    from fpa_project.governance import engine
    from fpa_project.recompute import client as recompute_client

    run_code = plan_version_code
    result = await recompute_client.progress(plan_version_code)
    if result is None:
        with engine().begin() as conn:
            predecessor = conn.execute(
                sql_text(
                    "SELECT p.plan_version_code FROM fpa_governance.plan_version v "
                    "JOIN fpa_governance.plan_version p ON p.plan_version_id = v.supersedes_plan_version_id "
                    "WHERE v.plan_version_code = :code"
                ),
                {"code": plan_version_code},
            ).scalar()
        if predecessor:
            candidate = await recompute_client.progress(predecessor)
            if candidate and candidate.get("target_version_code") == plan_version_code:
                result, run_code = candidate, predecessor
    if result is None:
        raise HTTPException(status_code=404, detail=f"no re-forecast running for {plan_version_code}")
    return {**result, "run_plan_version_code": run_code}


@app.get("/api/v1/plan-versions/{plan_version_code}/impact")
def plan_version_impact(
    plan_version_code: str, scenario: str = "base", who: Principal = Depends(global_plan_user),
) -> dict[str, Any]:
    """What a re-forecast successor does to the plan: shocks, reasons and the bridge.

    Read-only, and behind the same dependency as every other plan read, so it
    shows an approver nothing they could not already load.
    """
    from fpa_project.reforecast_impact import reforecast_impact

    try:
        return reforecast_impact(plan_version_code, scenario)
    except Refused as exc:
        raise _refusal(exc) from exc


@app.get("/api/v1/plan-versions/{plan_version_code}/lines")
def plan_version_lines(
    plan_version_code: str, limit: int = 200, offset: int = 0, who: Principal = Depends(global_plan_user),
) -> dict[str, Any]:
    """The version's governed lines with their derivation traces: for a re-forecast, the recomputed ones."""
    from fpa_project.governance import plan_lines

    try:
        result = plan_lines(plan_version_code, max(1, min(limit, 1_000)), max(0, offset))
    except Refused as exc:
        raise _refusal(exc) from exc
    return {
        **{k: _jsonable(v) for k, v in result.items() if k not in ("summary", "lines")},
        "summary": {k: _jsonable(v) for k, v in result["summary"].items()},
        "lines": [{k: _jsonable(v) for k, v in line.items()} for line in result["lines"]],
    }


# --------------------------------------------------------------------------
# Re-forecast requests: asked in words and confirmed by a planner
# --------------------------------------------------------------------------
@app.get("/api/v1/reforecast-requests")
def reforecast_requests(state: str | None = None, who: Principal = Depends(global_plan_user)) -> list[dict[str, Any]]:
    from fpa_project.reforecast_requests import list_requests

    return [{k: _jsonable(v) for k, v in row.items()} for row in list_requests(state)]


@app.get("/api/v1/reforecast-requests/{request_id}")
def reforecast_request(request_id: str, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    from fpa_project.reforecast_requests import load

    try:
        row = load(request_id)
    except Refused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row["covenant_checks"] = [{k: _jsonable(v) for k, v in c.items()} for c in row["covenant_checks"]]
    return {k: _jsonable(v) for k, v in row.items()}


class RequestConfirmation(BaseModel):
    confirmed: bool
    comment: str = Field(default="", max_length=2_000)


@app.post("/api/v1/reforecast-requests/{request_id}/confirm")
async def confirm_reforecast_request(request_id: str, req: RequestConfirmation, who: Principal = Depends(global_plan_user)) -> dict[str, Any]:
    """The planner who asked confirms the agent's draft (which starts the recompute) or withdraws it.

    Confirming approves nothing about the plan: the run recomputes a DRAFT the
    planner reviews and submits, then a controller approves and the CFO locks.
    The run is started while the confirmation's transaction is still open: if
    it cannot start, the request stays PROPOSED; it is committed only once the
    run exists, bound to it. The database refuses anyone but the human
    planner who asked.
    """
    from fpa_project import reforecast_requests as requests
    from fpa_project.recompute import client as recompute_client

    if not who.is_human or not who.has_role("planner"):
        raise HTTPException(status_code=403, detail="the human planner who asked confirms a re-forecast request")
    try:
        conn, tx, row = requests.begin_confirmation(request_id, who, req.confirmed, req.comment)
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a database verdict, shown as one
        raise HTTPException(status_code=409, detail=str(getattr(exc, "orig", exc)).splitlines()[0]) from exc
    started = None
    try:
        if req.confirmed:
            started = await recompute_client.start_request(
                row["plan_version_code"], requests.shock_of(row), row["requested_by"], request_id,
            )
            requests.bind_run(conn, request_id, started["run_id"])
        tx.commit()
    except Exception as exc:
        tx.rollback()
        if started:
            # The run exists but the confirmation did not land: stop it
            # rather than leave a run nobody confirmed.
            await recompute_client.cancel(row["plan_version_code"])
        reason = getattr(getattr(exc, "cause", None), "message", None) or str(getattr(exc, "orig", exc)).splitlines()[0]
        raise HTTPException(status_code=409, detail=reason) from exc
    finally:
        conn.close()
    loaded = requests.load(request_id)
    loaded["covenant_checks"] = [{k: _jsonable(v) for k, v in c.items()} for c in loaded["covenant_checks"]]
    return {**{k: _jsonable(v) for k, v in loaded.items()}, "run": started}


@app.get("/api/v1/reforecast/{plan_version_code}/runs")
def reforecast_runs(plan_version_code: str, who: Principal = Depends(global_plan_user)) -> list[dict[str, Any]]:
    """Past and present runs, from the durable record."""
    from sqlalchemy import text as sql_text

    from fpa_project.governance import engine

    with engine().begin() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT r.run_id, r.state, r.phase, r.dirty_rows, r.processed_rows, r.requested_by, r.decided_by, "
                "       r.detail, r.shocks, r.started_at, r.ended_at "
                "FROM fpa_governance.recompute_run r JOIN fpa_governance.plan_version v USING (plan_version_id) "
                "WHERE v.plan_version_code = :code ORDER BY r.started_at DESC LIMIT 50"
            ),
            {"code": plan_version_code},
        ).mappings().all()
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


@app.get("/api/v1/agent-proposals/{proposal_id}")
def agent_proposal(proposal_id: str, who: Principal = Depends(global_plan_user)):
    from fpa_project.agent_team.proposals import load
    try:
        row = load(proposal_id)
    except Refused as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {key: row[key] for key in ("proposal_id", "requested_by", "decided_by", "state", "drafts", "created_at", "decided_at")}


@app.post("/api/v1/agent-proposals/{proposal_id}/decision")
def decide_agent_proposal(proposal_id: str, req: DecisionRequest, who: Principal = Depends(global_plan_user)):
    from fpa_project.agent_team.proposals import decide, load, resume_snapshot
    if not who.is_human or not who.has_role("controller", "cfo"):
        raise HTTPException(status_code=403, detail="human controller approval required")
    try:
        decide(proposal_id, who.user_id, req.approved)
        row = load(proposal_id)
        original_scope = UserScope.model_validate(row["scope"])
        # Confirm current scope has not been revoked since the proposal.
        from fpa_project.governance import engine
        from sqlalchemy import text as sql
        with engine().connect() as conn:
            active = conn.execute(sql("SELECT active FROM fpa_governance.app_user WHERE user_id=:u"), {"u": original_scope.user_id}).scalar()
            current = frozenset(conn.execute(sql("SELECT company_code FROM fpa_governance.user_company_scope WHERE user_id=:u"), {"u": original_scope.user_id}).scalars())
        if not active or not (original_scope.allowed_companies or frozenset()) <= current:
            raise Refused("requester's original scope has been revoked; continuation refused")
        tools = scoped_tools(original_scope)
        team = build_agno_team(model=build_model(row["provider"]), toolset=tools)
        resume_snapshot(row, team)
        return {"proposal_id": proposal_id, "state": row["state"], "drafts": row["drafts"], "continued": True}
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
