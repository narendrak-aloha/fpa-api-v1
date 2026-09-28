"""Deterministic orchestration around the optional Agno model team."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from fpa_project.dsl.parser import parse_query

from .masking import mask_for_llm, mask_request_text
from .hooks import ArithmeticVerificationPostHook, MaskingGateHook, ScopeClaimGuard
from .logging_utils import ExternalAuditLogger, configure_terminal_logging, log_event
from .models import AgentFPAResponse, AgentPlan, ModelChangeProposal, PlanningRequest, PlanningResponse, ValidationIssue
from .registry import PlanningRegistry
from .tools import FPATools
from .api_keys import APIKeyRotator
import uuid


def _dsl_of(content: object) -> str:
    if isinstance(content, AgentPlan):
        return content.dsl
    if isinstance(content, dict):
        return str(content.get("dsl") or "")
    return str(getattr(content, "dsl", "") or "")


def _same_dsl(left: str, right: str) -> bool:
    return bool(left) and " ".join(left.split()) == " ".join(right.split())


def member_trace(run_output: object, dsl: str) -> dict[str, Any]:
    """Who in the team did what, and which of them produced the executed DSL.

    Read from Agno's own run record rather than from the model's say-so: a
    team run lists each member's response under ``member_responses`` with its
    stable ``agent_id``, the tools it called and what it returned. The
    producer is the last member whose plan or ``run_finops_query`` call
    carried exactly this DSL; if none did, the leader wrote it itself.
    """
    def entry(run: object) -> dict[str, Any]:
        tools = list(getattr(run, "tools", None) or [])
        return {
            "id": getattr(run, "agent_id", None) or getattr(run, "team_id", None),
            "name": getattr(run, "agent_name", None) or getattr(run, "team_name", None),
            "tools": [getattr(t, "tool_name", None) for t in tools],
            "_dsls": [_dsl_of(getattr(run, "content", None))]
            + [str((getattr(t, "tool_args", None) or {}).get("dsl", "")) for t in tools
               if getattr(t, "tool_name", None) == "run_finops_query"],
        }

    leader = entry(run_output)
    members = [entry(run) for run in getattr(run_output, "member_responses", None) or []]
    producer = next((m for m in reversed(members) if any(_same_dsl(d, dsl) for d in m["_dsls"])), leader)
    strip = lambda e: {k: v for k, v in e.items() if k != "_dsls"}  # noqa: E731
    return {
        "leader": strip(leader),
        "members": [strip(m) for m in members],
        "produced_by": {"id": producer["id"], "name": producer["name"]},
    }


def _check_error(run_output: object) -> tuple[str, str] | None:
    """The guardrail verdict Agno recorded on a run, if any: (type, message).

    Agno puts a check failure's message in ``content`` only when the run had
    no content yet, so an output check that rejected a model's answer looks
    like an ordinary answer there. The run's error event carries the type
    (``input_check_error`` / ``output_check_error``) and the real message,
    and is the only reliable way to tell a refusal from a repairable answer.
    """
    for event in getattr(run_output, "events", None) or []:
        kind = getattr(event, "error_type", None)
        if kind in ("input_check_error", "output_check_error"):
            return kind, str(getattr(event, "content", "") or getattr(event, "error", "") or "")
    return None


def validate_dsl(dsl: str, registry: PlanningRegistry | None = None) -> list[ValidationIssue]:
    """Validate syntax and planning registry semantics without compiling SQL."""
    # Syntax is checked first; registry checks only run against a typed AST.
    registry = registry or PlanningRegistry()
    try:
        query = parse_query(dsl)
    except Exception as exc:
        return [ValidationIssue(code="INVALID_DSL", message=str(exc))]
    return [ValidationIssue(code=i.code, message=i.message, field=i.field) for i in registry.validate(query)]


class FinOpsPlanner:
    """Safe boundary for an Agno team's proposed plan.

    ``generate`` accepts a model-produced ``AgentPlan`` (or a dict matching
    it), validates it, and never invokes the compiler or a database.
    """

    def __init__(self, registry: PlanningRegistry | None = None):
        self.registry = registry or PlanningRegistry()

    def prepare(self, request: PlanningRequest) -> PlanningRequest:
        # Mask both free-form text and structured context before any optional
        # model adapter can see the request.
        return PlanningRequest(request=mask_request_text(request.request), context=mask_for_llm(request.context))

    def generate(self, request: PlanningRequest, candidate: AgentPlan | dict) -> PlanningResponse:
        masked = self.prepare(request).context
        try:
            plan = candidate if isinstance(candidate, AgentPlan) else AgentPlan.model_validate(candidate)
        except ValidationError as exc:
            return PlanningResponse(status="ERROR", errors=[ValidationIssue(code="INVALID_OUTPUT", message=str(exc))], masked_context=masked)
        if plan.out_of_scope or plan.proposed_driver or (plan.proposed_reforecast and not plan.dsl.strip()):
            return PlanningResponse(status="VALID", plan=plan, masked_context=masked)
        # A typed model response is still untrusted until its DSL is validated.
        errors = validate_dsl(plan.dsl, self.registry)
        return PlanningResponse(status="VALID" if not errors else "ERROR", plan=plan if not errors else None, errors=errors, masked_context=masked)

    @staticmethod
    def propose_model_change(reason: str, *, metrics: list[str] | None = None, dimensions: list[str] | None = None) -> ModelChangeProposal:
        """Create a draft for a human reviewer; deliberately cannot mutate registry data."""
        return ModelChangeProposal(
            reason=reason,
            proposed_metrics=metrics or [],
            proposed_dimensions=dimensions or [],
        )


class FPAOrchestrator:
    """Final deterministic gate from an agent plan to an executed response."""

    MAX_SYNTAX_RETRIES = 2

    def __init__(self, tools: FPATools, *, masking_hook: MaskingGateHook | None = None, audit_logger: ExternalAuditLogger | None = None, api_key_rotator: APIKeyRotator | None = None):
        self.tools = tools
        self.planner = FinOpsPlanner(PlanningRegistry(tools.schema))
        self.masking_hook = masking_hook or tools.masking_hook
        self.arithmetic_hook = ArithmeticVerificationPostHook()
        self.scope_hook = ScopeClaimGuard()
        self.logger = configure_terminal_logging()
        self.audit_logger = audit_logger or tools.audit_logger
        self.api_key_rotator = api_key_rotator or APIKeyRotator.from_env()

    def finalize(self, request: PlanningRequest, candidate: AgentPlan | dict, narrative: str | None = None) -> AgentFPAResponse:
        run_id = uuid.uuid4().hex[:12]
        log_event(self.logger, "request_received", run_id=run_id)
        prepared = self.planner.prepare(request)
        log_event(self.logger, "context_masked", run_id=run_id)
        # Validate the candidate before allowing it to reach the scoped tool.
        result = self.planner.generate(request, candidate)
        dsl = result.plan.dsl if result.plan else ""
        assumptions = result.plan.assumptions if result.plan else []
        if result.status != "VALID":
            log_event(self.logger, "dsl_validation_failed", run_id=run_id, status="VALIDATION_ERROR", code=result.errors[0].code if result.errors else "INVALID_OUTPUT")
            message = "; ".join(issue.message for issue in result.errors)
            return AgentFPAResponse(user_query=request.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", narrative_explanation=narrative, error_message=message)
        if result.plan.proposed_driver:
            return AgentFPAResponse(user_query=prepared.request, execution_status="DRAFT",
                                    narrative_explanation="Driver proposal remains a draft; no active driver was changed.")
        if result.plan.proposed_reforecast:
            return self._finalize_reforecast(request, prepared, result.plan, narrative, run_id)
        if result.plan.out_of_scope:
            # Nothing is compiled or executed, so the refusal cannot cite numbers either.
            ok, _ = self.arithmetic_hook.verify(result.plan.explanation, [], dsl)
            explanation = result.plan.explanation if ok else "This question cannot be answered from the FP&A cube."
            log_event(self.logger, "request_out_of_scope", run_id=run_id, status="OUT_OF_SCOPE")
            return AgentFPAResponse(user_query=prepared.request, execution_status="OUT_OF_SCOPE", narrative_explanation=explanation, assumptions=assumptions)
        # FPATools injects authenticated scope, compiles parameterized SQL and
        # masks result rows before they are returned to this orchestrator.
        query_result = self.tools.run_finops_query(dsl)
        log_event(self.logger, "scoped_query_completed", run_id=run_id, status=query_result.status, row_count=query_result.row_count)
        if query_result.status == "REJECTED_SCOPE":
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="REJECTED_SCOPE", narrative_explanation=narrative, assumptions=assumptions, error_message=query_result.errors[0].message)
        if query_result.status != "SUCCESS":
            message = "; ".join(error.message for error in query_result.errors)
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", narrative_explanation=narrative, assumptions=assumptions, error_message=message)
        ok, error = self.arithmetic_hook.verify(narrative, query_result.rows, dsl)
        if ok:
            ok, error = self.scope_hook.verify(narrative, dsl)
        if not ok:
            log_event(self.logger, "arithmetic_verification_failed", run_id=run_id, status="VALIDATION_ERROR")
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", assumptions=assumptions, error_message=error)
        log_event(self.logger, "arithmetic_verification_completed", run_id=run_id, status="SUCCESS")
        # Stated by the orchestrator, not left to the model: every answer says
        # whose data it is, so a narrower result can never pass as the whole.
        assumptions = [*assumptions, self.coverage(dsl)]
        log_event(self.logger, "response_completed", run_id=run_id, status="SUCCESS", row_count=query_result.row_count)
        return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="SUCCESS", narrative_explanation=narrative, assumptions=assumptions, cited_data_rows=query_result.rows, drift_flags=self.tools.drift_flags)

    def coverage(self, dsl: str) -> str:
        """What the figures cover, assembled rather than described.

        The model is told not to characterise coverage because it cannot see
        the entity scope and does not know which filters the compiler applied.
        Both are known here: the dimension filters come from the DSL that was
        executed, the entity list from the authenticated scope. Left to the
        model, a country-filtered figure gets called the caller's whole scope,
        which is a different and much larger number.
        """
        companies = sorted(self.tools.scope.allowed_companies or [])
        within = f"within your entity scope of {len(companies)} companies ({', '.join(companies)})"
        if not dsl:
            return f"No figures were read; your entity scope is {len(companies)} companies ({', '.join(companies)})."
        filters = [f"{c.field} {c.operator.lower()} "
                   + ", ".join(str(v.value) for v in c.values)
                   for c in parse_query(dsl).predicates
                   if c.field in self.tools.schema.dimensions]
        if not filters:
            return f"Figures cover every entity {within}."
        return f"Figures cover only where {'; '.join(filters)}, {within}."

    def _finalize_reforecast(self, request: PlanningRequest, prepared: PlanningRequest, plan: AgentPlan,
                             narrative: str | None, run_id: str) -> AgentFPAResponse:
        """A re-forecast draft: the validated draft plus its read-path evidence.

        The draft comes from the tool call, never from the model's prose, so
        what the planner confirms is exactly what the desk
        validated. The evidence query runs through the same scoped compiler as
        any answer, and the narrative may only cite its rows and the draft's
        own two values.
        """
        drafts = [d for d in self.tools.reforecast_drafts if d.status == "DRAFT"]
        if not drafts:
            reasons = "; ".join(e.message for d in self.tools.reforecast_drafts for e in d.errors) or "propose_reforecast was not called"
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=plan.dsl, execution_status="VALIDATION_ERROR",
                                    error_message=f"the re-forecast could not be drafted: {reasons}")
        draft = drafts[-1]
        dsl = plan.dsl.strip()
        rows: list[dict] = []
        if dsl:
            query_result = self.tools.run_finops_query(dsl)
            if query_result.status != "SUCCESS":
                message = "; ".join(error.message for error in query_result.errors)
                return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR",
                                        narrative_explanation=narrative, error_message=message)
            rows = query_result.rows
        context = " ".join([dsl, draft.plan_version_code, *draft.companies, *draft.months])
        # The size of the change is the natural way to describe one ("down by
        # 5%"), and it is arithmetic on two figures the check already accepts,
        # so it is derived here rather than left to look invented. Both forms
        # are needed: "5%" is read as 0.05 and "5 percent" as 5. The keys stay
        # clear of _LEGS, where "change" and "delta" already mean the gap leg.
        evidence = {"from_value": draft.from_value, "to_value": draft.to_value,
                    "driver_change": round(draft.to_value - draft.from_value, 10)}
        if draft.from_value:
            ratio = round(draft.to_value / draft.from_value - 1, 10)
            evidence["driver_change_ratio"] = ratio
            evidence["driver_change_percent"] = round(ratio * 100, 10)
        # The reading the desk did not take is shown to the planner in the
        # assumptions, so the model quoting it back is citing the draft.
        if draft.alternative_value is not None:
            evidence["driver_alternative_value"] = draft.alternative_value
        ok, error = self.arithmetic_hook.verify(narrative, [*rows, evidence], context)
        if ok:
            ok, error = self.scope_hook.verify(narrative, dsl)
        if not ok:
            log_event(self.logger, "arithmetic_verification_failed", run_id=run_id, status="VALIDATION_ERROR")
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", error_message=error)
        assumptions = [*plan.assumptions,
                       "Nothing has changed yet: you confirm the draft, the recomputed lines are submitted and checked "
                       "against the covenants, then a controller approves and the CFO locks the result."]
        if draft.change_note:
            assumptions.append(draft.change_note)
        # The evidence query is scoped exactly like any answer, so it says whose
        # data it is here too rather than only on the read path.
        assumptions.append(self.coverage(dsl))
        log_event(self.logger, "reforecast_drafted", run_id=run_id, status="REFORECAST_PROPOSED")
        return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="REFORECAST_PROPOSED",
                                narrative_explanation=narrative, assumptions=assumptions, cited_data_rows=rows,
                                drift_flags=self.tools.drift_flags, reforecast_draft=draft.model_dump())

    def finalize_with_retries(self, request: PlanningRequest, candidates: list[AgentPlan | dict], narrative: str | None = None) -> AgentFPAResponse:
        """Try an initial candidate and one repair, retrying only validation failures."""
        last: AgentFPAResponse | None = None
        for candidate in candidates[:self.MAX_SYNTAX_RETRIES]:
            last = self.finalize(request, candidate, narrative)
            if last.execution_status == "SUCCESS" or last.execution_status == "REJECTED_SCOPE":
                return last
        return last or AgentFPAResponse(
            user_query=request.request,
            execution_status="VALIDATION_ERROR",
            error_message="no candidate DSL plan supplied",
        )

    def run_with_team(self, team: object, request: PlanningRequest, pause_handler=None) -> AgentFPAResponse:
        """Run an Agno Team with a hard two-attempt NL-to-DSL cap."""
        prepared = self.planner.prepare(request)
        run_id = uuid.uuid4().hex[:12]
        self.masking_hook.before_model(
            {"request": prepared.request, "context": prepared.context},
            user_id=self.tools.scope.user_id,
        )
        members = getattr(team, "members", [])
        last_result: AgentFPAResponse | None = None
        correction = prepared.request
        # Retry only bounded model/validation failures; execution and scope
        # outcomes are terminal and are not hidden by another model attempt.
        for attempt in range(1, self.MAX_SYNTAX_RETRIES + 1):
            log_event(self.logger, "team_attempt_started", run_id=run_id, attempt=attempt, max_attempts=self.MAX_SYNTAX_RETRIES)
            if attempt == 1 or not members:
                prompt = correction
                caller = team.run
            else:
                prompt = (
                    f"Return a corrected AgentPlan for this request: {prepared.request}. "
                    f"Previous attempt failed: {correction}. "
                    "Use plain FinOpsExpr text beginning with SELECT; do not use square brackets, JSON, Markdown, SQL, or commentary in dsl."
                )
                caller = members[0].run
            self.audit_logger.record("agno", "request", {"request": prompt, "attempt": attempt}, run_id=run_id)
            try:
                self.api_key_rotator.apply_to_team(team)
                raw = caller(prompt, user_id=self.tools.scope.user_id, dependencies={"scope": self.tools.scope.model_dump()})
                if getattr(raw, "is_paused", False):
                    if pause_handler is None:
                        raise ValueError("durable approval storage is required")
                    proposal_id = pause_handler(raw)
                    return AgentFPAResponse(user_query=prepared.request, execution_status="AWAITING_APPROVAL",
                                            proposal_id=proposal_id, narrative_explanation="Draft saved; awaiting a second human's decision.")
                content = getattr(raw, "content", raw)
                check = _check_error(raw)
                if check and check[0] == "input_check_error":
                    # An input guardrail refused the request. The same input
                    # would be refused again, so this is terminal and says why.
                    log_event(self.logger, "team_attempt_refused", run_id=run_id, attempt=attempt, status="REFUSED")
                    return AgentFPAResponse(user_query=prepared.request, execution_status="REFUSED",
                                            error_message=f"refused: {check[1]}")
                if check and check[0] == "output_check_error":
                    # An output check (the arithmetic post-hook) rejected the
                    # answer. Repairable: the next turn is told exactly why.
                    # Agno leaves the model's output in content here, so the
                    # reason comes from the error event, not from content.
                    log_event(self.logger, "team_attempt_failed", run_id=run_id, attempt=attempt, status="OUTPUT_CHECK")
                    correction = f"{check[1]}. Use only figures that appear in the query results."
                    last_result = AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message=check[1])
                    continue
                self.audit_logger.record("agno", "response", content, run_id=run_id)
                log_event(self.logger, "team_attempt_completed", run_id=run_id, attempt=attempt, status="RECEIVED")
            except Exception as exc:
                log_event(self.logger, "team_attempt_failed", run_id=run_id, attempt=attempt, status="ERROR", error_type=type(exc).__name__)
                correction = f"provider error: {type(exc).__name__}"
                last_result = AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message="model attempt failed")
                continue
            if isinstance(content, AgentFPAResponse):
                candidate = {"dsl": content.generated_dsl, "explanation": content.narrative_explanation or ""}
                narrative = content.narrative_explanation
            elif isinstance(content, AgentPlan):
                candidate, narrative = content, content.explanation
            elif isinstance(content, dict) and (content.get("dsl") or content.get("out_of_scope") or content.get("proposed_reforecast")):
                candidate, narrative = content, content.get("explanation") or content.get("narrative_explanation")
            else:
                correction = "response did not contain a non-empty AgentPlan.dsl"
                last_result = AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message=correction)
                continue
            result = self.finalize(request, candidate, narrative)
            trace = member_trace(raw, result.generated_dsl or _dsl_of(candidate))
            self.audit_logger.record("agno", "member_trace", trace, run_id=run_id)
            result = result.model_copy(update={"produced_by": trace["produced_by"]["id"], "member_trace": trace})
            if result.execution_status in {"SUCCESS", "REJECTED_SCOPE", "OUT_OF_SCOPE", "DRAFT", "REFORECAST_PROPOSED"}:
                return result
            last_result = result
            correction = result.error_message or "DSL validation failed"
        log_event(self.logger, "team_attempts_exhausted", run_id=run_id, status="VALIDATION_ERROR", max_attempts=self.MAX_SYNTAX_RETRIES)
        return last_result or AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message="initial model attempt and one repair exhausted")
