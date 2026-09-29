"""Migration 016 against the real governance store, and the seed import behind it.

Branches are driver overrides, not copied lines; every trace names its driver,
formula and inputs; a locked plan can be superseded and nothing else; a failed
covenant check cannot be overridden by hand; the controller who started a
re-forecast cannot also approve it. Each is tried the way someone with a
database URL would try it. Writes run inside a rolled-back transaction or on a
throwaway version.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from db.config import database_url

pytestmark = pytest.mark.integration
try:
    _engine = create_engine(database_url(), connect_args={"connect_timeout": 2})
    with _engine.connect() as probe:
        head = probe.execute(text("SELECT version_num FROM fpa_governance.alembic_version")).scalar()
except Exception:  # noqa: BLE001
    pytest.skip("needs Postgres: run `make docker-local-run-d`", allow_module_level=True)
if head is None or head < "016":
    pytest.skip("needs migration 016", allow_module_level=True)

from fpa_project.governance import Refused, create_plan_version, describe, set_covenant, transition  # noqa: E402
from fpa_project.identities import CFO, CONTROLLER, PLANNER, SERVICE  # noqa: E402

TRACE = {"driver": "utilisation", "formula": "quantity = baseline_quantity x 0.96", "inputs": {"baseline": {"quantity": 1}}}


@pytest.fixture
def conn():
    connection = _engine.connect()
    tx = connection.begin()
    yield connection
    tx.rollback()
    connection.close()


@pytest.fixture
def version():
    code = f"PV-T-{uuid.uuid4().hex[:6]}"
    create_plan_version(code, "FPA-2026", 2026, PLANNER)
    yield code
    with _engine.begin() as c:
        # Test clean-up only: the guards are switched off for this delete and back on in the same transaction.
        c.execute(text("ALTER TABLE fpa_governance.plan_version DISABLE TRIGGER plan_version_lock_guard"))
        c.execute(text("ALTER TABLE fpa_governance.plan_version_line DISABLE TRIGGER plan_line_lock_guard"))
        c.execute(text("DELETE FROM fpa_governance.plan_approval WHERE plan_version_id IN ("
                       "SELECT plan_version_id FROM fpa_governance.plan_version WHERE plan_version_code LIKE :c)"), {"c": code + "%"})
        c.execute(text("ALTER TABLE fpa_governance.covenant_check DISABLE TRIGGER covenant_check_no_update"))
        c.execute(text("DELETE FROM fpa_governance.covenant_check WHERE plan_version_id IN ("
                       "SELECT plan_version_id FROM fpa_governance.plan_version WHERE plan_version_code LIKE :c)"), {"c": code + "%"})
        c.execute(text("ALTER TABLE fpa_governance.covenant_check ENABLE TRIGGER covenant_check_no_update"))
        c.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code LIKE :c"), {"c": code + "%"})
        c.execute(text("ALTER TABLE fpa_governance.plan_version_line ENABLE TRIGGER plan_line_lock_guard"))
        c.execute(text("ALTER TABLE fpa_governance.plan_version ENABLE TRIGGER plan_version_lock_guard"))


def refused(conn, sql, params=None, match=""):
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        conn.execute(text(sql), params or {})
    savepoint.rollback()
    assert match in str(caught.value.orig), caught.value.orig


def insert_line(conn, pv_id, scenario="base", trace=TRACE, signature="0123456789abcdef"):
    conn.execute(text(
        "INSERT INTO fpa_governance.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
        "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, driver_derivation_trace) "
        "VALUES (CAST(:pv AS uuid), :scenario, 'RTPL1', '2026-04-01', '41000', :sig, 1, 2, 2, 'PLN', CAST(:trace AS jsonb))"),
        {"pv": pv_id, "scenario": scenario, "trace": json.dumps(trace), "sig": signature})


def lock(code):
    transition(code, "IN_REVIEW", PLANNER)
    set_covenant(code, True, "reviewed", CONTROLLER)
    transition(code, "APPROVED", CONTROLLER)
    transition(code, "LOCKED", CFO)


def pass_covenants_as_the_system(code):
    """What evaluate_covenants writes on a pass: a passing check, then covenant_ok, as the service."""
    target = describe(code)["plan_version_id"]
    with _engine.begin() as c:
        c.execute(text("SELECT set_config('fpa.actor', :s, true)"), {"s": SERVICE})
        c.execute(text(
            "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, plan_version_id, revision, rule_code, "
            " scenario_code, scope, metric, measure, measured_value, comparator, threshold, passed) "
            "VALUES (:run, 'wf-t', CAST(:pv AS uuid), 2, 'GM_PCT_FLOOR', 'base', 'REQUEST', 'gross_margin_pct', 'LEVEL', "
            "        33, '>=', 30, true)"), {"run": f"run-{code}", "pv": target})
        c.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true, covenant_note = 'automated covenant check passed' "
                       "WHERE plan_version_id = CAST(:pv AS uuid)"), {"pv": target})


def lock_through_the_run_gates(code):
    """A re-forecast is submitted, checked, approved and locked by its run's activities, never by plain transitions."""
    from fpa_project.recompute.activities import open_review, record_approval, record_lock, record_submission

    target = describe(code)["plan_version_id"]
    record_submission(target, PLANNER, "", "test-supersede")
    pass_covenants_as_the_system(code)
    open_review(target, "test-supersede")
    record_approval(target, CONTROLLER, "", "test-supersede")
    record_lock(target, CFO, "", "test-supersede")


# -- 1. branches, not copies -----------------------------------------------
def test_the_seeded_plan_holds_base_lines_only_each_with_a_trace():
    with _engine.connect() as c:
        rows = c.execute(text(
            "SELECT l.scenario_code, count(*), count(*) FILTER (WHERE l.driver_derivation_trace ?& array['driver','formula','inputs']) "
            "FROM fpa_governance.plan_version_line l JOIN fpa_governance.plan_version v USING (plan_version_id) "
            "WHERE v.plan_version_code = 'PV-2026-0001' GROUP BY 1")).all()
        overrides = c.execute(text(
            "SELECT s.scenario_code, count(*) FROM fpa_governance.scenario_driver_override o "
            "JOIN fpa_governance.scenario_set s USING (scenario_set_id) GROUP BY 1")).all()
    assert [(scenario, count == traced) for scenario, count, traced in rows] == [("base", True)]
    assert rows[0][1] > 70_000
    assert dict(overrides) == {"stretch": 2, "downside": 2}


def test_a_stretch_line_is_refused_because_branches_are_overrides(version, conn):
    pv = describe(version)["plan_version_id"]
    insert_line(conn, pv)  # base is fine
    refused(conn, "INSERT INTO fpa_governance.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
            "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, driver_derivation_trace) "
            "VALUES (CAST(:pv AS uuid), 'stretch', 'RTPL1', '2026-04-01', '41000', 'fedcba9876543210', 1, 2, 2, 'PLN', "
            "CAST(:trace AS jsonb))", {"pv": pv, "trace": json.dumps(TRACE)}, match="base scenario")


# -- 2. a trace that explains ----------------------------------------------
@pytest.mark.parametrize("trace", [{"x": 1}, {"driver": "u", "formula": "f"}, {"driver": "", "formula": "f", "inputs": {}},
                                   {"driver": "u", "formula": "f", "inputs": "not an object"}])
def test_a_trace_without_driver_formula_and_inputs_is_refused(version, conn, trace):
    pv = describe(version)["plan_version_id"]
    refused(conn, "INSERT INTO fpa_governance.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
            "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, driver_derivation_trace) "
            "VALUES (CAST(:pv AS uuid), 'base', 'RTPL1', '2026-04-01', '41000', '0123456789abcdef', 1, 2, 2, 'PLN', "
            "CAST(:trace AS jsonb))", {"pv": pv, "trace": json.dumps(trace)}, match="derivation_trace_explains")


# -- 3. LOCKED -> SUPERSEDED, and nothing else -------------------------------
def test_a_locked_plan_is_superseded_only_by_a_locked_successor(version):
    lock(version)
    with pytest.raises(Refused, match="no?ne|superseded only by a locked successor"):
        transition(version, "SUPERSEDED", PLANNER)
    source_id = describe(version)["plan_version_id"]
    successor = f"{version}-R2"
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO fpa_governance.plan_version (plan_version_code, model_id, plan_year, requested_by, "
            " supersedes_plan_version_id, revision) SELECT :code, model_id, 2026, '44cb9d21b3704eb6b9c7c14cfb2d1d80', plan_version_id, 2 "
            "FROM fpa_governance.plan_version WHERE plan_version_code = :src"), {"code": successor, "src": version})
    with pytest.raises(Refused, match="through its run"):
        transition(successor, "IN_REVIEW", PLANNER)
    lock_through_the_run_gates(successor)
    # With a locked successor the move is allowed, but only the move.
    with _engine.begin() as c, pytest.raises(DBAPIError, match="changes its state and nothing else"):
        c.execute(text("UPDATE fpa_governance.plan_version SET state = 'SUPERSEDED', covenant_note = 'x' "
                       "WHERE plan_version_code = :c"), {"c": version})
    transition(version, "SUPERSEDED", PLANNER)
    after = describe(version)
    assert after["state"] == "SUPERSEDED" and after["plan_version_id"] == source_id
    assert [v["plan_version_code"] for v in after["superseded_by"]] == [successor]
    # Superseded is history: as frozen as locked.
    with _engine.begin() as c, pytest.raises(DBAPIError, match="superseded; it is history"):
        c.execute(text("UPDATE fpa_governance.plan_version SET covenant_note = 'x' WHERE plan_version_code = :c"), {"c": version})
    with _engine.begin() as c, pytest.raises(DBAPIError, match="superseded; it is history"):
        insert_line(c, source_id)


# -- 4. no hand-written pass over a failed check ------------------------------
def test_a_controller_cannot_record_a_pass_over_a_failed_automated_check(version, conn):
    pv = describe(version)["plan_version_id"]
    conn.execute(text(
        "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, plan_version_id, revision, rule_code, scenario_code, "
        " scope, metric, measure, measured_value, comparator, threshold, passed) "
        "VALUES ('run-t', 'wf-t', CAST(:pv AS uuid), 2, 'GM_PCT_FLOOR', 'base', 'REQUEST', 'gross_margin_pct', 'LEVEL', "
        "        23, '>=', 30, false)"), {"pv": pv})
    conn.execute(text("SELECT set_config('fpa.actor', 'e2a112d82c994c3ea08f65f1b78b4056', true)"))
    refused(conn, "UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_id = CAST(:pv AS uuid)",
            {"pv": pv}, match="automated covenant check failed")


# -- 5. two gates, two people -------------------------------------------------
def test_no_controller_decides_a_proposed_request(conn):
    """Migration 022: the planner who asked confirms the draft; a controller's one decision is the plan's."""
    conn.execute(text("SELECT set_config('fpa.actor', '44cb9d21b3704eb6b9c7c14cfb2d1d80', true)"))
    request = conn.execute(text(
        "INSERT INTO fpa_governance.reforecast_request (source_plan_version_id, driver_code, from_value, to_value, "
        " companies, period_months, question, requested_by) "
        "SELECT plan_version_id, 'utilisation', 0.75, 0.72, ARRAY['RTPL1'], ARRAY[DATE '2026-07-01'], 'q', '44cb9d21b3704eb6b9c7c14cfb2d1d80' "
        "FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001' RETURNING request_id::text")).scalar()
    # fd40d13f7e8f485ea53c6a389a939f99 holds the controller role, and may no longer start or reject a request.
    conn.execute(text("SELECT set_config('fpa.actor', 'fd40d13f7e8f485ea53c6a389a939f99', true)"))
    for state in ("RUNNING", "CANCELLED"):
        refused(conn, "UPDATE fpa_governance.reforecast_request SET state = :s WHERE request_id = CAST(:r AS uuid)",
                {"s": state, "r": request}, match="only the planner who asked")
    refused(conn, "UPDATE fpa_governance.reforecast_request SET state = 'CONTROLLER_REJECTED', "
            "controller_decided_by = 'fd40d13f7e8f485ea53c6a389a939f99' WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, match="confirmed (RUNNING) or withdrawn (CANCELLED)")


# -- 6. the system owns a re-forecast's covenant verdict (migration 023) -------
def test_no_role_records_a_re_forecasts_covenant_by_hand(version):
    successor = f"{version}-R2"
    with _engine.begin() as c:
        c.execute(text(
            "INSERT INTO fpa_governance.plan_version (plan_version_code, model_id, plan_year, requested_by, "
            " supersedes_plan_version_id, revision) SELECT :code, model_id, 2026, '44cb9d21b3704eb6b9c7c14cfb2d1d80', plan_version_id, 2 "
            "FROM fpa_governance.plan_version WHERE plan_version_code = :src"), {"code": successor, "src": version})
    for actor in (CONTROLLER, CFO):
        with pytest.raises(Refused, match="checked by the system"):
            set_covenant(successor, True, "looks fine to me", actor)
    # The service may, and only once a passing automated check is on record
    with _engine.begin() as c, pytest.raises(DBAPIError, match="controller"):
        c.execute(text("SELECT set_config('fpa.actor', :s, true)"), {"s": SERVICE})
        c.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_code = :c"), {"c": successor})
    pass_covenants_as_the_system(successor)
    assert describe(successor)["covenant_ok"] is True
    # ...and the original plan keeps its controller review
    set_covenant(version, True, "reviewed", CONTROLLER)
