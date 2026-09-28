"""Planning-model registry boundary used by the agent layer."""

from __future__ import annotations

from dataclasses import dataclass

from fpa_project.dsl.ast import Aggregate, Query, TimeFunction
from fpa_project.dsl.compiler import Compiler
from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import detect_cycles, parse_formula, validate_formula
from fpa_project.dsl.schema import Schema


@dataclass(frozen=True)
class RegistryIssue:
    code: str
    message: str
    field: str | None = None


class PlanningRegistry:
    def __init__(self, schema: Schema | None = None):
        self.schema = schema or Schema()

    def validate(self, query: Query) -> list[RegistryIssue]:
        # This boundary intentionally exposes planning-safe semantics only;
        # SQL-specific checks remain in the compiler.
        issues: list[RegistryIssue] = []
        for name in query.dimensions:
            if name not in self.schema.dimensions:
                issues.append(RegistryIssue("UNKNOWN_DIMENSION", f"unknown dimension: {name}", name))
        for predicate in query.predicates:
            if predicate.field not in self.schema.dimensions:
                issues.append(RegistryIssue("INVALID_PREDICATE", "predicates must reference dimensions", predicate.field))
            if predicate.operator not in {"=", "!=", "IN", "NOT IN"}:
                issues.append(RegistryIssue("INVALID_OPERATOR", f"unsupported operator: {predicate.operator}", predicate.field))
        for measure in query.measures:
            if isinstance(measure.name, Aggregate):
                # The compiler owns the type rule; ask it, so the agent gets
                # the same explanation a human would at the API.
                try:
                    Compiler(self.schema).validate_aggregate(measure.name)
                except DSLValidationError as exc:
                    issues.append(RegistryIssue("ILLEGAL_AGGREGATION", str(exc), measure.name.metric))
                continue
            names = self._measure_names(measure.name)
            for name in names:
                if name not in self.schema.metric_names:
                    issues.append(RegistryIssue("UNKNOWN_METRIC", f"unknown measure: {name}", name))
                elif not self.schema.metrics[name].get("available", True):
                    issues.append(RegistryIssue("UNAVAILABLE_METRIC", f"measure is not available: {name}", name))
            if isinstance(measure.name, TimeFunction) and measure.name.name in {"PRIOR", "LEAD", "ROLLING"}:
                if measure.name.offset is not None and measure.name.offset <= 0:
                    issues.append(RegistryIssue("INVALID_OFFSET", "time-function offset must be positive"))
        if query.plan and query.plan.scenario not in self.schema.scenarios:
            issues.append(RegistryIssue("UNKNOWN_SCENARIO", f"unknown scenario: {query.plan.scenario}"))
        if query.bridge and query.plan is None:
            issues.append(RegistryIssue("BRIDGE_REQUIRES_PLAN", "BRIDGE requires COMPARE PLAN ... TO ACTUAL"))
        return issues

    def _measure_names(self, value: str | TimeFunction) -> set[str]:
        return self._measure_names(value.metric) if isinstance(value, TimeFunction) else {value}

    def validate_driver(self, name: str, expression: str) -> list[RegistryIssue]:
        """Validate one driver at the authoring boundary before persistence."""
        if not name or not name[0].isalpha() or not all(
            character.isalnum() or character == "_" for character in name
        ):
            return [RegistryIssue("INVALID_DRIVER_NAME", "driver name must be an identifier", name)]
        try:
            validate_formula(parse_formula(expression), self.schema)
        except (ParseError, DSLValidationError, ValueError) as exc:
            return [RegistryIssue("INVALID_DRIVER", str(exc), name)]
        return []

    def validate_model(self, formulas: dict[str, str]) -> list[RegistryIssue]:
        """Validate and cycle-check a complete planning model before saving it."""
        issues: list[RegistryIssue] = []
        parsed = {}
        model_schema = Schema({**self.schema.data, "drivers": sorted(self.schema.driver_names | set(formulas))})
        for name, expression in formulas.items():
            driver_issues: list[RegistryIssue]
            if not name or not name[0].isalpha() or not all(
                character.isalnum() or character == "_" for character in name
            ):
                driver_issues = [RegistryIssue("INVALID_DRIVER_NAME", "driver name must be an identifier", name)]
            else:
                try:
                    node = parse_formula(expression)
                    validate_formula(node, model_schema)
                    driver_issues = []
                except (ParseError, DSLValidationError, ValueError) as exc:
                    driver_issues = [RegistryIssue("INVALID_DRIVER", str(exc), name)]
            issues.extend(driver_issues)
            if not driver_issues:
                parsed[name] = node
        try:
            detect_cycles(parsed)
        except DSLValidationError as exc:
            issues.append(RegistryIssue("FORMULA_CYCLE", str(exc)))
        return issues
