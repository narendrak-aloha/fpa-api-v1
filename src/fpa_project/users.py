"""Accounts: signup, sign-in, sessions, and the superadmin who grants access.

The rules live in the database (migration 017): a signup is PENDING, only a
superadmin moves a status or grants roles and company scope, never their own,
and a superadmin holds no business role and no company data. This module is
the application half: it hashes passwords, issues and revokes session tokens,
and writes every change as the acting user so the triggers can judge it. Every
change is also appended to the hash-chained audit log.

A session token is shown once, to the person it was issued to, and stored as
its sha256 only -- the same treatment the seeded dev tokens get.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from fpa_project.governance import Refused, _verdict, engine, record, set_actor
from fpa_project.passwords import MIN_LENGTH, hash_password, verify_password

SCHEMA = "fpa_governance"
# What a superadmin may hand out. service and agent are machine identities;
# superadmin is granted alone (the database refuses it alongside anything else).
BUSINESS_ROLES = ("analyst", "planner", "controller", "cfo")
ASSIGNABLE_ROLES = (*BUSINESS_ROLES, "superadmin")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def session_hours() -> int:
    return int(os.getenv("FPA_SESSION_HOURS", "12"))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue_session(conn, user_id: str) -> dict[str, Any]:
    token = "fpa_" + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=session_hours())
    conn.execute(
        text(f"INSERT INTO {SCHEMA}.user_session (token_hash, user_id, expires_at) VALUES (:h, :u, :e)"),
        {"h": token_digest(token), "u": user_id, "e": expires},
    )
    return {"token": token, "expires_at": expires.isoformat()}


# ---------------------------------------------------------------------------
# Signup, sign-in, sign-out
# ---------------------------------------------------------------------------
def signup(email: str, display_name: str, password: str) -> dict[str, Any]:
    """Create a PENDING account and sign it in.

    The token lets the new person see one thing -- that they are waiting for
    a superadmin -- and nothing else: they have no role and no company scope
    until someone approves them, and every other endpoint requires ACTIVE.
    """
    email = (email or "").strip()
    display_name = (display_name or "").strip()
    if not _EMAIL.match(email):
        raise Refused("enter a valid email address")
    if not display_name:
        raise Refused("enter your name")
    if len(password or "") < MIN_LENGTH:
        raise Refused(f"the password needs at least {MIN_LENGTH} characters")
    user_id = uuid.uuid4().hex
    try:
        with engine().begin() as conn:
            if conn.execute(text(f"SELECT 1 FROM {SCHEMA}.app_user WHERE lower(email) = lower(:e)"), {"e": email}).first():
                raise Refused("an account with that email already exists; sign in instead")
            conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.app_user (user_id, display_name, email, is_human, status, password_hash) "
                    "VALUES (:u, :n, :e, true, 'PENDING', :p)"
                ),
                {"u": user_id, "n": display_name, "e": email, "p": hash_password(password)},
            )
            session = _issue_session(conn, user_id)
            record(conn, user_id, "app_user", user_id, "SIGNED_UP", {"email": email.lower()})
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 - a database verdict, translated
        raise Refused(_verdict(exc)) from exc
    return {"user_id": user_id, "status": "PENDING", **session}


def login(email: str, password: str) -> dict[str, Any]:
    """Email and password for a session token. One message for every failure
    of the credentials themselves, so the form does not reveal which emails exist."""
    with engine().begin() as conn:
        row = conn.execute(
            text(f"SELECT user_id, status, password_hash FROM {SCHEMA}.app_user WHERE lower(email) = lower(:e) AND is_human"),
            {"e": (email or "").strip()},
        ).mappings().first()
        if row is None or not verify_password(password or "", row["password_hash"]):
            raise Refused("that email and password do not match an account")
        if row["status"] == "REJECTED":
            raise Refused("this account request was declined; ask a superadmin")
        if row["status"] == "DISABLED":
            raise Refused("this account is disabled; ask a superadmin")
        session = _issue_session(conn, row["user_id"])
        record(conn, row["user_id"], "app_user", row["user_id"], "SIGNED_IN", {"session_expires_at": session["expires_at"]})
    return {"user_id": row["user_id"], "status": row["status"], **session}


def logout(token: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {SCHEMA}.user_session SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL"),
            {"h": token_digest(token)},
        )


# ---------------------------------------------------------------------------
# What a superadmin sees and does
# ---------------------------------------------------------------------------
def catalog() -> dict[str, Any]:
    """The roles a superadmin may grant and the companies they may scope to."""
    with engine().begin() as conn:
        roles = {r[0]: r[1] for r in conn.execute(text(f"SELECT role_code, description FROM {SCHEMA}.role"))}
        companies = [dict(r) for r in conn.execute(text(
            f"SELECT company_code, company_name, country_code FROM {SCHEMA}.dim_company ORDER BY country_code, company_code"
        )).mappings()]
    return {
        "roles": [{"role_code": code, "description": roles.get(code, ""), "exclusive": code == "superadmin"}
                  for code in ASSIGNABLE_ROLES if code in roles],
        "companies": companies,
    }


def directory() -> list[dict[str, str]]:
    """Every account's id and display name, so screens can say "approved by
    Test CFO" rather than show a UUID. Names only: no email, role or scope."""
    with engine().begin() as conn:
        rows = conn.execute(text(f"SELECT user_id, display_name FROM {SCHEMA}.app_user ORDER BY display_name")).mappings()
        return [dict(r) for r in rows]


def list_users() -> list[dict[str, Any]]:
    with engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT u.user_id, u.display_name, u.email, u.status, u.is_human, u.created_at, "
            "       u.status_changed_by, u.status_changed_at, u.status_note, "
            "       coalesce(array_agg(DISTINCT r.role_code) FILTER (WHERE r.role_code IS NOT NULL), '{}') AS roles, "
            "       coalesce(array_agg(DISTINCT s.company_code) FILTER (WHERE s.company_code IS NOT NULL), '{}') AS companies "
            f"FROM {SCHEMA}.app_user u "
            f"LEFT JOIN {SCHEMA}.user_role r USING (user_id) "
            f"LEFT JOIN {SCHEMA}.user_company_scope s USING (user_id) "
            "GROUP BY u.user_id ORDER BY (u.status = 'PENDING') DESC, u.created_at DESC"
        )).mappings().all()
    return [{**dict(r), "roles": sorted(r["roles"]), "companies": sorted(r["companies"])} for r in rows]


def _check_access(roles: list[str], companies: list[str]) -> tuple[list[str], list[str]]:
    roles, companies = sorted(set(roles or [])), sorted(set(companies or []))
    unknown = sorted(set(roles) - set(ASSIGNABLE_ROLES))
    if unknown:
        raise Refused(f"these roles cannot be granted here: {', '.join(unknown)}")
    if "superadmin" in roles and (len(roles) > 1 or companies):
        raise Refused("a superadmin holds no business role and no company access; grant superadmin on its own")
    if roles and "superadmin" not in roles and not companies:
        raise Refused("a business role needs at least one company to work on")
    return roles, companies


def _write_access(conn, user_id: str, roles: list[str], companies: list[str]) -> dict[str, list[str]]:
    """Replace the user's roles and companies with exactly these.

    Removals first: making someone a superadmin means taking their business
    roles and companies away before the grant, which is the order the 017
    trigger insists on.
    """
    current_roles = {r[0] for r in conn.execute(text(f"SELECT role_code FROM {SCHEMA}.user_role WHERE user_id = :u"), {"u": user_id})}
    current_companies = {r[0] for r in conn.execute(
        text(f"SELECT company_code FROM {SCHEMA}.user_company_scope WHERE user_id = :u"), {"u": user_id})}
    for code in sorted(current_roles - set(roles)):
        conn.execute(text(f"DELETE FROM {SCHEMA}.user_role WHERE user_id = :u AND role_code = :r"), {"u": user_id, "r": code})
    for code in sorted(current_companies - set(companies)):
        conn.execute(text(f"DELETE FROM {SCHEMA}.user_company_scope WHERE user_id = :u AND company_code = :c"),
                     {"u": user_id, "c": code})
    for code in sorted(set(companies) - current_companies):
        conn.execute(text(f"INSERT INTO {SCHEMA}.user_company_scope (user_id, company_code) VALUES (:u, :c)"),
                     {"u": user_id, "c": code})
    for code in sorted(set(roles) - current_roles):
        conn.execute(text(f"INSERT INTO {SCHEMA}.user_role (user_id, role_code) VALUES (:u, :r)"), {"u": user_id, "r": code})
    return {"added_roles": sorted(set(roles) - current_roles), "removed_roles": sorted(current_roles - set(roles)),
            "added_companies": sorted(set(companies) - current_companies),
            "removed_companies": sorted(current_companies - set(companies))}


def _account(conn, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        text(f"SELECT user_id, status, is_human FROM {SCHEMA}.app_user WHERE user_id = :u FOR UPDATE"), {"u": user_id},
    ).mappings().first()
    if row is None:
        raise Refused(f"no account {user_id!r}")
    if not row["is_human"]:
        raise Refused("service and agent identities are managed in the seed, not here")
    return dict(row)


def _set_status(conn, actor: str, user_id: str, status: str, note: str) -> None:
    conn.execute(
        text(
            f"UPDATE {SCHEMA}.app_user SET status = :s, status_changed_by = :a, status_changed_at = now(), "
            "status_note = :n WHERE user_id = :u"
        ),
        {"s": status, "a": actor, "n": note or None, "u": user_id},
    )


def _revoke_sessions(conn, user_id: str) -> None:
    conn.execute(text(f"UPDATE {SCHEMA}.user_session SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"),
                 {"u": user_id})


def _run(actor: str, work) -> dict[str, Any]:
    try:
        with engine().begin() as conn:
            set_actor(conn, actor)
            return work(conn)
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 - the trigger's message is the answer
        raise Refused(_verdict(exc)) from exc


def approve(actor: str, user_id: str, roles: list[str], companies: list[str], note: str = "") -> dict[str, Any]:
    """PENDING -> ACTIVE with the access the superadmin chose, in one transaction."""
    roles, companies = _check_access(roles, companies)
    if not roles:
        raise Refused("choose at least one role; an account with none can do nothing")

    def work(conn):
        account = _account(conn, user_id)
        if account["status"] != "PENDING":
            raise Refused(f"{user_id} is {account['status']}, not waiting for approval")
        _set_status(conn, actor, user_id, "ACTIVE", note)
        change = _write_access(conn, user_id, roles, companies)
        record(conn, actor, "app_user", user_id, "APPROVED", {"roles": roles, "companies": companies, "note": note})
        return {"user_id": user_id, "status": "ACTIVE", "roles": roles, "companies": companies, **change}

    return _run(actor, work)


def reject(actor: str, user_id: str, note: str = "") -> dict[str, Any]:
    def work(conn):
        account = _account(conn, user_id)
        if account["status"] != "PENDING":
            raise Refused(f"{user_id} is {account['status']}, not waiting for approval")
        _set_status(conn, actor, user_id, "REJECTED", note)
        _revoke_sessions(conn, user_id)
        record(conn, actor, "app_user", user_id, "REJECTED", {"note": note})
        return {"user_id": user_id, "status": "REJECTED"}

    return _run(actor, work)


def set_access(actor: str, user_id: str, roles: list[str], companies: list[str], note: str = "") -> dict[str, Any]:
    """Change an ACTIVE account's roles and companies. Takes effect on its next
    request: the token resolves roles and scope from the tables every time."""
    roles, companies = _check_access(roles, companies)

    def work(conn):
        account = _account(conn, user_id)
        if account["status"] != "ACTIVE":
            raise Refused(f"{user_id} is {account['status']}; approve or enable it first")
        change = _write_access(conn, user_id, roles, companies)
        record(conn, actor, "app_user", user_id, "ACCESS_CHANGED", {**change, "note": note})
        return {"user_id": user_id, "status": "ACTIVE", "roles": roles, "companies": companies, **change}

    return _run(actor, work)


def rename(actor: str, user_id: str, display_name: str) -> dict[str, Any]:
    """Change a person's display name. It grants nothing, so a superadmin may
    rename anyone, themselves included, in any status; migration 018 refuses it
    from anyone but a superadmin or the account holder."""
    name = (display_name or "").strip()
    if not name:
        raise Refused("enter a name")
    if len(name) > 200:
        raise Refused("a name is at most 200 characters")

    def work(conn):
        row = conn.execute(
            text(f"SELECT display_name, is_human FROM {SCHEMA}.app_user WHERE user_id = :u FOR UPDATE"), {"u": user_id},
        ).mappings().first()
        if row is None:
            raise Refused(f"no account {user_id!r}")
        if not row["is_human"]:
            raise Refused("service and agent identities are managed in the seed, not here")
        if row["display_name"] == name:
            return {"user_id": user_id, "display_name": name, "changed": False}
        conn.execute(text(f"UPDATE {SCHEMA}.app_user SET display_name = :n WHERE user_id = :u"), {"n": name, "u": user_id})
        record(conn, actor, "app_user", user_id, "RENAMED", {"from": row["display_name"], "to": name})
        return {"user_id": user_id, "display_name": name, "changed": True}

    return _run(actor, work)


def change_email(actor: str, user_id: str, email: str) -> dict[str, Any]:
    """Change the email a person signs in with.

    Only a superadmin changes it -- anyone's, their own included, because an
    email grants no access (migration 019). Unique ignoring case. The
    person's password and open sessions are unchanged: they sign in with the
    new address from now on.
    """
    email = (email or "").strip()
    if not _EMAIL.match(email) or len(email) > 320:
        raise Refused("enter a valid email address")

    def work(conn):
        row = conn.execute(
            text(f"SELECT email, is_human FROM {SCHEMA}.app_user WHERE user_id = :u FOR UPDATE"), {"u": user_id},
        ).mappings().first()
        if row is None:
            raise Refused(f"no account {user_id!r}")
        if not row["is_human"]:
            raise Refused("service and agent identities are managed in the seed, not here")
        if row["email"] == email:
            return {"user_id": user_id, "email": email, "changed": False}
        taken = conn.execute(
            text(f"SELECT 1 FROM {SCHEMA}.app_user WHERE lower(email) = lower(:e) AND user_id <> :u"), {"e": email, "u": user_id},
        ).first()
        if taken:
            raise Refused("another account already uses that email")
        conn.execute(text(f"UPDATE {SCHEMA}.app_user SET email = :e WHERE user_id = :u"), {"e": email, "u": user_id})
        record(conn, actor, "app_user", user_id, "EMAIL_CHANGED", {"from": row["email"].lower(), "to": email.lower()})
        return {"user_id": user_id, "email": email, "changed": True}

    return _run(actor, work)


def set_enabled(actor: str, user_id: str, enabled: bool, note: str = "") -> dict[str, Any]:
    """Disable (ends every session at once) or re-enable an account."""
    def work(conn):
        account = _account(conn, user_id)
        target = "ACTIVE" if enabled else "DISABLED"
        if account["status"] == target:
            return {"user_id": user_id, "status": target, "changed": False}
        _set_status(conn, actor, user_id, target, note)
        if not enabled:
            _revoke_sessions(conn, user_id)
        record(conn, actor, "app_user", user_id, "ENABLED" if enabled else "DISABLED", {"note": note})
        return {"user_id": user_id, "status": target, "changed": True}

    return _run(actor, work)
