"""The covenant check on a re-forecast's recomputed lines. No I/O.

The activity in ``activities.py`` reads the rules from ``covenant_rule`` and
the figures from the cube; this module only decides. For each active rule and
each scenario it names, the recomputed plan is measured and compared:

- ``LEVEL``: the recomputed value itself (gross margin % is in percent).
- ``CHANGE_PCT``: the percent change against the plan currently in the cube.

A rule's ``scope`` says what is measured: ``REQUEST`` is only the companies
and months the re-forecast changes, ``PLAN`` is the whole plan. A request with
no scope changes everything, so for it the two are the same.

Anything that cannot be measured (a zero base for a change, no revenue for a
margin) fails the rule: a covenant that cannot be shown to hold does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass

REVENUE = "revenue"
COST = "cost"


@dataclass(frozen=True)
class Rule:
    rule_code: str
    metric: str
    measure: str
    comparator: str
    threshold: float
    scope: str
    scenarios: tuple[str, ...]


@dataclass(frozen=True)
class Figures:
    """Revenue and delivery cost, in USD, before and after the re-forecast."""

    before_revenue: float
    before_cost: float
    after_revenue: float
    after_cost: float


@dataclass(frozen=True)
class Check:
    rule_code: str
    scenario_code: str
    scope: str
    metric: str
    measure: str
    before_value: float | None
    after_value: float | None
    measured_value: float | None
    comparator: str
    threshold: float
    passed: bool


def metric_value(metric: str, revenue: float, cost: float) -> float | None:
    if metric == "services_revenue":
        return revenue
    if metric == "delivery_cost":
        return cost
    if metric == "gross_margin_pct":
        return None if revenue == 0 else (revenue - cost) / revenue * 100
    raise ValueError(f"unknown covenant metric {metric!r}")


def evaluate(rules: list[Rule], figures: dict[tuple[str, str], Figures]) -> list[Check]:
    """One check per (rule, scenario). ``figures`` is keyed by (scenario, scope)."""
    checks: list[Check] = []
    for rule in sorted(rules, key=lambda r: r.rule_code):
        for scenario in sorted(rule.scenarios):
            numbers = figures.get((scenario, rule.scope))
            if numbers is None:
                continue
            before = metric_value(rule.metric, numbers.before_revenue, numbers.before_cost)
            after = metric_value(rule.metric, numbers.after_revenue, numbers.after_cost)
            if rule.measure == "LEVEL":
                measured = after
            elif rule.measure == "CHANGE_PCT":
                measured = None if before in (None, 0) or after is None else (after - before) / abs(before) * 100
            else:
                raise ValueError(f"unknown covenant measure {rule.measure!r}")
            if measured is None:
                passed = False
            elif rule.comparator == ">=":
                passed = measured >= rule.threshold
            elif rule.comparator == "<=":
                passed = measured <= rule.threshold
            else:
                raise ValueError(f"unknown covenant comparator {rule.comparator!r}")
            checks.append(Check(
                rule_code=rule.rule_code, scenario_code=scenario, scope=rule.scope,
                metric=rule.metric, measure=rule.measure,
                before_value=_round(before), after_value=_round(after), measured_value=_round(measured),
                comparator=rule.comparator, threshold=rule.threshold, passed=passed,
            ))
    return checks


def describe_failures(checks: list[Check]) -> list[str]:
    return [
        f"{c.rule_code} ({c.scenario_code}): {c.measured_value:.2f}, needs {c.comparator} {c.threshold:g}"
        if c.measured_value is not None else f"{c.rule_code} ({c.scenario_code}): could not be measured"
        for c in checks if not c.passed
    ]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
