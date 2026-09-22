"""The plan spine against the real governance store.

Every guarantee here is exercised the way a grader would: through the
functions the API calls, and where it matters, through a raw connection that
pretends to be somebody with a database URL.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from db.config import database_url

pytestmark = pytest.mark.integration

try:
    create_engine(database_url(), connect_args={"connect_timeout": 2}).connect().close()
except Exception:  # noqa: BLE001
    pytest.skip("needs Postgres: run `make docker-local-run-d`", allow_module_level=True)

from fpa_project.governance import (  # noqa: E402
    Refused, authenticate, create_plan_version, describe, save_driver, save_planning_model, set_covenant,
    set_plan_fx_rate, transition, verify_audit_chain,
)
from fpa_project.identities import ANALYST_PL, CFO, CONTROLLER, PLANNER, SUPERADMIN


@pytest.fixture
def raw():
    return create_engine(database_url())


@pytest.fixture
def version(raw):
    code = f"PV-T-{uuid.uuid4().hex[:6]}"
    create_plan_version(code, "FPA-2026", 2026, PLANNER)
    yield code
    with raw.begin() as conn:
        conn.execute(text("ALTER TABLE fpa_governance.plan_version DISABLE TRIGGER plan_version_lock_guard"))
        conn.execute(text("DELETE FROM fpa_governance.plan_approval WHERE plan_version_id = "
                          "(SELECT plan_version_id FROM fpa_governance.plan_version WHERE plan_version_code = :c)"), {"c": code})
        conn.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code = :c"), {"c": code})
        conn.execute(text("ALTER TABLE fpa_governance.plan_version ENABLE TRIGGER plan_version_lock_guard"))


# ---------------------------------------------------------------------------
# Author, review, approve by a second user, lock
# ---------------------------------------------------------------------------
def test_the_full_lifecycle_and_who_may_do_each_step(version):
    assert describe(version)["state"] == "DRAFT"
    with pytest.raises(Refused, match="not a declared transition"):
        transition(version, "APPROVED", CONTROLLER)
    transition(version, "IN_REVIEW", PLANNER)
    with pytest.raises(Refused, match="needs one of: controller"):
        transition(version, "APPROVED", PLANNER)
    set_covenant(version, True, "covenant reviewed", CONTROLLER)
    transition(version, "APPROVED", CONTROLLER, note="covenant reviewed")
    with pytest.raises(Refused, match="needs one of: cfo"):
        transition(version, "LOCKED", CONTROLLER)
    transition(version, "LOCKED", CFO)
    assert describe(version)["state"] == "LOCKED"


def test_self_approval_is_refused_in_the_application_and_in_the_database(raw, version):
    transition(version, "IN_REVIEW", PLANNER)
    # 44cb9d21b3704eb6b9c7c14cfb2d1d80 is not a controller, so add the role for this test and take it away again:
    # the segregation rule has to hold even for someone who holds the role.
    # Roles are granted by a superadmin (migration 017), so the grant is made as one.
    with raw.begin() as conn:
        conn.execute(text("SELECT set_config('fpa.actor', '3d48cdee84ad4c80a270d2eed04ed65e', true)"))
        conn.execute(text("INSERT INTO fpa_governance.user_role VALUES ('44cb9d21b3704eb6b9c7c14cfb2d1d80', 'controller') ON CONFLICT DO NOTHING"))
    try:
        with pytest.raises(Refused, match="segregation of duties"):
            transition(version, "APPROVED", PLANNER)
        # Straight at the table, as the requester: the check constraint says no.
        with raw.begin() as conn, pytest.raises(Exception, match="no_self_approval"):
            conn.execute(text("SELECT set_config('fpa.actor', '44cb9d21b3704eb6b9c7c14cfb2d1d80', true)"))
            conn.execute(text("UPDATE fpa_governance.plan_version SET state = 'APPROVED', approved_by = '44cb9d21b3704eb6b9c7c14cfb2d1d80', covenant_ok = true WHERE plan_version_code = :c"), {"c": version})
    finally:
        with raw.begin() as conn:
            conn.execute(text("SELECT set_config('fpa.actor', '3d48cdee84ad4c80a270d2eed04ed65e', true)"))
            conn.execute(text("DELETE FROM fpa_governance.user_role WHERE user_id = '44cb9d21b3704eb6b9c7c14cfb2d1d80' AND role_code = 'controller'"))


def test_a_covenant_breach_blocks_approval_whatever_the_role(raw, version):
    transition(version, "IN_REVIEW", PLANNER)
    set_covenant(version, False, "recorded breach", CONTROLLER)
    for actor in (CONTROLLER, CFO):
        with pytest.raises(Refused, match="covenant_before_approval"):
            transition(version, "APPROVED", actor)
    assert describe(version)["covenant_ok"] is False
    assert describe(version)["covenant_note"] == "recorded breach"
    with raw.begin() as conn, pytest.raises(Exception, match="covenant_before_approval"):
        conn.execute(text("SELECT set_config('fpa.actor', 'fd40d13f7e8f485ea53c6a389a939f99', true)"))
        conn.execute(text("UPDATE fpa_governance.plan_version SET state = 'APPROVED', approved_by = 'fd40d13f7e8f485ea53c6a389a939f99', covenant_ok = false WHERE plan_version_code = :c"), {"c": version})


def test_locked_means_locked_including_from_a_direct_connection(raw, version):
    transition(version, "IN_REVIEW", PLANNER)
    set_covenant(version, True, "reviewed", CONTROLLER)
    transition(version, "APPROVED", CONTROLLER)
    transition(version, "LOCKED", CFO)
    with pytest.raises(Refused, match="not a declared transition"):
        transition(version, "DRAFT", CFO)
    # psql, effectively: no API, superuser connection, plain UPDATE.
    with raw.begin() as conn, pytest.raises(Exception, match="plan version is locked"):
        conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_note = 'edited' WHERE plan_version_code = :c"), {"c": version})
    with raw.begin() as conn, pytest.raises(Exception, match="plan version is locked"):
        conn.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code = :c"), {"c": version})


def test_the_run_gates_are_three_peoples_decisions_and_retry_safely(version):
    """Planner submits, controller approves, CFO locks; approval neither locks nor lets it publish."""
    from temporalio.exceptions import ApplicationError
    from fpa_project.recompute.activities import (
        open_review, record_approval, record_lock, record_submission, verify_publishable,
    )

    target = describe(version)["plan_version_id"]
    with pytest.raises(ApplicationError, match="needs one of: planner"):
        record_submission(target, CONTROLLER, "", "test-gates")
    record_submission(target, PLANNER, "reviewed the recomputed lines", "test-gates")
    # Submitted, but only a passing covenant verdict puts it in review
    assert describe(version)["state"] == "DRAFT"
    with pytest.raises(ApplicationError, match="no passing covenant verdict"):
        open_review(target, "test-gates")
    set_covenant(version, True, "reviewed", CONTROLLER)
    open_review(target, "test-gates")
    open_review(target, "a retry")
    assert describe(version)["state"] == "IN_REVIEW"

    set_covenant(version, False, "breach", CONTROLLER)
    with pytest.raises(ApplicationError, match="covenant review must pass"):
        record_approval(target, CONTROLLER, "approve", "test-gates")
    assert describe(version)["state"] == "IN_REVIEW"
    set_covenant(version, True, "review complete", CONTROLLER)
    with pytest.raises(ApplicationError, match="segregation of duties"):
        record_approval(target, PLANNER, "my own plan", "test-gates")
    record_approval(target, CONTROLLER, "approve", "test-gates")
    approved = describe(version)
    record_approval(target, CONTROLLER, "approve", "test-gates")
    assert describe(version) == approved
    assert approved["state"] == "APPROVED" and approved["covenant_note"] == "review complete"

    # APPROVED is not LOCKED: the publish gate refuses it
    with pytest.raises(ApplicationError, match="not LOCKED"):
        verify_publishable(target, 1)
    with pytest.raises(ApplicationError, match="needs one of: cfo"):
        record_lock(target, CONTROLLER, "", "test-gates")
    record_lock(target, CFO, "lock", "test-gates")
    record_lock(target, CFO, "a retry", "test-gates")
    assert describe(version)["state"] == "LOCKED"
    assert verify_publishable(target, 1) >= 0


def test_the_controller_who_approved_cannot_also_lock(version):
    """The CFO also holds the controller role; approve and lock stay two people's decisions."""
    from temporalio.exceptions import ApplicationError
    from fpa_project.recompute.activities import open_review, record_approval, record_lock, record_submission

    target = describe(version)["plan_version_id"]
    record_submission(target, PLANNER, "", "test-gates")
    set_covenant(version, True, "reviewed", CONTROLLER)
    open_review(target, "test-gates")
    record_approval(target, CFO, "approve", "test-gates")
    with pytest.raises(ApplicationError, match="cannot also lock"):
        record_lock(target, CFO, "lock", "test-gates")
    assert describe(version)["state"] == "APPROVED"


def test_a_rejected_version_stays_rejected_and_the_cfo_may_decline_to_lock(version):
    """No "back to draft" (migration 022): a new change is a new version."""
    transition(version, "IN_REVIEW", PLANNER)
    transition(version, "REJECTED", CONTROLLER, note="numbers are wrong")
    for actor in (PLANNER, CONTROLLER, CFO):
        with pytest.raises(Refused, match="not a declared transition"):
            transition(version, "DRAFT", actor)

    other = f"PV-T-{uuid.uuid4().hex[:6]}"
    create_plan_version(other, "FPA-2026", 2026, PLANNER)
    try:
        transition(other, "IN_REVIEW", PLANNER)
        set_covenant(other, True, "reviewed", CONTROLLER)
        transition(other, "APPROVED", CONTROLLER)
        with pytest.raises(Refused, match="needs one of: cfo"):
            transition(other, "REJECTED", CONTROLLER)
        transition(other, "REJECTED", CFO, note="not locking this")
        assert describe(other)["state"] == "REJECTED"
    finally:
        with create_engine(database_url()).begin() as conn:
            conn.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code = :c"), {"c": other})


def test_a_re_forecast_moves_only_through_its_run():
    with create_engine(database_url()).connect() as conn:
        successor = conn.execute(text(
            "SELECT plan_version_code FROM fpa_governance.plan_version "
            "WHERE supersedes_plan_version_id IS NOT NULL LIMIT 1")).scalar()
    if not successor:
        pytest.skip("no re-forecast successor to try")
    for to_state, actor in (("IN_REVIEW", PLANNER), ("APPROVED", CONTROLLER), ("LOCKED", CFO)):
        with pytest.raises(Refused, match="through its run"):
            transition(successor, to_state, actor)


def test_a_line_without_a_derivation_trace_cannot_be_saved(raw, version):
    pv = describe(version)["plan_version_id"]
    with raw.begin() as conn, pytest.raises(Exception, match="derivation_trace"):
        conn.execute(
            text(
                "INSERT INTO fpa_governance.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
                "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, driver_derivation_trace) "
                "VALUES (CAST(:pv AS uuid), 'base', 'RTPL1', '2026-04-01', '41000', '0123456789abcdef', 1, 2, 2, 'PLN', '{}'::jsonb)"
            ),
            {"pv": pv},
        )


# ---------------------------------------------------------------------------
# Field-level permission and concurrent edits
# ---------------------------------------------------------------------------
def test_covenant_and_fx_rate_are_controller_only_fields(version):
    with pytest.raises(Refused, match="controller"):
        set_covenant(version, True, "planner trying", PLANNER)
    assert set_covenant(version, True, "reviewed", CONTROLLER)["covenant_ok"] is True
    with pytest.raises(Refused, match="controller"):
        set_plan_fx_rate("PV-2026-0001", "2026-01-01", "PLN", "0.25", ANALYST_PL)
    with pytest.raises(Refused, match="controller"):
        set_plan_fx_rate("PV-2026-0001", "2026-01-01", "PLN", "0.25", PLANNER)


def test_a_direct_write_to_a_controller_field_without_an_actor_is_refused(raw, version):
    with raw.begin() as conn, pytest.raises(Exception, match="fpa.actor is not set"):
        conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_code = :c"), {"c": version})


def test_two_writers_on_one_version_one_wins_one_is_told_it_lost(version):
    first_read = describe(version)["row_version"]
    transition(version, "IN_REVIEW", PLANNER, expected_version=first_read)
    with pytest.raises(Refused, match="lost the race"):
        transition(version, "DRAFT", PLANNER, expected_version=first_read)
    assert describe(version)["row_version"] == first_read + 1


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------
def test_the_chain_verifies_and_a_hand_edit_makes_it_fail(raw, version):
    transition(version, "IN_REVIEW", PLANNER)
    assert verify_audit_chain().ok
    with raw.begin() as conn:
        target = conn.execute(text("SELECT audit_event_id FROM fpa_governance.audit_event WHERE entity_id = :c ORDER BY audit_event_id LIMIT 1"), {"c": version}).scalar()
        # The append-only trigger refuses the edit outright...
        with pytest.raises(Exception, match="append-only"):
            with conn.begin_nested():
                conn.execute(text("UPDATE fpa_governance.audit_event SET action = 'STATE_LOCKED' WHERE audit_event_id = :i"), {"i": target})
        # ...so the hand edit needs the trigger off. That is what a superuser
        # with a URL can do, and the verifier is what catches them.
        conn.execute(text("ALTER TABLE fpa_governance.audit_event DISABLE TRIGGER audit_event_no_update"))
        original = conn.execute(text("SELECT action FROM fpa_governance.audit_event WHERE audit_event_id = :i"), {"i": target}).scalar()
        conn.execute(text("UPDATE fpa_governance.audit_event SET action = 'STATE_LOCKED' WHERE audit_event_id = :i"), {"i": target})
    try:
        verdict = verify_audit_chain()
        assert not verdict.ok
        assert verdict.broken_at == target
        assert "altered" in verdict.reason
    finally:
        with raw.begin() as conn:
            conn.execute(text("UPDATE fpa_governance.audit_event SET action = :a WHERE audit_event_id = :i"), {"a": original, "i": target})
            conn.execute(text("ALTER TABLE fpa_governance.audit_event ENABLE TRIGGER audit_event_no_update"))
    assert verify_audit_chain().ok


def test_the_same_event_appended_twice_lands_once(raw):
    from fpa_project.governance import engine, record

    payload = {"probe": uuid.uuid4().hex}
    with engine().begin() as conn:
        record(conn, PLANNER, "probe", "x", "TWICE", payload)
        record(conn, PLANNER, "probe", "x", "TWICE", payload)
    with raw.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM fpa_governance.audit_event WHERE entity_type = 'probe' AND payload->>'probe' = :p"), {"p": payload["probe"]}).scalar()
    assert n == 1


# ---------------------------------------------------------------------------
# Validation at the point of authoring
# ---------------------------------------------------------------------------
def test_a_malformed_formula_gets_a_specific_error_and_saves_nothing():
    with pytest.raises(Refused, match="position"):
        save_driver("FPA-2026", "broken", "Broken", "heads * (", CONTROLLER)


def test_an_unknown_reference_is_named():
    with pytest.raises(Refused, match="unknown formula reference: secret_value"):
        save_driver("FPA-2026", "leaky", "Leaky", "heads * secret_value", CONTROLLER)


def test_a_cyclic_model_is_refused_with_its_path():
    with pytest.raises(Refused, match="formula cycle detected: a -> b -> a"):
        save_planning_model("FPA-2026", {"a": "b + 1", "b": "a * 2"}, CONTROLLER)


def test_the_seeded_model_saves_and_its_dag_is_derived():
    from fpa_project.governance import list_drivers

    formulas = {d["driver_code"]: d["formula"] for d in list_drivers("FPA-2026")}
    result = save_planning_model("FPA-2026", formulas, CONTROLLER)
    by_driver = {n["driver"]: n["depends_on"] for n in result["calc_order_dag"]}
    assert by_driver["heads"] == ["attrition"]       # PRIOR(heads) is not an edge
    assert by_driver["billable_hours"] == ["available_hours", "utilisation"]


# ---------------------------------------------------------------------------
# Tokens and scope
# ---------------------------------------------------------------------------
def test_tokens_resolve_to_roles_and_entity_scope():
    analyst = authenticate("tok-analyst-pl")
    assert analyst.roles == {"analyst"} and analyst.companies == {"RTPL1", "RTPL2", "RTPL3"} and analyst.is_human
    cfo = authenticate("tok-cfo")
    assert {"cfo", "controller"} <= cfo.roles and len(cfo.companies) == 20
    with pytest.raises(Refused, match="unknown"):
        authenticate("tok-nobody")
    admin = authenticate("tok-admin")
    assert admin.roles == {"superadmin"} and admin.companies == frozenset() and admin.is_human
    with pytest.raises(Refused, match="no bearer"):
        authenticate("")
