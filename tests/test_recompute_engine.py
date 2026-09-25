"""The recompute arithmetic, tested without a database.

Everything in ``engine.py`` is a pure function, which is what makes these
tests cheap and what lets the workflow call them directly without breaking
determinism.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fpa_project.recompute import engine as calc
from fpa_project.recompute.models import AccountFactor, DriverBinding, DriverShock

# The seeded model's DAG, in the shape planning_model.calc_order_dag stores it.
DAG = [
    {"driver": "heads", "depends_on": []},
    {"driver": "available_hours", "depends_on": ["heads"]},
    {"driver": "utilisation", "depends_on": ["available_hours"]},
    {"driver": "bill_rate", "depends_on": []},
    {"driver": "realisation", "depends_on": ["bill_rate"]},
    {"driver": "attach_rate", "depends_on": ["heads"]},
]


class TestDependents:
    def test_walks_transitively(self):
        # heads -> available_hours -> utilisation, and heads -> attach_rate.
        assert calc.dependents(DAG, "heads") == ["available_hours", "utilisation", "attach_rate"]

    def test_a_leaf_has_none(self):
        # Nothing depends on utilisation, so shocking it moves only its own lines.
        assert calc.dependents(DAG, "utilisation") == []

    def test_unknown_driver_is_not_an_error_here(self):
        # Validation belongs in the snapshot activity, which can see the model.
        assert calc.dependents(DAG, "nonsense") == []

    def test_order_follows_the_declared_calc_order(self):
        # The result ends up in workflow history, so it has to be stable, and
        # it follows calc_order_dag's own order rather than discovery order:
        # attach_rate is reached in one hop but declared last, so it comes last.
        assert calc.dependents(DAG, "heads") == ["available_hours", "utilisation", "attach_rate"]
        assert calc.dependents(DAG, "heads") == calc.dependents(DAG, "heads")


class TestDirtyDrivers:
    def test_shocked_driver_carries_its_own_ratio(self):
        shock = DriverShock("utilisation", 0.75, 0.70)
        ratios = calc.dirty_drivers(DAG, [shock])
        assert ratios == {"utilisation": pytest.approx(0.70 / 0.75)}

    def test_downstream_drivers_inherit_the_ratio(self):
        ratios = calc.dirty_drivers(DAG, [DriverShock("heads", 100, 110)])
        assert set(ratios) == {"heads", "available_hours", "utilisation", "attach_rate"}
        assert all(value == pytest.approx(1.1) for value in ratios.values())

    def test_two_shocks_reaching_one_driver_compose(self):
        # Both moves are real; the driver they meet at sees both.
        ratios = calc.dirty_drivers(DAG, [DriverShock("heads", 100, 110), DriverShock("utilisation", 0.8, 0.6)])
        assert ratios["utilisation"] == pytest.approx(1.1 * 0.75)


class TestAccountFactors:
    def test_elasticity_damps_the_move(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("utilisation", "41000", "quantity", 0.5)],
        )
        # A 20% fall, half absorbed, is a 10% fall.
        assert factors[0].factor == pytest.approx(0.9)

    def test_elasticity_one_is_proportional(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("utilisation", "41000", "quantity", 1.0)],
        )
        assert factors[0].factor == pytest.approx(0.8)

    def test_elasticity_zero_means_no_response(self):
        factors = calc.account_factors(
            {"utilisation": 0.5},
            [DriverBinding("utilisation", "63100", "quantity", 0.0)],
        )
        assert factors[0].factor == pytest.approx(1.0)

    def test_bindings_for_clean_drivers_are_ignored(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("bill_rate", "41000", "unit_price", 1.0)],
        )
        assert factors == []

    def test_two_drivers_on_one_account_compose(self):
        factors = calc.account_factors(
            {"heads": 1.1, "utilisation": 0.9},
            [
                DriverBinding("heads", "51000", "quantity", 1.0),
                DriverBinding("utilisation", "51000", "quantity", 1.0),
            ],
        )
        assert len(factors) == 1
        assert factors[0].factor == pytest.approx(1.1 * 0.9)

    def test_output_is_sorted(self):
        factors = calc.account_factors(
            {"heads": 1.1},
            [
                DriverBinding("heads", "63100", "quantity", 1.0),
                DriverBinding("heads", "51000", "unit_price", 1.0),
                DriverBinding("heads", "51000", "quantity", 1.0),
            ],
        )
        assert [(f.account_code, f.target) for f in factors] == [
            ("51000", "quantity"), ("51000", "unit_price"), ("63100", "quantity"),
        ]


class TestRecomputeLine:
    def test_quantity_factor_applies_to_quantity_only(self):
        factors = [AccountFactor("41000", "quantity", 0.9)]
        quantity, price, amount = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("90.000000")
        assert price == Decimal("200.000000")
        assert amount == Decimal("18000.00")

    def test_price_factor_applies_to_price_only(self):
        factors = [AccountFactor("41000", "unit_price", 1.05)]
        quantity, price, amount = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("100.000000")
        assert price == Decimal("210.000000")

    def test_another_accounts_factor_is_not_applied(self):
        factors = [AccountFactor("51000", "quantity", 0.5)]
        quantity, _, _ = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("100.000000")

    def test_amount_ties_to_the_rounded_inputs(self):
        # plan_version_line has a check constraint that amount_functional
        # equals round(quantity * unit_price, 2). Rounding the inputs first is
        # what makes that hold; multiplying the raw floats does not.
        factors = [AccountFactor("41000", "quantity", 0.9333333333)]
        quantity, price, amount = calc.recompute_line(133.337, 187.774, factors, "41000")
        assert amount == (quantity * price).quantize(Decimal("0.01"))

    def test_precision_matches_the_columns(self):
        factors = [AccountFactor("41000", "quantity", 1.0 / 3.0)]
        quantity, price, _ = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity.as_tuple().exponent == -6
        assert price.as_tuple().exponent == -6

    def test_no_factors_leaves_the_line_alone(self):
        quantity, price, amount = calc.recompute_line(12.5, 4.0, [], "41000")
        assert (quantity, price, amount) == (Decimal("12.500000"), Decimal("4.000000"), Decimal("50.00"))

    def test_is_a_pure_function_of_its_inputs(self):
        # The idempotence guarantee rests on this: same baseline, same shock,
        # same numbers, however many times it runs.
        factors = [AccountFactor("41000", "quantity", 0.9333)]
        first = calc.recompute_line(133.337, 187.774, factors, "41000")
        second = calc.recompute_line(133.337, 187.774, factors, "41000")
        assert first == second


class TestDerivationTrace:
    def test_names_the_driver_the_formula_and_the_inputs(self):
        factors, trace = calc.plan_factors(
            MODEL_DAG, [DriverShock("utilisation", 0.75, 0.72)],
            [DriverBinding("utilisation", "41000", "quantity", 1.0)], FORMULAS,
        )
        result = calc.recompute_line(100.0, 50.0, factors, "41000")
        line = calc.derivation_trace("41000", factors, trace, baseline=(100.0, 50.0), result=result)
        # The three keys migration 016 requires, each saying something real.
        assert line["driver"] == "utilisation"
        assert "baseline_quantity" in line["formula"] and "ratio of utilisation" in line["formula"]
        assert line["inputs"]["baseline"] == {"quantity": 100.0, "unit_price": 50.0}
        assert line["inputs"]["drivers"]["utilisation"]["from"] == 0.75
        assert line["inputs"]["drivers"]["utilisation"]["to"] == 0.72
        assert line["inputs"]["bindings"] == [
            {"target": "quantity", "driver": "utilisation", "elasticity": 1.0, "ratio": 0.96, "factor": 0.96,
             "via": "utilisation"},
        ]
        assert line["inputs"]["applied_factors"] == {"quantity": 0.96}
        assert line["result"] == {"quantity": "96.000000", "unit_price": "50.000000", "amount": "4800.00"}

    def test_a_downstream_driver_is_named_by_the_shock_that_moved_it(self):
        factors, trace = calc.plan_factors(
            MODEL_DAG, [DriverShock("attrition", 0.12, 0.18)],
            [DriverBinding("heads", "51000", "quantity", 1.0)], FORMULAS,
        )
        line = calc.derivation_trace("51000", factors, trace)
        assert line["driver"] == "attrition"
        assert line["inputs"]["drivers"]["heads"]["formula"] == "PRIOR(heads, 1) * (1 - attrition / 12)"

    def test_is_never_empty(self):
        # Even with nothing applied the three required keys are present.
        line = calc.derivation_trace("41000", [], {})
        assert line["driver"] and line["formula"] and isinstance(line["inputs"], dict)


# The seeded model: formulas and the DAG derived from them.
MODEL_DAG = [
    {"driver": "attrition", "depends_on": []},
    {"driver": "heads", "depends_on": ["attrition"]},
    {"driver": "available_hours", "depends_on": ["heads"]},
    {"driver": "utilisation", "depends_on": []},
    {"driver": "billable_hours", "depends_on": ["available_hours", "utilisation"]},
    {"driver": "rate_increase", "depends_on": []},
    {"driver": "bill_rate", "depends_on": ["rate_increase"]},
]
FORMULAS = {
    "attrition": "0.12", "heads": "PRIOR(heads, 1) * (1 - attrition / 12)", "available_hours": "heads * 160",
    "utilisation": "0.75", "billable_hours": "available_hours * utilisation",
    "rate_increase": "0.03", "bill_rate": "PRIOR(bill_rate, 12) * (1 + rate_increase)",
}


class TestEvaluatedRatios:
    def test_a_multiplicative_dependent_moves_with_its_input(self):
        ratios = calc.dirty_drivers(MODEL_DAG, [DriverShock("utilisation", 0.75, 0.72)], FORMULAS)
        assert ratios == {"utilisation": pytest.approx(0.96), "billable_hours": pytest.approx(0.96)}

    def test_an_additive_input_is_evaluated_not_inherited(self):
        # Inheriting would scale heads by attrition's own 1.5, in the wrong
        # direction. The formula says heads shrink: (1 - 0.18/12) / (1 - 0.12/12).
        ratios = calc.dirty_drivers(MODEL_DAG, [DriverShock("attrition", 0.12, 0.18)], FORMULAS)
        expected = (1 - 0.18 / 12) / (1 - 0.12 / 12)
        assert ratios["heads"] == pytest.approx(expected)
        assert ratios["available_hours"] == pytest.approx(expected)
        assert ratios["billable_hours"] == pytest.approx(expected)

    def test_a_rate_increase_moves_the_bill_rate_by_one_plus_the_rate(self):
        ratios = calc.dirty_drivers(MODEL_DAG, [DriverShock("rate_increase", 0.03, 0.05)], FORMULAS)
        assert ratios["bill_rate"] == pytest.approx(1.05 / 1.03)

    def test_the_dag_is_sorted_not_trusted(self):
        # The seeded calc_order_dag is stored alphabetically: billable_hours
        # before utilisation. Found live: evaluated in that order, the
        # dependent read utilisation before it existed and reported ratio 1.0.
        alphabetical = sorted(MODEL_DAG, key=lambda node: node["driver"])
        ratios = calc.dirty_drivers(alphabetical, [DriverShock("utilisation", 0.75, 0.70)], FORMULAS)
        assert ratios["billable_hours"] == pytest.approx(0.70 / 0.75)
        entry = calc.evaluate_ratios(alphabetical, [DriverShock("utilisation", 0.75, 0.70)], FORMULAS)["billable_hours"]
        assert entry["to"] / entry["from"] == pytest.approx(0.70 / 0.75)
        attrition = calc.dirty_drivers(alphabetical, [DriverShock("attrition", 0.12, 0.18)], FORMULAS)
        assert attrition["heads"] == pytest.approx((1 - 0.18 / 12) / (1 - 0.12 / 12))

    def test_without_formulas_the_old_inheritance_is_kept(self):
        # Histories recorded before formulas were snapshotted replay unchanged.
        ratios = calc.dirty_drivers(MODEL_DAG, [DriverShock("attrition", 0.12, 0.18)])
        assert ratios["heads"] == pytest.approx(1.5)


class TestTwoMovesOfOneDriver:
    def test_each_move_is_its_own_binding_named_by_its_scope(self):
        shocks = [DriverShock("utilisation", 0.75, 0.70),
                  DriverShock("utilisation", 0.70, 0.69, ["RTPL1"], ["2026-07-01"])]
        factors, trace = calc.plan_factors(MODEL_DAG, shocks, [DriverBinding("utilisation", "41000", "quantity", 1.0)], FORMULAS)
        line = calc.derivation_trace("41000", factors, trace, "RTPL1", "2026-07-01")
        assert line["inputs"]["applied_factors"]["quantity"] == pytest.approx(0.69 / 0.75)
        assert [b["via"] for b in line["inputs"]["bindings"]] == ["utilisation", "utilisation [RTPL1 2026-07]"]
        assert "ratio of utilisation [RTPL1 2026-07]" in line["formula"]
        # Outside the scope only the whole-plan move applies.
        outside = calc.derivation_trace("41000", factors, trace, "RTDE1", "2026-07-01")
        assert outside["inputs"]["applied_factors"]["quantity"] == pytest.approx(0.70 / 0.75)


class TestDirtyScopes:
    def test_a_scoped_shock_dirties_only_its_companies_and_months(self):
        factors, _ = calc.plan_factors(
            MODEL_DAG, [DriverShock("utilisation", 0.75, 0.72, ["RTPL1"], ["2026-07-01"])],
            [DriverBinding("utilisation", "41000", "quantity", 1.0)], FORMULAS,
        )
        assert calc.dirty_scopes(factors) == [(["41000"], ["RTPL1"], ["2026-07-01"])]

    def test_an_unscoped_shock_is_account_only(self):
        factors, _ = calc.plan_factors(
            MODEL_DAG, [DriverShock("utilisation", 0.75, 0.72)],
            [DriverBinding("utilisation", "41000", "quantity", 1.0),
             DriverBinding("utilisation", "51000", "quantity", 0.35)], FORMULAS,
        )
        assert calc.dirty_scopes(factors) == [(["41000", "51000"], [], [])]


class TestPartitionPlan:
    def test_groups_months_up_to_the_target(self):
        counts = {("base", f"2026-{m:02d}-01"): 100 for m in range(1, 13)}
        months = sorted({month for _, month in counts})
        plan = calc.partition_plan(["base"], months, counts, target_size=350)
        assert [rows for _, _, rows in plan] == [300, 300, 300, 300]

    def test_a_month_bigger_than_the_target_is_its_own_partition(self):
        # Splitting inside a month would let two children write the same row.
        counts = {("base", "2026-01-01"): 9_000, ("base", "2026-02-01"): 100}
        plan = calc.partition_plan(["base"], ["2026-01-01", "2026-02-01"], counts, target_size=5_000)
        assert plan == [("base", ["2026-01-01"], 9_000), ("base", ["2026-02-01"], 100)]

    def test_empty_months_are_skipped(self):
        counts = {("base", "2026-01-01"): 10, ("base", "2026-03-01"): 10}
        plan = calc.partition_plan(["base"], ["2026-01-01", "2026-02-01", "2026-03-01"], counts, 5_000)
        assert plan == [("base", ["2026-01-01", "2026-03-01"], 20)]

    def test_scenarios_never_share_a_partition(self):
        counts = {("base", "2026-01-01"): 10, ("stretch", "2026-01-01"): 10}
        plan = calc.partition_plan(["base", "stretch"], ["2026-01-01"], counts, 5_000)
        assert [scenario for scenario, _, _ in plan] == ["base", "stretch"]

    def test_no_dirty_rows_means_no_partitions(self):
        assert calc.partition_plan(["base"], ["2026-01-01"], {}, 5_000) == []


class TestMergeShocks:
    def test_adds_new_drivers_and_keeps_published_ones(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("heads", 100, 110)])
        assert [(s.driver_code, s.from_value, s.to_value) for s in merged] == [("heads", 100, 110), ("utilisation", 0.75, 0.70)]

    def test_a_driver_moved_again_keeps_its_baseline(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("utilisation", 0.70, 0.65)])
        assert [(s.from_value, s.to_value) for s in merged] == [(0.75, 0.65)]

    def test_a_move_back_to_baseline_stays_as_a_ratio_of_one(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("utilisation", 0.70, 0.75)])
        assert merged[0].ratio() == 1.0

    def test_merging_is_idempotent_and_ordered(self):
        once = calc.merge_shocks([DriverShock("b", 1, 2)], [DriverShock("a", 1, 3)])
        assert calc.merge_shocks(once, once) == once
        assert [s.driver_code for s in once] == ["a", "b"]
