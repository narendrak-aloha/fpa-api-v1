"""The grammar itself: precedence, nesting, the time operators and the failures.

Both entry rules parse to an AST and nothing here touches SQL. There is no
eval anywhere in the path: the parsers are recursive descent over a token
list, and the tests that would catch an eval sneaking in are the injection
ones at the bottom.
"""

from decimal import Decimal

import pytest

from fpa_project.dsl.ast import Aggregate, TimeFunction
from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import (
    Binary, Function, Number, Reference, Unary, dependency_names, detect_cycles, parse_formula,
    referenced_names, validate_formula,
)
from fpa_project.dsl.parser import parse_query
from fpa_project.dsl.schema import Schema


# ---------------------------------------------------------------------------
# Entry 1: driver expressions
# ---------------------------------------------------------------------------
class TestFormulaPrecedence:
    def test_multiplication_binds_tighter_than_addition(self):
        node = parse_formula("a + b * c")
        assert node == Binary("+", Reference("a"), Binary("*", Reference("b"), Reference("c")))

    def test_left_associative_subtraction(self):
        # a - b - c is (a - b) - c, not a - (b - c).
        node = parse_formula("a - b - c")
        assert node == Binary("-", Binary("-", Reference("a"), Reference("b")), Reference("c"))

    def test_exponent_is_right_associative_and_tightest(self):
        node = parse_formula("2 * a ^ b ^ c")
        assert node == Binary("*", Number(Decimal(2)), Binary("^", Reference("a"), Binary("^", Reference("b"), Reference("c"))))

    def test_unary_minus_wraps_the_primary_not_the_product(self):
        node = parse_formula("-a * b")
        assert node == Binary("*", Unary("-", Reference("a")), Reference("b"))

    def test_parentheses_override_precedence(self):
        node = parse_formula("(a + b) * c")
        assert node == Binary("*", Binary("+", Reference("a"), Reference("b")), Reference("c"))

    def test_functions_nest_and_take_numeric_offsets(self):
        node = parse_formula("PRIOR(services_revenue, 12) * (1 + YOY(bookings) * attach_rate)")
        assert isinstance(node, Binary)
        assert node.left == Function("PRIOR", (Reference("services_revenue"), Number(Decimal(12))))
        assert isinstance(node.right, Binary)

    def test_function_names_are_case_insensitive_references_are_not(self):
        assert parse_formula("prior(x, 1)") == Function("PRIOR", (Reference("x"), Number(Decimal(1))))
        assert parse_formula("Heads") == Reference("Heads")


class TestFormulaFailures:
    @pytest.mark.parametrize("source, position", [("heads * (", 9), ("heads +", 7), ("* heads", 0), ("heads ) ", 6)])
    def test_malformed_formula_names_the_position(self, source, position):
        with pytest.raises(ParseError, match=f"position {position}"):
            parse_formula(source)

    def test_unknown_reference_is_named(self):
        with pytest.raises(DSLValidationError, match="unknown formula reference: secret_value"):
            validate_formula(parse_formula("heads * secret_value"), Schema())

    def test_unknown_function_is_named(self):
        with pytest.raises(DSLValidationError, match="unknown formula function: EXEC"):
            validate_formula(parse_formula("EXEC(heads)"), Schema())

    def test_python_is_not_a_formula(self):
        # The one line that would matter if there were an eval anywhere.
        with pytest.raises(ParseError):
            parse_formula("__import__('os').system('id')")


class TestCycles:
    def test_a_prior_self_reference_is_a_legitimate_model(self):
        """A growth rate off last period's number is not a cycle."""
        formulas = {"bill_rate": parse_formula("PRIOR(bill_rate, 1) * (1 + rate_increase)"), "rate_increase": parse_formula("0.03")}
        detect_cycles(formulas)

    def test_a_same_period_self_reference_is_a_broken_graph(self):
        with pytest.raises(DSLValidationError, match="formula cycle detected: heads -> heads"):
            detect_cycles({"heads": parse_formula("heads * (1 - attrition)")})

    def test_a_two_node_cycle_names_the_path(self):
        formulas = {"a": parse_formula("b + 1"), "b": parse_formula("a * 2")}
        with pytest.raises(DSLValidationError, match="a -> b -> a"):
            detect_cycles(formulas)

    def test_a_cycle_through_a_prior_is_broken_by_the_prior(self):
        # a needs b now; b needs a *last period*. That is a lag, not a loop.
        formulas = {"a": parse_formula("b * 2"), "b": parse_formula("PRIOR(a, 1) + 1")}
        detect_cycles(formulas)

    def test_dependency_names_exclude_time_shifted_references_but_referenced_names_do_not(self):
        node = parse_formula("PRIOR(bill_rate, 1) * (1 + rate_increase)")
        assert dependency_names(node) == {"rate_increase"}
        assert referenced_names(node) == {"bill_rate", "rate_increase"}

    def test_rolling_and_yoy_are_same_period_dependencies(self):
        # ROLLING includes the current period, so it is a real edge.
        assert dependency_names(parse_formula("ROLLING(x, 3)")) == {"x"}
        assert dependency_names(parse_formula("YOY(x)")) == {"x"}


# ---------------------------------------------------------------------------
# Entry 2: queries
# ---------------------------------------------------------------------------
class TestQueryGrammar:
    def test_every_clause_in_order(self):
        query = parse_query(
            "SELECT services_revenue, YOY(bookings) AS bookings_yoy "
            "BY practice, geo_country "
            "WHERE geo_country IN ('PL', 'DE') AND engine != 'Shared' "
            "FOR PERIOD 2026-Q2 "
            "AS OF '2026-07-05T18:00:00' "
            "COMPARE PLAN pv='PV-2026-0001', scenario='downside' TO ACTUAL "
            "BRIDGE LIMIT 50"
        )
        assert [m.name for m in query.measures] == ["services_revenue", TimeFunction("YOY", "bookings")]
        assert query.measures[1].alias == "bookings_yoy"
        assert query.dimensions == ("practice", "geo_country")
        assert query.predicate_connectors == ("AND",)
        assert query.period.start == query.period.end == "2026-Q2"
        assert query.as_of == "2026-07-05T18:00:00"
        assert query.plan.version == "PV-2026-0001" and query.plan.scenario == "downside"
        assert query.bridge is True and query.limit == 50

    def test_clauses_out_of_order_are_rejected(self):
        with pytest.raises(ParseError):
            parse_query("SELECT services_revenue FOR PERIOD 2026 BY practice")

    def test_time_function_offset_and_nesting_parse(self):
        query = parse_query("SELECT ROLLING(PRIOR(bookings, 1), 3) FOR PERIOD 2026-Q1..2026-Q4")
        outer = query.measures[0].name
        assert outer == TimeFunction("ROLLING", TimeFunction("PRIOR", "bookings", Decimal(1)), Decimal(3))

    def test_aggregates_parse_to_their_own_node(self):
        query = parse_query("SELECT SUM(utilisation)")
        assert query.measures[0].name == Aggregate("SUM", "utilisation")

    @pytest.mark.parametrize("period", ["2026", "2026-H2", "2026-Q4", "2026-04", "2026-01..2026-06"])
    def test_period_forms(self, period):
        parse_query(f"SELECT services_revenue FOR PERIOD {period}")

    def test_string_literal_escaping(self):
        query = parse_query("SELECT services_revenue WHERE customer = 'O''Brien'")
        assert query.predicates[0].values[0].value == "O'Brien"

    def test_numeric_literal_is_decimal(self):
        query = parse_query("SELECT services_revenue WHERE services_revenue >= 12.5")
        assert query.predicates[0].values[0].value == Decimal("12.5")

    @pytest.mark.parametrize(
        "source",
        [
            "SELECT services_revenue WHERE geo_country IN ()",
            "SELECT services_revenue BRIDGE",
            "SELECT services_revenue FOR PERIOD 2026-Q5",
            "SELECT services_revenue FOR PERIOD 2026-13",
            "SELECT services_revenue AS OF '2026-99-99T00:00:00'",
            "SELECT services_revenue LIMIT 1.5",
            "SELECT services_revenue COMPARE PLAN scenario='base' TO ACTUAL",
            "SELECT services_revenue; DROP TABLE x",
            "SELECT * FROM fact_gl_actual",
            "SELECT services_revenue WHERE geo_country = PL",
        ],
    )
    def test_invalid_syntax_is_rejected(self, source):
        with pytest.raises(ParseError):
            parse_query(source)

    def test_not_in_requires_a_list(self):
        query = parse_query("SELECT services_revenue WHERE geo_country NOT IN ('PL')")
        assert query.predicates[0].operator == "NOT IN"
