"""Strict Pydantic contracts exchanged by the planning team."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field: str | None = None


class PlanningRequest(BaseModel):
    """Untrusted caller input.  ``context`` is masked before model invocation."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=8_000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentPlan(BaseModel):
    """The only successful payload an LLM is allowed to return."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dsl: str = Field(default="", max_length=8_000)
    explanation: str = Field(default="", max_length=2_000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    out_of_scope: bool = False
    proposed_driver: bool = False
    # A re-forecast drafted with propose_reforecast. The dsl, if any, is the
    # read-path evidence for the slice being changed.
    proposed_reforecast: bool = False

    @model_validator(mode="after")
    def _dsl_matches_scope(self) -> "AgentPlan":
        # A refusal must not smuggle a query in; an answer must carry one.
        if self.out_of_scope and self.dsl.strip():
            raise ValueError("out_of_scope plans must not contain dsl")
        if self.proposed_driver and (self.dsl.strip() or self.out_of_scope):
            raise ValueError("driver proposals must not contain queries or scope refusals")
        if self.proposed_reforecast and (self.out_of_scope or self.proposed_driver):
            raise ValueError("a re-forecast draft is neither a scope refusal nor a driver formula proposal")
        if not self.out_of_scope and not self.proposed_driver and not self.proposed_reforecast and not self.dsl.strip():
            raise ValueError("dsl is required unless out_of_scope is true")
        return self


class PlanningResponse(BaseModel):
    """Fail-closed result.  A response is either valid DSL or structured errors."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["VALID", "ERROR", "DRAFT"]
    plan: AgentPlan | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)
    masked_context: dict[str, Any] = Field(default_factory=dict)


class UserScope(BaseModel):
    """Authenticated scope; never populated from model-generated DSL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    allowed_companies: frozenset[str] | None = None
    max_estimated_rows: int = Field(default=1_000_000, ge=1)


class AgentFPAResponse(BaseModel):
    """Public team response required by the Agno integration contract."""

    model_config = ConfigDict(extra="forbid")

    user_query: str
    generated_dsl: str = ""
    execution_status: Literal["SUCCESS", "VALIDATION_ERROR", "REJECTED_SCOPE", "OUT_OF_SCOPE", "AWAITING_APPROVAL", "DRAFT", "REFUSED", "REFORECAST_PROPOSED"]
    drift_flags: list[dict[str, Any]] = Field(default_factory=list)
    proposal_id: str | None = None
    narrative_explanation: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    cited_data_rows: list[dict[str, Any]] = Field(default_factory=list)
    error_message: str | None = None
    # The team member (stable Agno id) whose output carried the executed DSL,
    # and the leader/member delegation it came out of. Set on team runs only.
    produced_by: str | None = None
    member_trace: dict[str, Any] | None = None
    # A re-forecast the planner asked for: the validated draft, and the id of
    # the request the API stored it as for a controller to decide.
    reforecast_draft: dict[str, Any] | None = None
    reforecast_request_id: str | None = None


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class QueryToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["SUCCESS", "VALIDATION_ERROR", "REJECTED_SCOPE", "EXECUTION_ERROR"]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    drift_flags: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    errors: list[ToolError] = Field(default_factory=list)
    # The companies the rows are limited to, from the caller's token. Without
    # it a model asked about Germany sees Poland's zero-filled rows and
    # reports Germany as zero. Found live.
    scope: list[str] = Field(default_factory=list)


class DriverProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["DRAFT"] = "DRAFT"
    name: str = Field(min_length=1, max_length=100)
    expr_dsl: str = Field(min_length=1, max_length=4_000)
    references: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)


class ModelChangeProposal(BaseModel):
    """HITL-only governance artifact; this package has no apply operation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["DRAFT"] = "DRAFT"
    reason: str = Field(min_length=1, max_length=2_000)
    proposed_metrics: list[str] = Field(default_factory=list, max_length=50)
    proposed_dimensions: list[str] = Field(default_factory=list, max_length=50)


class ReforecastProposal(BaseModel):
    """What propose_reforecast returns: a validated draft, or why it is not one."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["DRAFT", "INVALID"] = "DRAFT"
    plan_version_code: str = ""
    driver_code: str = ""
    driver_name: str = ""
    from_value: float | None = None
    to_value: float | None = None
    companies: list[str] = Field(default_factory=list)
    months: list[str] = Field(default_factory=list)
    scope_label: str = ""
    # Which reading of "by N%" the desk applied, and that reading spelled out
    # against the one it did not take, so the planner confirming sees it.
    change_basis: str = "absolute"
    change_note: str = ""
    alternative_value: float | None = None
    errors: list[ToolError] = Field(default_factory=list)
