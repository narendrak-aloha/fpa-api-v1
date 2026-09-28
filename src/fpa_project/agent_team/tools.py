"""Safe Agno-facing tools backed by the existing deterministic compiler."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fpa_project.dsl.compiler import SecurityContext, compile_query
from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import parse_formula, referenced_names, validate_formula
from fpa_project.dsl.parser import parse_query
from fpa_project.dsl.schema import Schema

from .hooks import MaskingGateHook
from .logging_utils import ExternalAuditLogger, configure_terminal_logging, log_event
from .masking import mask_for_llm
from .models import DriverProposal, QueryToolResult, ReforecastProposal, ToolError, UserScope


def clickhouse_executor(client: Any) -> Callable[[str, Mapping[str, Any]], Iterable[Mapping[str, Any]]]:
    """Adapt a connected ``clickhouse-connect`` client to the safe tool boundary.

    The client is supplied by the application after authentication. This module
    never creates connections and never executes mutating statements.
    """
    # dim_signature_hash is a FixedString(16); left to the driver's default it
    # comes back as bytes, which no JSON response and no mask can carry.
    from clickhouse_connect.datatypes.format import set_default_formats

    set_default_formats("FixedString", "string")

    def execute(sql: str, params: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        result = client.query(sql, parameters=dict(params))
        if hasattr(result, "named_results"):
            return result.named_results()
        if hasattr(result, "result_rows") and hasattr(result, "column_names"):
            return [dict(zip(result.column_names, row)) for row in result.result_rows]
        raise TypeError("ClickHouse result must expose named_results() or result_rows/column_names")
    return execute


class FPATools:
    """Tool collection.  Compiled SQL never appears in a tool result."""

    def __init__(
        self,
        scope: UserScope,
        *,
        schema: Schema | None = None,
        executor: Callable[[str, Mapping[str, Any]], Iterable[Mapping[str, Any]]] | None = None,
        masking_hook: MaskingGateHook | None = None,
        audit_logger: ExternalAuditLogger | None = None,
        drift_checker=None,
        reforecast_desk=None,
    ):
        self.drift_checker = drift_checker
        # Present only for a human planner: what propose_reforecast resolves
        # words against (fpa_project.reforecast_requests.ReforecastDesk).
        self.reforecast_desk = reforecast_desk
        self.reforecast_drafts: list[ReforecastProposal] = []
        self.drift_flags = []
        self.scope = scope
        self.schema = schema or Schema()
        self.executor = executor
        self.masking_hook = masking_hook or MaskingGateHook()
        self.draft_drivers: list[DriverProposal] = []
        self.logger = configure_terminal_logging()
        self.audit_logger = audit_logger or ExternalAuditLogger()

    def list_metrics(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "kind": metadata.get("kind"), "available": metadata.get("available", True),
             "allowed_operations": ["RECOMPUTE"] if metadata.get("kind") == "ratio" else ["SUM"]}
            for name, metadata in sorted(self.schema.metrics.items())
        ]

    def list_dimensions(self) -> list[dict[str, Any]]:
        # Everything the compiler accepts in BY and WHERE: the 19 planning
        # dimensions and the separate company/account axes. Found live:
        # listing only the 19 made the agent tell a user the cube has no
        # company dimension. period_month is reached through FOR PERIOD.
        return [{"name": name, "allowed_filter_operators": ["=", "!=", "IN", "NOT IN"]}
                for name in sorted(self.schema.dimensions)]

    def run_finops_query(self, dsl: str) -> QueryToolResult:
        # Scope rejection happens before parsing/compilation so an unscoped
        # caller cannot use error behavior to probe the cube.
        log_event(self.logger, "query_received")
        self.masking_hook.before_tool({"dsl": dsl}, user_id=self.scope.user_id)
        if not self.scope.allowed_companies:
            log_event(self.logger, "scope_rejected", status="REJECTED_SCOPE")
            return QueryToolResult(status="REJECTED_SCOPE", errors=[ToolError(code="REJECTED_SCOPE", message="no allowed company scope")])
        log_event(self.logger, "scope_injected")
        try:
            # SecurityContext is constructed from authenticated scope, not DSL
            # fields or model output.
            compiled = compile_query(dsl, self.schema, SecurityContext(
                self.scope.allowed_companies, self.scope.max_estimated_rows,
            ))
        except (ParseError, DSLValidationError, ValueError) as exc:
            log_event(self.logger, "dsl_validation_failed", status="VALIDATION_ERROR", error_type=type(exc).__name__)
            return QueryToolResult(status="VALIDATION_ERROR", errors=[ToolError(code="INVALID_QUERY", message=str(exc))])
        log_event(self.logger, "query_compiled", estimated_rows=compiled.estimated_rows)
        if self.executor is None:
            log_event(self.logger, "query_execution_skipped", status="EXECUTION_ERROR", code="NO_EXECUTOR")
            return QueryToolResult(status="EXECUTION_ERROR", errors=[ToolError(code="NO_EXECUTOR", message="no ClickHouse executor configured")])
        try:
            log_event(self.logger, "clickhouse_execution_started")
            self.audit_logger.record(
                "clickhouse",
                "request",
                {"sql": compiled.sql, "parameter_names": sorted(compiled.params)},
            )
            raw_rows = list(self.executor(compiled.sql, compiled.params))
            self.masking_hook.after_tool(raw_rows, user_id=self.scope.user_id)
            # Mask after execution too: dimension values can contain hostile
            # or sensitive master-data text even when the query was safe.
            if compiled.bridge:
                # A bridge is read as its decomposition, not its raw lines:
                # the legs are what "explain a gap" means, and the arithmetic
                # check then holds the narrative to these figures.
                raw_rows = self._bridge_nodes(dsl, raw_rows)
            rows = [mask_for_llm(dict(row)) for row in raw_rows]
            self.audit_logger.record("clickhouse", "response", {"rows": rows, "row_count": len(rows)})
            columns = list(rows[0]) if rows else []
            log_event(self.logger, "clickhouse_execution_completed", status="SUCCESS", row_count=len(rows))
            log_event(self.logger, "result_rows_masked", row_count=len(rows))
            if self.drift_checker:
                drift = self.drift_checker(dsl)
                if drift and drift["drift"] and drift not in self.drift_flags:
                    self.drift_flags.append(drift)
            return QueryToolResult(status="SUCCESS", rows=rows, columns=columns, row_count=len(rows), drift_flags=self.drift_flags,
                                   scope=sorted(self.scope.allowed_companies))
        except Exception as exc:
            self.audit_logger.record("clickhouse", "response", {"status": "ERROR", "error_type": type(exc).__name__})
            log_event(self.logger, "clickhouse_execution_failed", status="EXECUTION_ERROR", error_type=type(exc).__name__)
            return QueryToolResult(status="EXECUTION_ERROR", errors=[ToolError(code="EXECUTION_ERROR", message=str(exc))])

    def _bridge_nodes(self, dsl: str, raw_rows: list) -> list[dict[str, Any]]:
        """One row per rollup node: gap, legs and residual in USD, and the vintage read."""
        from fpa_project.bridge_service import _line
        from fpa_project.dsl.bridge import decompose
        from fpa_project.dsl.compiler import vintage_lookup

        query = parse_query(dsl)
        levels = tuple(query.dimensions)
        if not raw_rows:
            return []
        closes = list(self.executor(*vintage_lookup(query.as_of)))
        if not closes:
            raise ValueError(f"no ledger close on or before {query.as_of}")
        vintage = int(closes[0]["vintage"])
        result = decompose([_line(row, levels) for row in raw_rows], levels=levels)
        nodes = []
        for node in result.walk():
            data = node.to_dict(levels)
            row = {level: (node.path[i] if i < len(node.path) else "(all)") for i, level in enumerate(levels)}
            row.update({k: data[k] for k in ("gap", "price", "volume", "mix", "fx", "rate", "efficiency", "residual", "line_count")})
            row.update({"level": node.level, "ties": node.ties, "vintage": vintage, "currency": "USD"})
            nodes.append(row)
        return nodes

    def propose_driver(self, expr_dsl: str, name: str = "draft_driver") -> DriverProposal:
        # Driver proposals are validated and stored as drafts only; there is
        # deliberately no mutation path into the production schema.
        try:
            node = parse_formula(expr_dsl)
            validate_formula(node, self.schema)
            proposal = DriverProposal(name=name, expr_dsl=expr_dsl, references=sorted(referenced_names(node)))
        except (ParseError, DSLValidationError, ValueError) as exc:
            proposal = DriverProposal(name=name, expr_dsl=expr_dsl, errors=[ToolError(code="INVALID_DRIVER", message=str(exc))])
        self.draft_drivers.append(proposal)
        log_event(self.logger, "driver_proposal_created", status="DRAFT", code="INVALID_DRIVER" if proposal.errors else "VALID")
        return proposal

    def propose_reforecast(
        self,
        driver_code: str,
        to_value: float | None = None,
        country: str = "",
        companies: list[str] | None = None,
        period: str = "",
        plan_version_code: str = "",
        by_amount: float | None = None,
        by_percent: float | None = None,
    ) -> ReforecastProposal:
        """Draft a re-forecast: set or move one driver's value for a slice of the plan.

        Give exactly one of to_value, by_amount or by_percent. Never work a change
        out yourself: you do not know what the plan currently applies to the slice,
        so pass the change as asked and let the desk do the arithmetic.

        driver_code: the driver to change (e.g. utilisation).
        to_value: an absolute new value, as the driver stores it; ratios are fractions (74% is 0.74).
        by_amount: a change in the driver's own units, signed. "up by 5%" on a ratio driver is
            five percentage points: by_amount=0.05. "down 2 points" is by_amount=-0.02.
        by_percent: a relative change in percent, signed: "5% higher than it is now" is by_percent=5.
        country: country code or name whose companies the change applies to (e.g. PL); empty means all.
        companies: explicit company codes instead of, or as well as, a country.
        period: FinOpsExpr period the change applies to (2026-H2, 2026-Q3, 2026-07..2026-12); empty means the whole plan year.
        plan_version_code: the plan to re-forecast; empty means the default plan.

        The returned draft carries the resolved from_value and to_value; cite those, not your own.
        Nothing is changed: the planner who asked confirms the draft before anything is recomputed.
        """
        if self.reforecast_desk is None:
            proposal = ReforecastProposal(status="INVALID", driver_code=driver_code, errors=[
                ToolError(code="NOT_A_PLANNER", message="only a human planner can request a re-forecast")])
        else:
            try:
                draft = self.reforecast_desk.resolve(driver_code, to_value, country, companies or [], period, plan_version_code,
                                                     by_amount=by_amount, by_percent=by_percent)
                proposal = ReforecastProposal(**draft.as_dict())
            except ValueError as exc:
                proposal = ReforecastProposal(status="INVALID", driver_code=driver_code,
                                              errors=[ToolError(code="INVALID_REFORECAST", message=str(exc))])
        self.reforecast_drafts.append(proposal)
        log_event(self.logger, "reforecast_proposal_created", status=proposal.status,
                  code=proposal.errors[0].code if proposal.errors else "VALID")
        return proposal
