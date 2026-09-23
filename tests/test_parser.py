import pytest

from fpa_project.dsl.errors import ParseError
from fpa_project.dsl.parser import parse_query


def test_full_query_parses():
    query = parse_query(
        "SELECT services_revenue, YOY(bookings) AS bookings_yoy "
        "BY practice, geo_country "
        "WHERE geo_country IN ('PL', 'DE') AND engine != 'Shared' "
        "FOR PERIOD 2026-Q2 "
        "AS OF '2026-07-05T18:00:00' "
        "COMPARE PLAN pv='PV-2026-0001', scenario='downside' TO ACTUAL "
        "BRIDGE LIMIT 50"
    )
    assert len(query.measures) == 2
    assert query.dimensions == ("practice", "geo_country")
    assert query.period.start == "2026-Q2"
    assert query.period.end == "2026-Q2"
    assert query.plan.scenario == "downside"
    assert query.bridge is True
    assert query.limit == 50


def test_period_range_and_nested_time_function():
    query = parse_query("SELECT ROLLING(PRIOR(bookings, 1), 3) FOR PERIOD 2026-Q1..2026-Q4")
    assert query.period.start == "2026-Q1"
    assert query.period.end == "2026-Q4"


def test_month_and_year_periods_parse():
    assert parse_query("SELECT services_revenue FOR PERIOD 2026-04").period.start == "2026-04"
    assert parse_query("SELECT services_revenue FOR PERIOD 2026").period.start == "2026"


@pytest.mark.parametrize(
    "source",
    [
        "SELECT services_revenue WHERE geo_country IN ()",
        "SELECT services_revenue BRIDGE",
        "SELECT services_revenue FOR PERIOD 2026-Q5",
        "SELECT services_revenue AS OF '2026-99-99T00:00:00'",
        "SELECT services_revenue LIMIT 1.5",
    ],
)
def test_invalid_syntax_is_rejected(source):
    with pytest.raises(ParseError):
        parse_query(source)


def test_not_in_requires_a_list():
    query = parse_query("SELECT services_revenue WHERE geo_country NOT IN ('PL')")
    assert query.predicates[0].operator == "NOT IN"
