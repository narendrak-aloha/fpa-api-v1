"""Accounts and access (migration 017) against the real governance store.

Signup is PENDING and sees nothing; only a superadmin approves and grants,
never for themselves; a superadmin holds no business role and no company
data. Each rule is tried through the service the API calls and, where it
matters, straight at the tables as someone with a database URL would.
"""

from __future__ import annotations

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
if head is None or head < "017":
    pytest.skip("needs migration 017", allow_module_level=True)

from fpa_project import users  # noqa: E402
from fpa_project.governance import Refused, authenticate  # noqa: E402
from fpa_project.identities import ANALYST_PL, CFO, PLANNER, SERVICE, SUPERADMIN  # noqa: E402

PASSWORD = "Fpa!12345"


@pytest.fixture
def newcomer():
    email = f"t.{uuid.uuid4().hex[:8]}@realtech.example"
    account = users.signup(email, "Test Newcomer", PASSWORD)
    yield {**account, "email": email}
    # Accounts are never deleted (the audit log names them), so a test account
    # is closed instead and does not sit in the superadmin's waiting list.
    with _engine.connect() as c:
        status = c.execute(text("SELECT status FROM fpa_governance.app_user WHERE user_id = :u"),
                           {"u": account["user_id"]}).scalar()
    if status == "PENDING":
        users.reject(SUPERADMIN, account["user_id"], "test account")
    elif status == "ACTIVE":
        users.set_enabled(SUPERADMIN, account["user_id"], False, "test account")


@pytest.fixture
def conn():
    connection = _engine.connect()
    tx = connection.begin()
    yield connection
    tx.rollback()
    connection.close()


def refused(conn, sql, params=None, match=""):
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        conn.execute(text(sql), params or {})
    savepoint.rollback()
    assert match in str(caught.value.orig), caught.value.orig


def as_actor(conn, actor):
    conn.execute(text("SELECT set_config('fpa.actor', :a, true)"), {"a": actor})


# -- signup, sign-in, sessions -------------------------------------------------
def test_every_seeded_person_signs_in_with_the_demo_password():
    for email in ("test@superadmin.com", "test@planner.com", "test@controller.com",
                  "test@cfo.com", "test@analyst.com"):
        session = users.login(email, PASSWORD)
        assert session["status"] == "ACTIVE" and session["token"].startswith("fpa_")
    with pytest.raises(Refused, match="do not match"):
        users.login("test@superadmin.com", "wrong")
    with pytest.raises(Refused, match="do not match"):
        users.login("temporal@realtech.example", PASSWORD)  # a service identity has no password


def test_a_signup_is_pending_and_its_token_sees_only_that(newcomer):
    who = authenticate(newcomer["token"], allow_pending=True)
    assert who.status == "PENDING" and who.roles == frozenset() and who.companies == frozenset()
    with pytest.raises(Refused, match="waiting for a superadmin"):
        authenticate(newcomer["token"])
    with pytest.raises(Refused, match="already exists"):
        users.signup(newcomer["email"].upper(), "Again", PASSWORD)


@pytest.mark.parametrize("email, name, password, message", [
    ("not-an-email", "X", PASSWORD, "valid email"),
    ("x@realtech.example", "", PASSWORD, "your name"),
    ("x@realtech.example", "X", "short", "at least 8"),
])
def test_signup_says_what_is_wrong(email, name, password, message):
    with pytest.raises(Refused, match=message):
        users.signup(email, name, password)


def test_approval_grants_access_and_the_same_token_starts_working(newcomer):
    result = users.approve(SUPERADMIN, newcomer["user_id"], ["planner", "analyst"], ["RTPL1", "RTPL2"], "test")
    assert result["status"] == "ACTIVE"
    who = authenticate(newcomer["token"])
    assert who.roles == {"planner", "analyst"} and who.companies == {"RTPL1", "RTPL2"}
    users.set_access(SUPERADMIN, newcomer["user_id"], ["analyst"], ["RTPL1"])
    assert authenticate(newcomer["token"]).roles == {"analyst"}  # effective on the next request


def test_disabling_ends_every_session_and_sign_in(newcomer):
    users.approve(SUPERADMIN, newcomer["user_id"], ["analyst"], ["RTPL1"])
    users.set_enabled(SUPERADMIN, newcomer["user_id"], False, "left")
    with pytest.raises(Refused):
        authenticate(newcomer["token"])
    with pytest.raises(Refused, match="disabled"):
        users.login(newcomer["email"], PASSWORD)
    users.set_enabled(SUPERADMIN, newcomer["user_id"], True)
    assert users.login(newcomer["email"], PASSWORD)["status"] == "ACTIVE"


def test_a_signed_out_token_is_dead(newcomer):
    users.logout(newcomer["token"])
    with pytest.raises(Refused, match="signed-out"):
        authenticate(newcomer["token"], allow_pending=True)


def test_a_declined_account_cannot_sign_in(newcomer):
    users.reject(SUPERADMIN, newcomer["user_id"], "not staff")
    with pytest.raises(Refused, match="declined"):
        users.login(newcomer["email"], PASSWORD)


# -- the superadmin's own limits ----------------------------------------------
@pytest.mark.parametrize("roles, companies, message", [
    (["superadmin", "planner"], [], "grant superadmin on its own"),
    (["superadmin"], ["RTPL1"], "grant superadmin on its own"),
    (["planner"], [], "at least one company"),
    (["service"], ["RTPL1"], "cannot be granted"),
])
def test_what_a_superadmin_cannot_hand_out(newcomer, roles, companies, message):
    with pytest.raises(Refused, match=message):
        users.approve(SUPERADMIN, newcomer["user_id"], roles, companies)


def test_only_a_superadmin_approves_and_never_themselves(newcomer):
    with pytest.raises(Refused, match="only a superadmin"):
        users.approve(CFO, newcomer["user_id"], ["analyst"], ["RTPL1"])
    with pytest.raises(Refused, match="own access"):
        users.set_access(SUPERADMIN, SUPERADMIN, ["planner"], ["RTPL1"])


# -- straight at the tables ----------------------------------------------------
def test_nobody_grants_a_role_from_psql_without_being_a_superadmin(conn):
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('19e78ea2addf472fb1083c080ea9f6a6', 'cfo')", match="only a superadmin")
    as_actor(conn, CFO)
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('19e78ea2addf472fb1083c080ea9f6a6', 'cfo')", match="only a superadmin")
    refused(conn, "INSERT INTO fpa_governance.user_company_scope VALUES ('19e78ea2addf472fb1083c080ea9f6a6', 'RTUS1')", match="only a superadmin")


def test_a_superadmin_holds_no_business_role_and_no_company(conn):
    as_actor(conn, SUPERADMIN)
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('44cb9d21b3704eb6b9c7c14cfb2d1d80', 'superadmin')", match="holds no business role")
    as_actor(conn, SUPERADMIN)
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('3d48cdee84ad4c80a270d2eed04ed65e', 'planner')", match="own access")


def test_machine_roles_stay_with_machines(conn):
    as_actor(conn, SUPERADMIN)
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('19e78ea2addf472fb1083c080ea9f6a6', 'service')", match="never to a person")
    refused(conn, "INSERT INTO fpa_governance.user_role VALUES ('3e91198b720f46579ad74b3aac146ee8', 'planner')", match="only its own role")


def test_a_signup_from_psql_cannot_skip_approval(conn):
    refused(conn, "INSERT INTO fpa_governance.app_user (user_id, display_name, email, status) "
            "VALUES ('u-sneaky', 'Sneaky', 'sneaky@realtech.example', 'ACTIVE')", match="starts PENDING")
    refused(conn, "UPDATE fpa_governance.app_user SET status = 'DISABLED' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'",
            match="only a superadmin")


def test_the_last_superadmin_stays(conn):
    as_actor(conn, CFO)
    refused(conn, "DELETE FROM fpa_governance.user_role WHERE user_id = '3d48cdee84ad4c80a270d2eed04ed65e' AND role_code = 'superadmin'",
            match="only a superadmin")


# -- names (migration 018) ----------------------------------------------------
def test_a_superadmin_renames_anyone_and_it_is_audited(newcomer):
    result = users.rename(SUPERADMIN, newcomer["user_id"], "  Renamed Person  ")
    assert result == {"user_id": newcomer["user_id"], "display_name": "Renamed Person", "changed": True}
    assert authenticate(newcomer["token"], allow_pending=True).display_name == "Renamed Person"
    assert users.rename(SUPERADMIN, newcomer["user_id"], "Renamed Person")["changed"] is False
    with _engine.connect() as c:
        payload = c.execute(text(
            "SELECT payload FROM fpa_governance.audit_event WHERE entity_id = :u AND action = 'RENAMED' "
            "ORDER BY audit_event_id DESC LIMIT 1"), {"u": newcomer["user_id"]}).scalar()
    assert payload == {"from": "Test Newcomer", "to": "Renamed Person"}
    with pytest.raises(Refused, match="enter a name"):
        users.rename(SUPERADMIN, newcomer["user_id"], "   ")
    with pytest.raises(Refused, match="managed in the seed"):
        users.rename(SUPERADMIN, SERVICE, "Workflow")


def test_nobody_else_renames_a_person_from_psql(conn):
    refused(conn, "UPDATE fpa_governance.app_user SET display_name = 'Mallory' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'",
            match="only the account holder or a superadmin")
    as_actor(conn, PLANNER)
    refused(conn, "UPDATE fpa_governance.app_user SET display_name = 'Mallory' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'",
            match="only the account holder or a superadmin")
    as_actor(conn, SUPERADMIN)
    refused(conn, "UPDATE fpa_governance.app_user SET display_name = '  ' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'",
            match="cannot be empty")


# -- email ----------------------------------------------------------------------
def test_a_superadmin_changes_an_email_and_sign_in_follows_it(newcomer):
    users.approve(SUPERADMIN, newcomer["user_id"], ["analyst"], ["RTPL1"])
    new_email = f"moved.{uuid.uuid4().hex[:8]}@realtech.example"
    assert users.change_email(SUPERADMIN, newcomer["user_id"], f"  {new_email}  ")["changed"] is True
    assert users.login(new_email.upper(), PASSWORD)["user_id"] == newcomer["user_id"]
    with pytest.raises(Refused, match="do not match"):
        users.login(newcomer["email"], PASSWORD)
    assert authenticate(newcomer["token"]).email == new_email  # the open session carries on
    with _engine.connect() as c:
        payload = c.execute(text(
            "SELECT payload FROM fpa_governance.audit_event WHERE entity_id = :u AND action = 'EMAIL_CHANGED' "
            "ORDER BY audit_event_id DESC LIMIT 1"), {"u": newcomer["user_id"]}).scalar()
    assert payload == {"from": newcomer["email"].lower(), "to": new_email}


@pytest.mark.parametrize("actor, email, message", [
    (SUPERADMIN, "not-an-email", "valid email"),
    (SUPERADMIN, "TEST@CFO.COM", "already uses that email"),
    (CFO, "x@realtech.example", "only a superadmin"),
])
def test_what_an_email_change_refuses(newcomer, actor, email, message):
    with pytest.raises(Refused, match=message):
        users.change_email(actor, newcomer["user_id"], email)


def test_a_superadmin_changes_their_own_name_and_email():
    """Name and email grant nothing, so a superadmin edits their own (migration 019)."""
    users.rename(SUPERADMIN, SUPERADMIN, "Sam Admin Renamed")
    users.change_email(SUPERADMIN, SUPERADMIN, "sam.renamed@realtech.example")
    try:
        assert users.login("sam.renamed@realtech.example", PASSWORD)["user_id"] == SUPERADMIN
    finally:
        users.change_email(SUPERADMIN, SUPERADMIN, "test@superadmin.com")
        users.rename(SUPERADMIN, SUPERADMIN, "Test Superadmin")
    # Access stays out of reach of oneself
    with pytest.raises(Refused, match="own access"):
        users.set_access(SUPERADMIN, SUPERADMIN, ["planner"], ["RTPL1"])


def test_nobody_changes_an_email_from_psql(conn):
    as_actor(conn, CFO)
    refused(conn, "UPDATE fpa_governance.app_user SET email = 'x@evil.example' WHERE user_id = '44cb9d21b3704eb6b9c7c14cfb2d1d80'",
            match="only a superadmin")
    refused(conn, "UPDATE fpa_governance.app_user SET email = 'x@evil.example' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'",
            match="only a superadmin")
    as_actor(conn, SUPERADMIN)
    refused(conn, "UPDATE fpa_governance.app_user SET email = '  ' WHERE user_id = 'fd40d13f7e8f485ea53c6a389a939f99'", match="cannot be empty")
    # Their own status is still off limits
    refused(conn, "UPDATE fpa_governance.app_user SET status = 'DISABLED' WHERE user_id = '3d48cdee84ad4c80a270d2eed04ed65e'",
            match="cannot change their own account")
