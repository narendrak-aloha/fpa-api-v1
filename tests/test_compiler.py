"""The compiler contract: names, types, scope, parameters, cost and vintage.

Everything here is about the SQL text and the parameter map. The tests that
run the SQL against a real cube live in test_cube_integration.py.
"""

import re

import pytest

from fpa_project.dsl.compiler import SecurityContext, compile_query, vintage_lookup
from fpa_project.dsl.errors import DSLValidationError

Q2 = "FOR PERIOD 2026-Q2"


def test_compiles_parameterised_clickhouse_sql():
    compiled = compile_query(
        "SELECT services_revenue, gross_margin_pct AS margin "
        "BY company, practice "
        "WHERE geo_country = 'PL' "
        "FOR PERIOD 2026-Q2 LIMIT 10"
    )
    assert "FROM (SELECT * FROM fpa_cube.fact_gl_actual WHERE" in compiled.sql
    assert "GROUP BY company, practice" in compiled.sql
    assert "toDate({p1:String})" in compiled.sql
    assert "{p0:String}" in compiled.sql
    assert "PL" not in compiled.sql
    assert compiled.params["p0"] == "PL"
    assert compiled.params["p1"] == "2026-04-01"
    assert compiled.params["p2"] == "2026-07-01"


def test_plan_compiles_to_plan_fact_and_scenario_predicates():
    compiled = compile_query(
        "SELECT delivery_cost BY geo_country "
        "FOR PERIOD 2026-H1 "
        "COMPARE PLAN pv='PV-2026-0001', scenario='stretch' TO ACTUAL"
    )
    assert "fact_plan_line AS p" in compiled.sql
    assert "p.plan_version =" in compiled.sql
    assert "p.scenario_id =" in compiled.sql
    assert "PV-2026-0001" in compiled.params.values()
    assert "stretch" in compiled.params.values()


# ---------------------------------------------------------------------------
# Reading the past as it was
# ---------------------------------------------------------------------------
def test_actual_reads_are_vintage_aware_even_without_as_of():
    """A current read is an as-of read at the latest close, built the same way."""
    compiled = compile_query(f"SELECT delivery_cost {Q2}")
    assert "_version <= (SELECT max(vintage) FROM fpa_cube.dim_ledger_vintage)" in compiled.sql
    assert ("ORDER BY _version DESC, amount_functional DESC, quantity DESC "
            "LIMIT 1 BY company, period_month, account, dim_signature_hash, voucher_no") in compiled.sql
    assert "a._is_deleted = 0" in compiled.sql
    assert "FINAL" not in compiled.sql
    assert compiled.vintage == "current"


def test_as_of_resolves_a_close_and_is_parameterised():
    compiled = compile_query(f"SELECT delivery_cost {Q2} AS OF '2026-07-05T18:00:00'")
    placeholder = re.search(r"closed_at <= \{(p\d+):DateTime\}", compiled.sql)
    assert placeholder, compiled.sql
    assert compiled.params[placeholder.group(1)] == "2026-07-05T18:00:00"
    assert "2026-07-05" not in compiled.sql
    assert compiled.vintage == "2026-07-05T18:00:00"


def test_vintage_lookup_names_the_close_a_read_ran_on():
    sql, params = vintage_lookup("2026-07-05T18:00:00")
    assert "closed_at <= {as_of:DateTime}" in sql and params == {"as_of": "2026-07-05T18:00:00"}
    sql, params = vintage_lookup(None)
    assert "ORDER BY closed_at DESC LIMIT 1" in sql and params == {}


def test_as_of_cannot_be_silently_ignored_for_plan_reads():
    with pytest.raises(DSLValidationError, match="only valid for actual-ledger"):
        compile_query(
            f"SELECT delivery_cost {Q2} AS OF '2026-07-05T18:00:00' COMPARE PLAN pv='PV-2026-0001' TO ACTUAL"
        )


# ---------------------------------------------------------------------------
# Time functions: a monthly series, windowed by month distance
# ---------------------------------------------------------------------------
def test_time_function_compiles_to_three_layers_with_range_frames():
    compiled = compile_query(f"SELECT YOY(services_revenue) BY practice {Q2}")
    assert compiled.monthly
    assert "toYear(period_month) * 12 + toMonth(period_month) AS month_no" in compiled.sql
    assert "GROUP BY practice, period_month" in compiled.sql
    assert "OVER (PARTITION BY practice ORDER BY month_no RANGE BETWEEN 12 PRECEDING AND 12 PRECEDING)" in compiled.sql
    assert "ROWS BETWEEN" not in compiled.sql
    # The inner read is widened by the lookback; the outer keeps what was asked for.
    assert compiled.params["p0"] == "2025-04-01"
    assert compiled.params["p1"] == "2026-07-01"
    assert compiled.params["p2"] == "2026-04-01"


def test_rolling_reads_back_its_window_and_sums_forward():
    compiled = compile_query(f"SELECT ROLLING(services_revenue, 3) {Q2}")
    assert "RANGE BETWEEN 2 PRECEDING AND CURRENT ROW" in compiled.sql
    assert compiled.params["p0"] == "2026-02-01"


def test_ytd_partitions_by_year_and_reads_from_january():
    compiled = compile_query("SELECT YTD(services_revenue) BY practice FOR PERIOD 2026-06")
    assert "PARTITION BY practice, toYear(period_month)" in compiled.sql
    assert compiled.params["p0"] == "2026-01-01"


def test_lead_reads_forward():
    compiled = compile_query("SELECT LEAD(services_revenue, 2) FOR PERIOD 2026-04")
    assert "RANGE BETWEEN 2 FOLLOWING AND 2 FOLLOWING" in compiled.sql
    assert compiled.params["p1"] == "2026-07-01"


@pytest.mark.parametrize(
    "source, message",
    [
        ("SELECT YOY(services_revenue)", "needs FOR PERIOD"),
        (f"SELECT ROLLING(utilisation, 3) {Q2}", "ratio measure"),
        (f"SELECT YTD(headcount) {Q2}", "semi-additive"),
        (f"SELECT ROLLING(PRIOR(services_revenue, 1), 3) {Q2}", "nested time functions"),
        (f"SELECT PRIOR(services_revenue, 0) {Q2}", "offset must be positive"),
    ],
)
def test_time_function_misuse_is_specific(source, message):
    with pytest.raises(DSLValidationError, match=message):
        compile_query(source)


# ---------------------------------------------------------------------------
# Measures have types, and the type decides what is legal
# ---------------------------------------------------------------------------
def test_sum_of_a_ratio_is_a_type_error_that_explains_itself():
    with pytest.raises(DSLValidationError) as caught:
        compile_query("SELECT SUM(utilisation)")
    message = str(caught.value)
    assert "type error" in message
    assert "ratio measure" in message
    assert "numerator and denominator" in message
    assert "without SUM" in message


def test_aggregating_a_semi_additive_measure_names_the_time_rule():
    with pytest.raises(DSLValidationError, match="never across time"):
        compile_query("SELECT SUM(headcount)")


def test_sum_of_an_additive_measure_is_accepted_as_the_identity():
    compiled = compile_query(f"SELECT SUM(services_revenue) BY practice {Q2}")
    assert "AS services_revenue" in compiled.sql


def test_other_aggregates_are_refused_because_the_registry_decides():
    with pytest.raises(DSLValidationError, match="fixed by the registry"):
        compile_query(f"SELECT AVG(services_revenue) {Q2}")


def test_semi_additive_measure_cannot_span_time_without_closing_rule():
    with pytest.raises(DSLValidationError, match="closing period"):
        compile_query("SELECT headcount FOR PERIOD 2026-Q1..2026-Q2")


# ---------------------------------------------------------------------------
# Names, literals, scope, cost
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "source, message",
    [
        ("SELECT services_revenue BY not_a_dimension", "unknown dimension"),
        ("SELECT not_a_measure", "unknown measure"),
        ("SELECT services_revenue WHERE not_a_field = 'x'", "unknown predicate field"),
        (
            "SELECT services_revenue COMPARE PLAN pv='PV-1', scenario='unknown' TO ACTUAL",
            "unknown scenario",
        ),
    ],
)
def test_schema_validation_errors(source, message):
    with pytest.raises(DSLValidationError, match=message):
        compile_query(source)


def test_values_are_not_interpolated_into_sql():
    compiled = compile_query("SELECT services_revenue WHERE customer = 'Robert''; DROP TABLE users' LIMIT 100")
    assert "DROP TABLE" not in compiled.sql
    assert compiled.params["p0"] == "Robert'; DROP TABLE users"


def test_or_predicate_is_preserved():
    compiled = compile_query(
        f"SELECT services_revenue WHERE services_revenue > 10 OR gross_margin > 5 {Q2} LIMIT 100"
    )
    assert " OR " in compiled.sql
    assert "HAVING" in compiled.sql


def test_or_between_dimension_filters_is_preserved():
    # Found in review: dimension filters were joined with AND regardless of
    # the connector, so "Cloud OR Data" asked for rows that are both.
    compiled = compile_query(f"SELECT services_revenue WHERE practice = 'Cloud' OR practice = 'Data' {Q2}")
    assert "(practice = {p0:String} OR practice = {p1:String}) AND period_month" in compiled.sql


def test_or_across_a_dimension_and_a_measure_is_refused():
    with pytest.raises(DSLValidationError, match="OR cannot join a dimension filter and a measure filter"):
        compile_query(f"SELECT services_revenue WHERE practice = 'Cloud' OR services_revenue > 10 {Q2}")
    # AND across the two is still fine: WHERE for one, HAVING for the other.
    compiled = compile_query(f"SELECT services_revenue BY practice WHERE practice = 'Cloud' AND services_revenue > 10 {Q2}")
    assert "HAVING" in compiled.sql


def test_security_scope_is_injected_inside_the_read():
    compiled = compile_query(
        f"SELECT services_revenue BY company {Q2}",
        security_context=SecurityContext(frozenset({"C001", "C002"})),
    )
    assert "company IN ({p2:String}, {p3:String})" in compiled.sql
    assert {compiled.params["p2"], compiled.params["p3"]} == {"C001", "C002"}


def test_empty_scope_reads_nothing():
    compiled = compile_query(f"SELECT services_revenue {Q2}", security_context=SecurityContext(frozenset()))
    assert "0 = 1" in compiled.sql


def test_query_budget_is_enforced():
    with pytest.raises(DSLValidationError, match="row budget"):
        compile_query(
            "SELECT services_revenue",
            security_context=SecurityContext(max_estimated_rows=10),
        )


def test_unavailable_seed_measure_is_rejected():
    with pytest.raises(DSLValidationError, match="not available"):
        compile_query("SELECT open_pipeline")


def test_reverse_period_range_is_rejected():
    with pytest.raises(DSLValidationError, match="period range"):
        compile_query("SELECT services_revenue FOR PERIOD 2026-Q3..2026-Q2")


def test_alias_is_checked_before_sql_generation():
    compiled = compile_query(f"SELECT services_revenue AS safe_alias {Q2}")
    assert "AS safe_alias" in compiled.sql


def test_security_context_rejects_invalid_budget():
    with pytest.raises(ValueError, match="max_estimated_rows"):
        SecurityContext(max_estimated_rows=-1)


# ---------------------------------------------------------------------------
# Bridge: matched lines for the bridge module
# ---------------------------------------------------------------------------
def test_bridge_compiles_matched_lines_with_both_fx_rates():
    compiled = compile_query(
        f"SELECT services_revenue BY practice, grade WHERE geo_country = 'PL' {Q2} "
        "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
    )
    assert compiled.bridge
    for column in ("plan_quantity", "plan_unit_price", "actual_quantity", "actual_unit_price", "plan_fx", "actual_fx", "account_type"):
        assert column in compiled.sql
    assert "a.dim_signature_hash = p.dim_signature_hash" in compiled.sql
    assert "dim_fx_plan" in compiled.sql and "dim_fx_actual" in compiled.sql
    # Only the measure's accounts are matched, and the actual side is vintage-aware.
    assert set(compiled.params.values()) >= {"41000", "41010", "41020", "41400", "PL", "PV-2026-0001", "base"}
    assert "LIMIT 1 BY company, period_month, account, dim_signature_hash" in compiled.sql


def test_bridge_eliminates_intercompany_pairs():
    compiled = compile_query(
        f"SELECT services_revenue BY practice {Q2} COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
    )
    placeholder = next(f"{{{name}:String}}" for name, value in compiled.params.items() if value == "Yes")
    assert f"intercompany_flag != {placeholder}" in compiled.sql


def test_bridge_keeps_or_between_dimension_filters():
    compiled = compile_query(
        f"SELECT services_revenue BY practice WHERE geo_country = 'PL' OR geo_country = 'DE' {Q2} "
        "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
    )
    assert re.search(r"\(geo_country = \{p\d+:String\} OR geo_country = \{p\d+:String\}\)", compiled.sql)


def test_bridge_refuses_limit():
    # A bridge over the first N lines still ties, and explains the wrong gap.
    with pytest.raises(DSLValidationError, match="LIMIT does not apply to BRIDGE"):
        compile_query(f"SELECT services_revenue {Q2} COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE LIMIT 10")


def test_bridge_refuses_a_measure_that_is_not_over_accounts():
    with pytest.raises(DSLValidationError, match="cannot be bridged"):
        compile_query(f"SELECT utilisation {Q2} COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE")


def test_bridge_as_of_is_allowed_and_carried():
    compiled = compile_query(
        f"SELECT services_revenue {Q2} AS OF '2026-07-05T18:00:00' COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
    )
    assert compiled.vintage == "2026-07-05T18:00:00"


def test_an_offset_aware_vintage_binds_as_clickhouse_utc():
    # A close read back from Postgres is offset-aware. ClickHouse refuses the
    # offset, so the drill-through 500s unless it is normalized here.
    from fpa_project.dsl.compiler import clickhouse_datetime

    assert clickhouse_datetime("2026-08-12T09:30:00+00:00") == "2026-08-12T09:30:00"
    # A non-UTC offset is converted, not merely stripped: the instant is the same.
    assert clickhouse_datetime("2026-08-12T11:30:00+02:00") == "2026-08-12T09:30:00"
    # Anything already acceptable is passed through untouched.
    assert clickhouse_datetime("2026-08-12 09:30:00") == "2026-08-12 09:30:00"
    assert clickhouse_datetime("2026-08-12T09:30:00") == "2026-08-12T09:30:00"

    # The DSL parser refuses an offset, so the only way one reaches the binder
    # is the citation drill-through, which pins the vintage from a stored close.
    from fpa_project.dsl.compiler import compile_citation_rows

    keys = [{"company_code": "RTPL1", "period_month": "2026-04-01",
             "account_code": "41000", "dim_signature_hash": "109ff15dc52db502"}]
    compiled = compile_citation_rows(keys, "2026-08-12T09:30:00+00:00", SecurityContext(frozenset({"RTPL1"})))
    assert "2026-08-12T09:30:00" in compiled.params.values()
    assert not any("+00:00" in str(value) for value in compiled.params.values())
