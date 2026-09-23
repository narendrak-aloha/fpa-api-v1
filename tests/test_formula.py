import pytest

from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import Binary, Reference, detect_cycles, parse_formula, validate_formula
from fpa_project.dsl.schema import Schema


def test_formula_precedence_and_nesting():
    node = parse_formula("PRIOR(services_revenue, 12) * (1 + YOY(bookings) * attach_rate)")
    assert isinstance(node, Binary)
    assert node.operator == "*"


def test_formula_references_resolve_against_schema():
    node = parse_formula("heads * available_hours * utilisation * bill_rate * realisation")
    validate_formula(node, Schema())


def test_unknown_formula_reference_is_rejected():
    with pytest.raises(DSLValidationError, match="unknown formula reference"):
        validate_formula(parse_formula("known_driver * secret_value"), Schema())


def test_ratio_aggregation_is_rejected():
    with pytest.raises(DSLValidationError, match="cannot aggregate ratio"):
        validate_formula(parse_formula("SUM(utilisation)"), Schema())


def test_formula_cycles_are_rejected():
    formulas = {
        "a": parse_formula("b + 1"),
        "b": parse_formula("a * 2"),
    }
    with pytest.raises(DSLValidationError, match="formula cycle detected"):
        detect_cycles(formulas)


def test_prior_reference_is_not_a_formula_cycle():
    formulas = {"growth": parse_formula("PRIOR(services_revenue, 12)")}
    detect_cycles(formulas)


def test_malformed_formula_has_positioned_error():
    with pytest.raises(ParseError, match="position"):
        parse_formula("heads * (")


@pytest.mark.parametrize("expression", ["YOY()", "ROUND(heads, 1, 2)", "ABS(heads, 1)"])
def test_formula_function_arity_is_validated(expression):
    with pytest.raises(DSLValidationError, match="expects"):
        validate_formula(parse_formula(expression), Schema())
