"""The covenant decision on recomputed figures. Pure: no database, no cube."""

from __future__ import annotations

from fpa_project.recompute.covenants import Figures, Rule, describe_failures, evaluate

ALL = ("base", "stretch", "downside")
RULES = [
    Rule("GM_PCT_FLOOR", "gross_margin_pct", "LEVEL", ">=", 30, "REQUEST", ALL),
    Rule("REVENUE_DROP_LIMIT", "services_revenue", "CHANGE_PCT", ">=", -5, "REQUEST", ALL),
    Rule("DELIVERY_COST_CEILING", "delivery_cost", "CHANGE_PCT", "<=", 3, "REQUEST", ALL),
]


def poland_h2(after_revenue: float, after_cost: float) -> dict:
    # Poland Jul-Dec in the seeded plan: 5.399M revenue, 3.591M delivery cost (base)
    return {(s, "REQUEST"): Figures(5_399_000, 3_591_000, after_revenue, after_cost) for s in ALL}


def test_utilisation_at_74_percent_passes_every_rule_in_every_scenario():
    checks = evaluate(RULES, poland_h2(5_355_000, 3_585_000))
    assert len(checks) == 9
    assert all(check.passed for check in checks)
    margin = next(c for c in checks if c.rule_code == "GM_PCT_FLOOR")
    assert round(margin.measured_value, 2) == 33.05


def test_utilisation_at_60_percent_breaks_margin_and_revenue():
    checks = evaluate(RULES, poland_h2(4_739_000, 3_506_000))
    failed = {(c.rule_code, c.scenario_code) for c in checks if not c.passed}
    assert failed == {(rule, s) for rule in ("GM_PCT_FLOOR", "REVENUE_DROP_LIMIT") for s in ALL}
    assert "GM_PCT_FLOOR (base): 26.02, needs >= 30" in describe_failures(checks)


def test_a_cost_rise_past_the_ceiling_fails():
    checks = evaluate([RULES[2]], poland_h2(5_399_000, 3_591_000 * 1.031))
    assert [c.passed for c in checks] == [False, False, False]


def test_one_failing_scenario_is_enough_to_fail():
    figures = poland_h2(5_355_000, 3_585_000)
    figures[("downside", "REQUEST")] = Figures(5_399_000, 3_591_000, 5_000_000, 3_591_000)
    failed = [c for c in evaluate(RULES, figures) if not c.passed]
    assert {c.scenario_code for c in failed} == {"downside"}


def test_what_cannot_be_measured_does_not_pass():
    checks = evaluate(RULES[:2], {("base", "REQUEST"): Figures(0, 0, 0, 0)})
    assert [c.passed for c in checks] == [False, False]
    assert all("could not be measured" in f for f in describe_failures(checks))


def test_a_rule_only_measures_its_own_scope_and_scenarios():
    rule = Rule("PLAN_GM", "gross_margin_pct", "LEVEL", ">=", 25, "PLAN", ("base",))
    figures = {("base", "PLAN"): Figures(257e6, 179.8e6, 256.9e6, 179.7e6), **poland_h2(1, 1)}
    checks = evaluate([rule], figures)
    assert [(c.scenario_code, c.scope, c.passed) for c in checks] == [("base", "PLAN", True)]
