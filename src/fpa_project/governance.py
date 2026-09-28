"""The plan spine's application half: what Postgres cannot decide on its own.

The database enforces what it can: the lock, the self-approval ban, the
covenant gate, the derivation trace, the controller-only fields, the audit
chain. This module is the rest -- the pieces that need the calculation
engine, the token, or a conversation with the caller -- and every write it
makes goes through the same connection settings the triggers read, so the
two halves agree on who is acting.

Three gates for a human live in this system; this is the second one, the
state machine. It records who may move what, and it holds whether or not
anything is running. The Temporal signal (recompute/) holds an execution that
is waiting; the Agno confirmation (agent_team/) holds a model that is about
to act. See docs/RECOMPUTE.md for why they are not one thing.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from db.config import database_url
from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import dependency_names, detect_cycles, parse_formula, referenced_names, validate_formula
from fpa_project.dsl.schema import Schema

SCHEMA = "fpa_governance"

_lock = threading.Lock()
_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = create_engine(database_url(), pool_pre_ping=True)
        return _engine


def set_actor(conn: Connection, actor: str) -> None:
    """Name the actor for this transaction. The triggers read it."""
    conn.execute(text("SELECT set_config('fpa.actor', :actor, true)"), {"actor": actor})


def record(conn: Connection, actor: str | None, entity_type: str, entity_id: str, action: str, payload: dict) -> None:
    """Append to the audit log. The database computes the chain."""
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.audit_event (actor_user_id, entity_type, entity_id, action, payload) "
            "VALUES (:actor, :entity_type, :entity_id, :action, CAST(:payload AS jsonb)) "
            "ON CONFLICT (event_key) DO NOTHING"
        ),
        {"actor": actor, "entity_type": entity_type, "entity_id": entity_id, "action": action,
         "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)},
    )


class Refused(ValueError):
    """The request is not allowed, and this says why. Never a bare 403."""


TransitionRefused = Refused


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """The authenticated caller: identity, roles and the entities they may read.

    Built from the token and the tables, never from anything in a request
    body. ``companies`` is exactly the rows in ``user_company_scope``; a user
    with none reads nothing. ``status`` is the account's (migration 017): only
    an ACTIVE principal reaches anything but "who am I".
    """

    user_id: str
    display_name: str
    roles: frozenset[str]
    companies: frozenset[str]
    is_human: bool
    status: str = "ACTIVE"
    email: str = ""

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def authenticate(token: str | None, allow_pending: bool = False) -> Principal:
    """Resolve a bearer token: a seeded standing token, or a session issued at
    signup or sign-in (live, not revoked, not expired). Unknown, empty, expired
    or inactive is refused; a PENDING account only when ``allow_pending``."""
    if not token:
        raise Refused("no bearer token")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT u.user_id, u.display_name, u.email, u.is_human, u.status "
                f"FROM {SCHEMA}.user_session s JOIN {SCHEMA}.app_user u USING (user_id) "
                "WHERE s.token_hash = :h AND s.revoked_at IS NULL "
                "AND (s.expires_at IS NULL OR s.expires_at > now()) "
                "LIMIT 1"
            ),
            {"h": digest},
        ).mappings().first()
        if row is None:
            raise Refused("unknown, expired or signed-out token")
        if row["status"] in ("REJECTED", "DISABLED"):
            raise Refused(f"this account is {row['status'].lower()}")
        if row["status"] == "PENDING" and not allow_pending:
            raise Refused("this account is waiting for a superadmin to approve it")
        roles = {r[0] for r in conn.execute(text(f"SELECT role_code FROM {SCHEMA}.user_role WHERE user_id = :u"), {"u": row["user_id"]})}
        companies = {r[0] for r in conn.execute(text(f"SELECT company_code FROM {SCHEMA}.user_company_scope WHERE user_id = :u"), {"u": row["user_id"]})}
    return Principal(row["user_id"], row["display_name"], frozenset(roles), frozenset(companies), bool(row["is_human"]),
                     row["status"], row["email"] or "")


# ---------------------------------------------------------------------------
# Plan versions
# ---------------------------------------------------------------------------
def create_plan_version(code: str, model_code: str, plan_year: int, actor: str, covenant_note: str = "") -> dict[str, Any]:
    """Author a DRAFT. Planners do this; the table of transitions does not
    cover creation, so the role rule for it lives here and is named."""
    with engine().begin() as conn:
        set_actor(conn, actor)
        roles = _roles(conn, actor)
        if "planner" not in roles:
            raise Refused(f"{actor} may not author a plan version; that needs the planner role")
        model_id = conn.execute(
            text(f"SELECT model_id FROM {SCHEMA}.planning_model WHERE model_code = :m"), {"m": model_code}
        ).scalar()
        if model_id is None:
            raise Refused(f"no planning model {model_code!r}")
        if conn.execute(text(f"SELECT 1 FROM {SCHEMA}.plan_version WHERE plan_version_code = :c"), {"c": code}).first():
            raise Refused(f"plan version {code!r} already exists")
        row = conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.plan_version (plan_version_code, model_id, plan_year, state, covenant_ok, covenant_note, requested_by) "
                "VALUES (:c, :m, :y, 'DRAFT', false, :note, :actor) RETURNING plan_version_id, row_version"
            ),
            {"c": code, "m": model_id, "y": plan_year, "note": covenant_note or None, "actor": actor},
        ).first()
        record(conn, actor, "plan_version", code, "CREATED", {"model": model_code, "plan_year": plan_year})
    return {"plan_version_code": code, "plan_version_id": str(row[0]), "state": "DRAFT", "row_version": row[1]}


def transition(plan_version_code: str, to_state: str, actor: str, note: str = "", expected_version: int | None = None) -> dict[str, Any]:
    """Move a plan version, if this actor holds a role that may.

    Checks in this order, because each one makes the next question
    meaningful: the version exists, the caller read the version they think
    they did, the move is a declared transition, the actor holds a role
    allowed to make it. The column constraints and triggers then have the
    last word: self-approval, the covenant gate, the lock and the
    controller-only covenant fields are all refused by the database itself,
    and this function only translates the refusal.
    """
    try:
        with engine().begin() as conn:
            set_actor(conn, actor)
            row = conn.execute(
                text(
                    "SELECT plan_version_id, state, requested_by, row_version, supersedes_plan_version_id "
                    f"FROM {SCHEMA}.plan_version WHERE plan_version_code = :code"
                ),
                {"code": plan_version_code},
            ).first()
            if row is None:
                raise Refused(f"no plan version {plan_version_code!r}")
            plan_version_id, from_state, requested_by, row_version = str(row[0]), row[1], row[2], row[3]
            if row[4] is not None and to_state != "SUPERSEDED":
                # A re-forecast's review, approval and lock are its run's
                # gates; moving it here would leave the run parked on a
                # version that had already moved on without it.
                raise Refused(
                    f"{plan_version_code} is a re-forecast: the planner submits it, a controller approves it and "
                    "the CFO locks it through its run, not through a plain transition"
                )

            if expected_version is not None and expected_version != row_version:
                raise Refused(
                    f"lost the race: {plan_version_code} is at row_version {row_version}, you read {expected_version}. "
                    "Reload it and decide again."
                )
            if from_state == to_state:
                return {"plan_version_code": plan_version_code, "state": to_state, "changed": False, "row_version": row_version}

            allowed = [r[0] for r in conn.execute(
                text(f"SELECT role_code FROM {SCHEMA}.plan_state_transition WHERE from_state = :f AND to_state = :t"),
                {"f": from_state, "t": to_state},
            )]
            if not allowed:
                raise Refused(f"{from_state} -> {to_state} is not a declared transition for a plan version")
            roles = _roles(conn, actor)
            if not roles & set(allowed):
                raise Refused(
                    f"{actor} may not move a plan version from {from_state} to {to_state}; "
                    f"that needs one of: {', '.join(sorted(allowed))}"
                )
            if to_state == "APPROVED" and actor == requested_by:
                raise Refused(f"segregation of duties: {actor} requested this plan version and cannot approve it")

            if to_state == "APPROVED":
                # Approval consumes the stored covenant verdict. It must not
                # turn a failed review into a passing one; the database check
                # rejects approval until a controller explicitly records it.
                updated = conn.execute(
                    text(
                        f"UPDATE {SCHEMA}.plan_version SET state = :to, approved_by = :actor, updated_at = now() "
                        "WHERE plan_version_id = CAST(:pv AS uuid) AND row_version = :rv RETURNING row_version"
                    ),
                    {"to": to_state, "actor": actor, "pv": plan_version_id, "rv": row_version},
                ).scalar()
            else:
                updated = conn.execute(
                    text(
                        f"UPDATE {SCHEMA}.plan_version SET state = :to, updated_at = now() "
                        "WHERE plan_version_id = CAST(:pv AS uuid) AND row_version = :rv RETURNING row_version"
                    ),
                    {"to": to_state, "pv": plan_version_id, "rv": row_version},
                ).scalar()
            if updated is None:
                raise Refused(f"lost the race: {plan_version_code} changed under you; reload it and decide again")
            record(conn, actor, "plan_version", plan_version_code, f"STATE_{to_state}", {"from": from_state, "to": to_state, "note": note})
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 - a database verdict, translated
        raise Refused(_verdict(exc)) from exc
    return {"plan_version_code": plan_version_code, "from_state": from_state, "state": to_state, "changed": True, "row_version": updated}


def describe(plan_version_code: str) -> dict[str, Any]:
    """The version's current state and where it may go from here."""
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT plan_version_id, plan_version_code, state, requested_by, approved_by, covenant_ok, covenant_note, "
                "       revision, row_version, supersedes_plan_version_id, updated_at "
                f"FROM {SCHEMA}.plan_version WHERE plan_version_code = :code"
            ),
            {"code": plan_version_code},
        ).mappings().first()
        if row is None:
            raise Refused(f"no plan version {plan_version_code!r}")
        onward = [
            {"to_state": r[0], "role_code": r[1]}
            for r in conn.execute(
                text(f"SELECT to_state, role_code FROM {SCHEMA}.plan_state_transition WHERE from_state = :state ORDER BY to_state"),
                {"state": row["state"]},
            )
        ]
        chain = conn.execute(
            text(
                f"SELECT plan_version_code, state, revision FROM {SCHEMA}.plan_version "
                "WHERE supersedes_plan_version_id = CAST(:pv AS uuid) ORDER BY revision"
            ),
            {"pv": str(row["plan_version_id"])},
        ).mappings().all()
    out = dict(row)
    out["plan_version_id"] = str(out["plan_version_id"])
    out["supersedes_plan_version_id"] = str(out["supersedes_plan_version_id"]) if out["supersedes_plan_version_id"] else None
    out["transitions"] = onward
    out["superseded_by"] = [dict(r) for r in chain]
    return out


def plan_lines(plan_version_code: str, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """A version's governed lines with their derivation traces, largest amounts first.

    For a re-forecast successor these are exactly the recomputed lines: the
    run writes only the dirty slice, base scenario only (migration 016).
    """
    with engine().begin() as conn:
        version = conn.execute(
            text(f"SELECT plan_version_id, state FROM {SCHEMA}.plan_version WHERE plan_version_code = :code"),
            {"code": plan_version_code},
        ).first()
        if version is None:
            raise Refused(f"no plan version {plan_version_code!r}")
        summary = conn.execute(
            text(
                "SELECT count(*) AS line_count, coalesce(sum(amount_functional), 0) AS amount_functional, "
                "       array_agg(DISTINCT company_code ORDER BY company_code) AS companies, "
                "       min(period_month) AS first_month, max(period_month) AS last_month "
                f"FROM {SCHEMA}.plan_version_line WHERE plan_version_id = :pv"
            ),
            {"pv": version[0]},
        ).mappings().one()
        lines = conn.execute(
            text(
                "SELECT scenario_code, company_code, period_month, account_code, dim_signature_hash, quantity, "
                "       unit_price, amount_functional, functional_currency, source_revision, driver_derivation_trace "
                f"FROM {SCHEMA}.plan_version_line WHERE plan_version_id = :pv "
                "ORDER BY abs(amount_functional) DESC, company_code, period_month, account_code, dim_signature_hash "
                "LIMIT :limit OFFSET :offset"
            ),
            {"pv": version[0], "limit": limit, "offset": offset},
        ).mappings().all()
    return {
        "plan_version_code": plan_version_code, "state": version[1],
        "summary": {**dict(summary), "companies": [c for c in (summary["companies"] or []) if c]},
        "lines": [dict(r) for r in lines],
    }


def countries_of(companies: frozenset[str] | set[str]) -> frozenset[str]:
    """The countries a set of companies sits in, read from dim_company.

    Read rather than parsed out of the company code: the codes happen to carry
    the country today, but an authorisation decision that rests on a naming
    convention breaks silently the first time one does not.
    """
    if not companies:
        return frozenset()
    with engine().begin() as conn:
        return frozenset(conn.execute(
            text(f"SELECT DISTINCT country_code FROM {SCHEMA}.dim_company WHERE company_code = ANY(:codes)"),
            {"codes": sorted(companies)},
        ).scalars())


def list_plan_versions() -> list[dict[str, Any]]:
    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT v.plan_version_code, v.state, v.requested_by, v.approved_by, v.covenant_ok, v.revision, "
                "       v.row_version, v.created_at, v.updated_at, s.plan_version_code AS supersedes_plan_version_code "
                f"FROM {SCHEMA}.plan_version v "
                f"LEFT JOIN {SCHEMA}.plan_version s ON s.plan_version_id = v.supersedes_plan_version_id "
                "ORDER BY v.created_at"
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Drivers and the planning model: validation at the point of authoring
# ---------------------------------------------------------------------------
def _model_schema(conn: Connection, model_id: str, extra: dict[str, str] | None = None) -> tuple[Schema, dict[str, str]]:
    """The registry as the model's drivers see it: metrics plus these drivers."""
    formulas = {
        r[0]: r[1] for r in conn.execute(
            text(f"SELECT driver_code, formula FROM {SCHEMA}.plan_driver WHERE model_id = CAST(:m AS uuid) AND status <> 'RETIRED'"),
            {"m": model_id},
        )
    }
    formulas.update(extra or {})
    base = Schema()
    return Schema({**base.data, "drivers": sorted(formulas)}), formulas


def save_driver(model_code: str, driver_code: str, driver_name: str, formula: str, actor: str,
                unit: str = "ratio", value_type: str = "numeric", effective_from: str = "2026-01-01", expected_version: str | None = None) -> dict[str, Any]:
    """Save a driver: parse, resolve every name, check the graph, then write.

    Unparseable is rejected with the position. A name that is neither a
    driver of this model nor a metric is rejected by name. A same-period
    cycle is rejected with the path. Nothing is saved on any of those.
    """
    with engine().begin() as conn:
        set_actor(conn, actor)
        if "controller" not in _roles(conn, actor) and "planner" not in _roles(conn, actor):
            raise Refused(f"{actor} may not author drivers; that needs the planner or controller role")
        model_id = conn.execute(text(f"SELECT model_id FROM {SCHEMA}.planning_model WHERE model_code = :m"), {"m": model_code}).scalar()
        if model_id is None:
            raise Refused(f"no planning model {model_code!r}")
        # Serialize library edits before validating the derived dependency graph.
        conn.execute(text(f"SELECT model_id FROM {SCHEMA}.planning_model WHERE model_id = :m FOR UPDATE"), {"m": model_id})
        existing = conn.execute(text(
            f"SELECT xmin::text FROM {SCHEMA}.plan_driver WHERE model_id = :m AND driver_code = :code AND effective_from = CAST(:eff AS date) FOR UPDATE"
        ), {"m": model_id, "code": driver_code, "eff": effective_from}).scalar()
        if existing is not None and expected_version != existing:
            raise Refused("driver changed or no version supplied; reload its row_version before editing")
        if existing is None and expected_version is not None:
            raise Refused("driver no longer exists; reload before editing")
        try:
            node = parse_formula(formula)
        except ParseError as exc:
            raise Refused(f"formula does not parse: {exc}") from exc
        schema, formulas = _model_schema(conn, str(model_id), {driver_code: formula})
        try:
            validate_formula(node, schema)
            detect_cycles({code: parse_formula(f) for code, f in formulas.items()})
        except DSLValidationError as exc:
            raise Refused(str(exc)) from exc
        unresolved = referenced_names(node) - schema.driver_names - schema.metric_names
        if unresolved:
            raise Refused(f"formula references names that are not in calc_order_dag: {', '.join(sorted(unresolved))}")

        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.plan_driver (model_id, driver_code, driver_name, formula, effective_from, unit, value_type, status, created_by) "
                "VALUES (CAST(:m AS uuid), :code, :name, :formula, CAST(:eff AS date), :unit, :vtype, 'DRAFT', :actor) "
                "ON CONFLICT (model_id, driver_code, effective_from) DO UPDATE SET formula = EXCLUDED.formula, "
                "    driver_name = EXCLUDED.driver_name, unit = EXCLUDED.unit, value_type = EXCLUDED.value_type"
            ),
            {"m": str(model_id), "code": driver_code, "name": driver_name, "formula": formula, "eff": effective_from,
             "unit": unit, "vtype": value_type, "actor": actor},
        )
        _write_dag(conn, str(model_id), formulas)
        record(conn, actor, "plan_driver", f"{model_code}:{driver_code}", "SAVED",
               {"formula": formula, "depends_on": sorted(dependency_names(node))})
    return {"driver_code": driver_code, "formula": formula, "depends_on": sorted(dependency_names(node)), "status": "DRAFT"}


def save_planning_model(model_code: str, formulas: dict[str, str], actor: str) -> dict[str, Any]:
    """Validate a whole driver library and derive its DAG.

    The DAG is derived from the formulas rather than accepted from the
    caller, so it cannot disagree with them. A cycle is refused with its path
    and nothing is written.
    """
    parsed: dict[str, Any] = {}
    issues: list[str] = []
    schema = Schema({**Schema().data, "drivers": sorted(formulas)})
    for code, formula in formulas.items():
        try:
            node = parse_formula(formula)
            validate_formula(node, schema)
            parsed[code] = node
        except (ParseError, DSLValidationError) as exc:
            issues.append(f"{code}: {exc}")
    if issues:
        raise Refused("; ".join(issues))
    try:
        detect_cycles(parsed)
    except DSLValidationError as exc:
        raise Refused(str(exc)) from exc
    with engine().begin() as conn:
        set_actor(conn, actor)
        if "controller" not in _roles(conn, actor):
            raise Refused(f"{actor} may not save a planning model; that needs the controller role")
        model_id = conn.execute(text(f"SELECT model_id FROM {SCHEMA}.planning_model WHERE model_code = :m"), {"m": model_code}).scalar()
        if model_id is None:
            raise Refused(f"no planning model {model_code!r}")
        dag = _write_dag(conn, str(model_id), formulas)
        record(conn, actor, "planning_model", model_code, "DAG_SAVED", {"drivers": sorted(formulas)})
    return {"model_code": model_code, "calc_order_dag": dag}


def _write_dag(conn: Connection, model_id: str, formulas: dict[str, str]) -> list[dict[str, Any]]:
    dag = [{"driver": code, "depends_on": sorted(dependency_names(parse_formula(f)))} for code, f in formulas.items()]
    conn.execute(
        text(f"UPDATE {SCHEMA}.planning_model SET calc_order_dag = CAST(:dag AS jsonb) WHERE model_id = CAST(:m AS uuid)"),
        {"dag": json.dumps(dag), "m": model_id},
    )
    return dag


def list_drivers(model_code: str = "FPA-2026") -> list[dict[str, Any]]:
    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT d.driver_code, d.driver_name, d.formula, d.unit, d.value_type, d.status, d.effective_from, d.xmin::text AS row_version "
                f"FROM {SCHEMA}.plan_driver d JOIN {SCHEMA}.planning_model m USING (model_id) "
                "WHERE m.model_code = :m ORDER BY d.driver_code, d.effective_from"
            ),
            {"m": model_code},
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Controller-only fields, through the one place both tiers route
# ---------------------------------------------------------------------------
CONTROLLER_FIELDS = {"plan_version": ("covenant_ok", "covenant_note"), "plan_fx_rate": ("rate",)}


def set_plan_fx_rate(plan_version_code: str, period_month: str, from_currency: str, rate: str, actor: str, expected_version: int | None = None) -> dict[str, Any]:
    """Write a plan FX rate. The trigger refuses anyone but a controller;
    this function exists so the API and the agent tier share one path."""
    try:
        with engine().begin() as conn:
            set_actor(conn, actor)
            _lock_version(conn, plan_version_code, expected_version)
            pv = conn.execute(text(f"SELECT plan_version_id FROM {SCHEMA}.plan_version WHERE plan_version_code = :c"), {"c": plan_version_code}).scalar()
            if pv is None:
                raise Refused(f"no plan version {plan_version_code!r}")
            conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.plan_fx_rate (plan_version_id, period_month, from_currency, to_currency, rate) "
                    "VALUES (CAST(:pv AS uuid), CAST(:pm AS date), :ccy, 'USD', :rate) "
                    "ON CONFLICT (plan_version_id, period_month, from_currency, to_currency) DO UPDATE SET rate = EXCLUDED.rate"
                ),
                {"pv": str(pv), "pm": period_month, "ccy": from_currency, "rate": rate},
            )
            conn.execute(text(f"UPDATE {SCHEMA}.plan_version SET updated_at = now() WHERE plan_version_code = :c"), {"c": plan_version_code})
            record(conn, actor, "plan_fx_rate", f"{plan_version_code}:{from_currency}:{period_month}", "SET", {"rate": rate})
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Refused(_verdict(exc)) from exc
    return {"plan_version_code": plan_version_code, "period_month": period_month, "from_currency": from_currency, "rate": rate}


def set_covenant(plan_version_code: str, covenant_ok: bool, note: str, actor: str, expected_version: int | None = None) -> dict[str, Any]:
    try:
        with engine().begin() as conn:
            set_actor(conn, actor)
            _lock_version(conn, plan_version_code, expected_version)
            updated = conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_version SET covenant_ok = :ok, covenant_note = :note, updated_at = now() "
                    "WHERE plan_version_code = :c RETURNING row_version"
                ),
                {"ok": covenant_ok, "note": note, "c": plan_version_code},
            ).scalar()
            if updated is None:
                raise Refused(f"no plan version {plan_version_code!r}")
            record(conn, actor, "plan_version", plan_version_code, "COVENANT_SET", {"covenant_ok": covenant_ok, "note": note})
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Refused(_verdict(exc)) from exc
    return {"plan_version_code": plan_version_code, "covenant_ok": covenant_ok, "row_version": updated}


# ---------------------------------------------------------------------------
# The audit chain verifier
# ---------------------------------------------------------------------------
@dataclass
class ChainVerdict:
    ok: bool
    rows: int
    checked_to: int | None = None
    broken_at: int | None = None
    reason: str = ""
    failures: list[dict[str, Any]] = field(default_factory=list)


def verify_audit_chain(stop_at_first: bool = False) -> ChainVerdict:
    """Recompute every hash and every link. Fails loudly on any altered row.

    Each row's key is recomputed from its stored facts, its hash from the
    previous row's hash and that key, and its stored previous_hash is checked
    against the row before it. Change a payload, an actor, an action, or
    splice a row out, and this says which row and why.
    """
    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT audit_event_id, actor_user_id, entity_type, entity_id, action, payload::text AS payload, "
                f"       previous_hash, event_key, event_hash FROM {SCHEMA}.audit_event ORDER BY audit_event_id"
            )
        ).mappings().all()
    previous: str | None = None
    verdict = ChainVerdict(ok=True, rows=len(rows))
    for row in rows:
        facts = "|".join([row["actor_user_id"] or "", row["entity_type"], row["entity_id"], row["action"], row["payload"]])
        key = hashlib.sha256(facts.encode()).hexdigest()
        expected_hash = hashlib.sha256(f"{previous or ''}|{key}".encode()).hexdigest()
        problems = []
        if key != row["event_key"]:
            problems.append("the stored facts no longer hash to event_key: a payload, actor, action or entity was altered")
        if (row["previous_hash"] or None) != previous:
            problems.append(f"previous_hash does not point at the row before it (expected {previous!r})")
        if expected_hash != row["event_hash"]:
            problems.append("event_hash does not chain from the previous row")
        if problems:
            verdict.ok = False
            verdict.failures.append({"audit_event_id": row["audit_event_id"], "problems": problems})
            if verdict.broken_at is None:
                verdict.broken_at, verdict.reason = row["audit_event_id"], "; ".join(problems)
            if stop_at_first:
                break
        else:
            verdict.checked_to = row["audit_event_id"]
        previous = row["event_hash"]
    return verdict


def audit_trail(entity_type: str | None = None, entity_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses, params = [], {"limit": limit}
    if entity_type:
        clauses.append("entity_type = :et"); params["et"] = entity_type
    if entity_id:
        clauses.append("entity_id = :ei"); params["ei"] = entity_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT audit_event_id, occurred_at, actor_user_id, entity_type, entity_id, action, payload, previous_hash, event_hash "
                f"FROM {SCHEMA}.audit_event {where} ORDER BY audit_event_id DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _roles(conn: Connection, actor: str) -> set[str]:
    return {r[0] for r in conn.execute(text(f"SELECT role_code FROM {SCHEMA}.user_role WHERE user_id = :u"), {"u": actor})}


def _verdict(exc: Exception) -> str:
    """A database refusal, as its first line: the trigger's message is the answer."""
    original = getattr(exc, "orig", exc)
    return str(original).splitlines()[0]


def require_global_scope(who: Principal) -> None:
    with engine().connect() as conn:
        companies = frozenset(conn.execute(text(f"SELECT company_code FROM {SCHEMA}.dim_company")).scalars())
    if not companies or not companies <= who.companies:
        raise Refused("this operation covers the global plan; full model entity scope is required")


def _lock_version(conn, code: str, expected_version: int | None):
    current = conn.execute(text(f"SELECT row_version FROM {SCHEMA}.plan_version WHERE plan_version_code = :c FOR UPDATE"), {"c": code}).scalar()
    if current is None:
        raise Refused(f"no plan version {code!r}")
    if expected_version is not None and current != expected_version:
        raise Refused("lost the race: reload the plan before editing")
