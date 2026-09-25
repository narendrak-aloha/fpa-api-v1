"""The database rules behind question-driven re-forecasts (migration 015), and the desk.

Every write runs inside a transaction that is rolled back, so nothing is left
behind: requests and covenant checks are otherwise retained for good.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from db.config import database_url
from fpa_project.governance import Principal
from fpa_project.reforecast_requests import ReforecastDesk
from fpa_project.identities import AGENT, CONTROLLER, PLANNER, SERVICE

pytestmark = pytest.mark.integration
try:
    _engine = create_engine(database_url(), connect_args={"connect_timeout": 2})
    with _engine.connect() as probe:
        probe.execute(text("SELECT 1 FROM fpa_governance.reforecast_request LIMIT 1"))
        state = probe.execute(text(
            "SELECT state FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001'")).scalar()
except Exception:
    pytest.skip("Postgres with migration 015 required", allow_module_level=True)
if state not in ("APPROVED", "LOCKED", "SUPERSEDED"):
    pytest.skip("PV-2026-0001 must be APPROVED, LOCKED or SUPERSEDED", allow_module_level=True)

ALL = frozenset(f"RT{c}" for c in (
    "AE1", "AU1", "AU2", "CA1", "CA2", "DE1", "DE2", "IN1", "IN2", "IN3", "PL1", "PL2", "PL3",
    "SG1", "SG2", "UK1", "UK2", "US1", "US2", "US3"))
PLANNER = Principal(PLANNER, "Planner", frozenset({"planner", "analyst"}), ALL, True)
H2 = ["2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"]


@pytest.fixture
def conn():
    connection = _engine.connect()
    tx = connection.begin()
    yield connection
    tx.rollback()
    connection.close()


def as_actor(conn, actor):
    conn.execute(text("SELECT set_config('fpa.actor', :a, true)"), {"a": getattr(actor, "user_id", actor)})


def refused(conn, sql, params=None, match=""):
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        conn.execute(text(sql), params or {})
    savepoint.rollback()
    assert match in str(caught.value.orig), caught.value.orig


def insert_request(conn, actor=PLANNER):
    actor = getattr(actor, "user_id", actor)
    as_actor(conn, actor)
    return conn.execute(text(
        "INSERT INTO fpa_governance.reforecast_request "
        " (source_plan_version_id, driver_code, from_value, to_value, companies, period_months, question, requested_by) "
        "SELECT plan_version_id, 'utilisation', 0.74, 0.72, ARRAY['RTPL1'], ARRAY[DATE '2026-07-01'], 'q', :who "
        "FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001' RETURNING request_id::text"),
        {"who": actor}).scalar()


# -- the desk -----------------------------------------------------------------
def test_the_desk_turns_words_into_a_scoped_draft():
    draft = ReforecastDesk(PLANNER).resolve("utilisation", 0.60, country="Poland", period="2026-H2")
    assert draft.companies == ["RTPL1", "RTPL2", "RTPL3"] and draft.months == H2
    assert draft.plan_version_code == "PV-2026-0001" and draft.to_value == 0.60
    # Not pinned to the seeded 0.75: every published re-forecast moves what the
    # plan applies to this slice, and the desk is meant to follow it.
    assert 0 < draft.from_value <= 1


def test_the_desk_resolves_a_change_against_the_current_value():
    """"increase by 5%" on a ratio driver is five points, worked out by the desk."""
    absolute = ReforecastDesk(PLANNER).resolve("utilisation", 0.60, country="Poland", period="2026-H2")
    moved = ReforecastDesk(PLANNER).resolve("utilisation", country="Poland", period="2026-H2", by_amount=0.05)
    assert moved.from_value == absolute.from_value
    assert moved.to_value == round(absolute.from_value + 0.05, 10)


def test_the_desk_scales_the_current_value_for_a_relative_change():
    scaled = ReforecastDesk(PLANNER).resolve("utilisation", country="Poland", period="2026-H2", by_percent=5)
    assert scaled.to_value == round(scaled.from_value * 1.05, 10)


@pytest.mark.parametrize("kwargs, message", [
    ({}, "exactly one of to_value"),
    ({"to_value": 0.6, "by_amount": 0.05}, "exactly one of to_value"),
    ({"by_amount": 5}, "percentage points"),
    ({"by_amount": -0.9}, "not a value it can hold"),
])
def test_the_desk_refuses_an_ambiguous_or_impossible_change(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ReforecastDesk(PLANNER).resolve("utilisation", country="PL", period="2026-H2", **kwargs)


@pytest.mark.parametrize("args, message", [
    (("heads", 98), "computed"),
    (("nonsense", 1), "not an active driver"),
    (("utilisation", 74, "PL"), "pass 0.74 for 74%"),
    (("utilisation", 0.6, "PL", None, "2027-H1"), "the plan covers 2026 only"),
    (("utilisation", 0.6, "Atlantis"), "no company in"),
    (("utilisation", 0.6, "PL", None, "2026-H9"), "not a FinOpsExpr period"),
])
def test_the_desk_refuses_what_it_cannot_do_and_says_why(args, message):
    with pytest.raises(ValueError, match=message):
        ReforecastDesk(PLANNER).resolve(*args)


def test_the_desk_refuses_companies_outside_the_callers_scope():
    poland_only = Principal(PLANNER, "Planner", frozenset({"planner"}), frozenset({"RTPL1"}), True)
    with pytest.raises(ValueError, match="outside your entity scope"):
        ReforecastDesk(poland_only).resolve("utilisation", 0.6, country="DE")


def test_the_desk_refuses_a_successor_as_the_source():
    with _engine.connect() as c:
        successor = c.execute(text(
            "SELECT plan_version_code FROM fpa_governance.plan_version WHERE supersedes_plan_version_id IS NOT NULL "
            "AND state IN ('APPROVED','LOCKED','SUPERSEDED') LIMIT 1")).scalar()
    if not successor:
        pytest.skip("no locked successor to try")
    with pytest.raises(ValueError, match="itself a re-forecast"):
        ReforecastDesk(PLANNER).resolve("utilisation", 0.6, plan_version_code=successor)


# -- the request record -------------------------------------------------------
def test_only_a_human_planner_as_themselves_creates_a_request(conn):
    assert insert_request(conn)
    for actor in (CONTROLLER, AGENT):
        as_actor(conn, actor)
        refused(conn, "INSERT INTO fpa_governance.reforecast_request (source_plan_version_id, driver_code, from_value, "
                "to_value, question, requested_by) SELECT plan_version_id, 'utilisation', 0.74, 0.72, 'q', :a "
                "FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001'",
                {"a": actor}, "only a human planner")


def test_only_the_planner_who_asked_confirms_or_withdraws_and_only_once(conn):
    request = insert_request(conn)
    as_actor(conn, CONTROLLER)
    refused(conn, "UPDATE fpa_governance.reforecast_request SET state = 'RUNNING' WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "only the planner who asked")
    as_actor(conn, PLANNER)
    refused(conn, "UPDATE fpa_governance.reforecast_request SET state = 'CONTROLLER_REJECTED' WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "confirmed (RUNNING) or withdrawn (CANCELLED)")
    conn.execute(text("UPDATE fpa_governance.reforecast_request SET state = 'CANCELLED' WHERE request_id = CAST(:r AS uuid)"),
                 {"r": request})
    # Closed: no reopening, no second decision, no edit of what was asked
    refused(conn, "UPDATE fpa_governance.reforecast_request SET state = 'PROPOSED' WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "closed")
    refused(conn, "UPDATE fpa_governance.reforecast_request SET to_value = 0.5 WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "immutable")
    refused(conn, "DELETE FROM fpa_governance.reforecast_request WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "retained")


def test_after_confirmation_the_request_only_mirrors_its_run(conn):
    request = insert_request(conn)
    as_actor(conn, PLANNER)
    conn.execute(text("UPDATE fpa_governance.reforecast_request SET state = 'RUNNING' WHERE request_id = CAST(:r AS uuid)"),
                 {"r": request})
    # Nobody can declare it published: the run says otherwise
    refused(conn, "UPDATE fpa_governance.reforecast_request SET state = 'PUBLISHED' WHERE request_id = CAST(:r AS uuid)",
            {"r": request}, "request state follows its run")


def _confirmed_with_run(conn, run_id):
    """A confirmed request bound to a run row, and the run's (throwaway) target version."""
    request = insert_request(conn)
    as_actor(conn, PLANNER)
    conn.execute(text("UPDATE fpa_governance.reforecast_request SET state = 'RUNNING', run_id = :run "
                      "WHERE request_id = CAST(:r AS uuid)"), {"r": request, "run": run_id})
    source = conn.execute(text("SELECT plan_version_id FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001'")).scalar()
    conn.execute(text("INSERT INTO fpa_governance.recompute_run (run_id, workflow_id, plan_version_id, requested_by, shocks) "
                      "VALUES (:run, 'wf', :pv, '44cb9d21b3704eb6b9c7c14cfb2d1d80', '[]')"), {"run": run_id, "pv": source})
    as_actor(conn, CONTROLLER)
    target = conn.execute(text(
        "INSERT INTO fpa_governance.plan_version (plan_version_code, model_id, plan_year, requested_by) "
        "SELECT :code, model_id, 2026, '44cb9d21b3704eb6b9c7c14cfb2d1d80' FROM fpa_governance.plan_version "
        "WHERE plan_version_code = 'PV-2026-0001' RETURNING plan_version_id"), {"code": f"PV-T-{run_id}"}).scalar()
    as_actor(conn, SERVICE)
    conn.execute(text(
        "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, request_id, plan_version_id, revision, rule_code, "
        " scenario_code, scope, metric, measure, measured_value, comparator, threshold, passed) "
        "VALUES (:run, 'wf', CAST(:r AS uuid), :pv, 99, 'GM_PCT_FLOOR', 'base', 'REQUEST', 'gross_margin_pct', "
        " 'LEVEL', 33, '>=', 30, true)"), {"run": run_id, "r": request, "pv": target})
    return request, target


def _request_state(conn, request):
    return conn.execute(text("SELECT state FROM fpa_governance.reforecast_request WHERE request_id = CAST(:r AS uuid)"),
                        {"r": request}).scalar()


def test_the_request_follows_the_three_gates_of_its_run(conn):
    request, _ = _confirmed_with_run(conn, "test-run-022a")
    for run_state, request_state in (("AWAITING_SUBMISSION", "AWAITING_SUBMISSION"),
                                     ("AWAITING_APPROVAL", "AWAITING_CONTROLLER"),
                                     ("AWAITING_LOCK", "AWAITING_CFO"),
                                     ("PUBLISHING", "PUBLISHING"),
                                     ("COMPLETED", "PUBLISHED")):
        conn.execute(text("UPDATE fpa_governance.recompute_run SET state = :s WHERE run_id = 'test-run-022a'"), {"s": run_state})
        assert _request_state(conn, request) == request_state, run_state


def test_a_rejection_reads_as_the_controllers_before_approval_and_the_cfos_after(conn):
    request, _ = _confirmed_with_run(conn, "test-run-022b")
    conn.execute(text("UPDATE fpa_governance.recompute_run SET state = 'REJECTED' WHERE run_id = 'test-run-022b'"))
    assert _request_state(conn, request) == "CONTROLLER_REJECTED"

    request, target = _confirmed_with_run(conn, "test-run-022c")
    as_actor(conn, CONTROLLER)
    conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true, state = 'APPROVED', "
                      "approved_by = 'e2a112d82c994c3ea08f65f1b78b4056' WHERE plan_version_id = :pv"), {"pv": target})
    conn.execute(text("UPDATE fpa_governance.recompute_run SET state = 'REJECTED' WHERE run_id = 'test-run-022c'"))
    assert _request_state(conn, request) == "CFO_REJECTED"


def test_a_run_drives_its_request_and_a_failed_check_reads_as_covenant_failed(conn):
    request = insert_request(conn)
    as_actor(conn, PLANNER)
    conn.execute(text("UPDATE fpa_governance.reforecast_request SET state = 'RUNNING', "
                      "run_id = 'test-run-015' WHERE request_id = CAST(:r AS uuid)"), {"r": request})
    source = conn.execute(text("SELECT plan_version_id FROM fpa_governance.plan_version WHERE plan_version_code = 'PV-2026-0001'")).scalar()
    conn.execute(text("INSERT INTO fpa_governance.recompute_run (run_id, workflow_id, plan_version_id, requested_by, shocks) "
                      "VALUES ('test-run-015', 'wf', :pv, '44cb9d21b3704eb6b9c7c14cfb2d1d80', '[]')"), {"pv": source})
    as_actor(conn, SERVICE)
    conn.execute(text(
        "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, request_id, plan_version_id, revision, rule_code, "
        " scenario_code, scope, metric, measure, measured_value, comparator, threshold, passed) "
        "VALUES ('test-run-015', 'wf', CAST(:r AS uuid), :pv, 99, 'GM_PCT_FLOOR', 'base', 'REQUEST', 'gross_margin_pct', "
        " 'LEVEL', 26.02, '>=', 30, false)"), {"r": request, "pv": source})
    conn.execute(text("UPDATE fpa_governance.recompute_run SET state = 'REJECTED', detail = 'COVENANT_BREACH' WHERE run_id = 'test-run-015'"))
    assert conn.execute(text("SELECT state FROM fpa_governance.reforecast_request WHERE request_id = CAST(:r AS uuid)"),
                        {"r": request}).scalar() == "COVENANT_FAILED"
    # The record of why is append-only
    refused(conn, "UPDATE fpa_governance.covenant_check SET passed = true WHERE run_id = 'test-run-015'", match="append-only")
    refused(conn, "DELETE FROM fpa_governance.covenant_check WHERE run_id = 'test-run-015'", match="append-only")


def test_the_service_sets_covenant_ok_only_on_a_version_whose_checks_all_passed(conn):
    target = conn.execute(text(
        "SELECT plan_version_id FROM fpa_governance.plan_version WHERE state = 'REJECTED' "
        "AND supersedes_plan_version_id IS NULL LIMIT 1")).scalar()
    as_actor(conn, CONTROLLER)
    target = target or conn.execute(text(
        "INSERT INTO fpa_governance.plan_version (plan_version_code, model_id, plan_year, requested_by) "
        "SELECT 'PV-TEST-015', model_id, 2026, '44cb9d21b3704eb6b9c7c14cfb2d1d80' FROM fpa_governance.plan_version "
        "WHERE plan_version_code = 'PV-2026-0001' RETURNING plan_version_id")).scalar()
    as_actor(conn, SERVICE)
    # No checks yet: refused
    refused(conn, "UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_id = :pv",
            {"pv": target}, "controller")
    conn.execute(text(
        "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, plan_version_id, revision, rule_code, scenario_code, "
        " scope, metric, measure, measured_value, comparator, threshold, passed) VALUES "
        "('t15', 'wf', :pv, 1, 'GM_PCT_FLOOR', 'base', 'REQUEST', 'gross_margin_pct', 'LEVEL', 33, '>=', 30, true)"),
        {"pv": target})
    conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_id = :pv"), {"pv": target})
    # One failing check and the service may not set it any more
    conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = false WHERE plan_version_id = :pv AND false"), {"pv": target})
    conn.execute(text(
        "INSERT INTO fpa_governance.covenant_check (run_id, workflow_id, plan_version_id, revision, rule_code, scenario_code, "
        " scope, metric, measure, measured_value, comparator, threshold, passed) VALUES "
        "('t15', 'wf', :pv, 1, 'REVENUE_DROP_LIMIT', 'base', 'REQUEST', 'services_revenue', 'CHANGE_PCT', -12, '>=', -5, false)"),
        {"pv": target})
    as_actor(conn, CONTROLLER)
    conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = false WHERE plan_version_id = :pv"), {"pv": target})
    as_actor(conn, SERVICE)
    refused(conn, "UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_id = :pv",
            {"pv": target}, "controller")


def test_covenant_rules_are_controller_data(conn):
    as_actor(conn, PLANNER)
    refused(conn, "UPDATE fpa_governance.covenant_rule SET threshold = 0 WHERE rule_code = 'GM_PCT_FLOOR'", match="controller")
    as_actor(conn, CONTROLLER)
    conn.execute(text("UPDATE fpa_governance.covenant_rule SET threshold = 29 WHERE rule_code = 'GM_PCT_FLOOR'"))


def test_a_rejected_re_forecast_successor_is_closed(conn):
    successor = conn.execute(text(
        "SELECT plan_version_id FROM fpa_governance.plan_version WHERE state = 'REJECTED' "
        "AND supersedes_plan_version_id IS NOT NULL LIMIT 1")).scalar()
    if not successor:
        pytest.skip("no rejected successor to try")
    as_actor(conn, PLANNER)
    refused(conn, "UPDATE fpa_governance.plan_version SET state = 'DRAFT' WHERE plan_version_id = :pv",
            {"pv": successor}, "rejected re-forecast is closed")
