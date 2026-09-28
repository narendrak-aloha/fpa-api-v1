"""Safe natural-language to FinOpsExpr planning team.

This package is deliberately separate from :mod:`fpa_project.dsl.compiler`.
The team produces and validates FinOpsExpr only; SQL compilation and database
execution remain outside the agent boundary.
"""

from .models import AgentFPAResponse, AgentPlan, DriverProposal, ModelChangeProposal, PlanningRequest, PlanningResponse, QueryToolResult, UserScope, ValidationIssue
from .planner import FPAOrchestrator, FinOpsPlanner, validate_dsl
from .masking import mask_for_llm, mask_request_text
from .team import build_agno_team
from .tools import FPATools, clickhouse_executor
from .logging_utils import ExternalAuditLogger
from .api_keys import APIKeyRotator

__all__ = [
    "AgentFPAResponse", "AgentPlan", "DriverProposal", "ModelChangeProposal", "PlanningRequest", "PlanningResponse", "QueryToolResult", "UserScope", "ValidationIssue",
    "ExternalAuditLogger", "FPAOrchestrator", "FPATools", "clickhouse_executor", "FinOpsPlanner", "validate_dsl", "mask_for_llm", "mask_request_text", "build_agno_team", "APIKeyRotator",
]
