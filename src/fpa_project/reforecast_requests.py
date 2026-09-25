"""Question-driven re-forecast requests: drafted by the agent team, confirmed by the planner.

The write path this module serves:

1. A planner asks in words ("drop Poland utilisation to 74% and re-run the
   second half"). Only a human planner's agent team gets the
   ``propose_reforecast`` tool, and the tool resolves the words against this
   module's :class:`ReforecastDesk`: the driver must be active and have one
   current value, the country becomes companies, the period becomes months of
   the plan year, and the starting value is what the published plan currently
   applies to that slice.
2. The API stores the draft as a ``reforecast_request`` (PROPOSED), with the
   read-path evidence the planner was shown.
3. The planner who asked confirms the agent's draft, which starts the durable
   workflow with the covenant gate on, or withdraws it. Confirming decides
   nothing about the plan: the run recomputes a DRAFT the planner then reviews
   and submits, a controller approves and the CFO locks (migration 022).
4. From then on the request's state mirrors its run (migrations 015, 022).

The agent never writes: it returns a validated draft and the API persists it
as the planner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from fpa_project.governance import Principal, Refused, engine, record, set_actor
from fpa_project.recompute.models import DriverShock

SCHEMA = "fpa_governance"
TERMINAL = {"CONTROLLER_REJECTED", "COVENANT_FAILED", "PUBLISHED", "CFO_REJECTED", "EXPIRED", "CANCELLED",
            "COMPENSATED", "FAILED"}

# Names a planner is likely to type, to the country codes dim_company uses.
COUNTRY_NAMES = {
    "poland": "PL", "germany": "DE", "united kingdom": "UK", "great britain": "UK", "britain": "UK", "gb": "UK",
    "united states": "US", "usa": "US", "america": "US", "india": "IN", "singapore": "SG",
    "australia": "AU", "canada": "CA", "uae": "AE", "united arab emirates": "AE",
}


@dataclass
class Draft:
    """A validated re-forecast the planner asked for. Plain data; nothing is written."""

    plan_version_code: str
    driver_code: str
    driver_name: str
    from_value: float
    to_value: float
    companies: list[str] = field(default_factory=list)
    months: list[str] = field(default_factory=list)
    scope_label: str = "all companies, all months"
    # Which reading of the request produced to_value. "by 10%" is a change in
    # the driver's own units to one reader and a relative change to another,
    # and on a ratio driver the two give different numbers. The desk cannot
    # tell which was meant, so it records the one it applied and the planner
    # confirming the draft is shown it.
    change_basis: str = "absolute"

    def alternative_value(self) -> float | None:
        """What the reading not taken would have produced, if there is one.

        Published in change_note, so it has to count as a figure the narrative
        may cite: the desk puts this number in front of the planner, and the
        model repeating it back is quoting the draft, not inventing.
        """
        if self.change_basis == "absolute" or not self.from_value:
            return None
        delta = self.to_value - self.from_value
        if self.change_basis == "points":
            return round(self.from_value * (1 + delta), 10)
        return round(self.from_value + delta / self.from_value, 10)

    def change_note(self) -> str:
        """The reading applied, and the number the other reading would give."""
        if self.change_basis == "absolute":
            return (f"Read as an absolute target: {self.driver_name} moves from "
                    f"{self.from_value:g} to {self.to_value:g}.")
        delta = self.to_value - self.from_value
        other = self.alternative_value()
        if self.change_basis == "points":
            return (f"Read as a {delta:+g} change in the driver's own units: {self.from_value:g} "
                    f"to {self.to_value:g}. Read instead as a relative change of the same size it "
                    f"would be {other:g}; say which you meant if this is not it.")
        return (f"Read as a relative change: {self.from_value:g} to {self.to_value:g}."
                + (f" Read instead as a change in the driver's own units it would be {other:g};"
                   " say which you meant if this is not it." if other is not None else ""))

    def shock(self) -> DriverShock:
        return DriverShock(self.driver_code, self.from_value, self.to_value, list(self.companies), list(self.months))

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_version_code": self.plan_version_code, "driver_code": self.driver_code,
            "driver_name": self.driver_name, "from_value": self.from_value, "to_value": self.to_value,
            "companies": self.companies, "months": self.months, "scope_label": self.scope_label,
            "change_basis": self.change_basis, "change_note": self.change_note(),
            "alternative_value": self.alternative_value(),
        }


class ReforecastDesk:
    """What a planner's agent team resolves a re-forecast request against.

    Built per request, only for a human planner with the whole-model scope.
    Reads governance data; writes nothing.
    """

    def __init__(self, who: Principal, default_plan: str = "PV-2026-0001"):
        self.who = who
        self.default_plan = default_plan

    def resolve(
        self,
        driver_code: str,
        to_value: float | None = None,
        country: str = "",
        companies: list[str] | None = None,
        period: str = "",
        plan_version_code: str = "",
        *,
        by_amount: float | None = None,
        by_percent: float | None = None,
    ) -> Draft:
        """Resolve a target value for the slice, stated absolutely or as a change.

        A relative request ("increase utilisation by 5 points") can only be
        turned into a number once the slice's current value is known, and only
        the desk knows that. So the arithmetic happens here, against the value
        the published plan actually applies, never upstream in the caller's
        head: ``by_amount`` moves the current value by that much in the
        driver's own units, ``by_percent`` scales it.
        """
        given = [name for name, value in
                 (("to_value", to_value), ("by_amount", by_amount), ("by_percent", by_percent)) if value is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of to_value (the new value), by_amount (a change in the driver's own units) or "
                f"by_percent (a relative change, 5 for +5%); got {', '.join(given) or 'none'}"
            )
        target = _number("to_value", to_value) if to_value is not None else None
        amount = _number("by_amount", by_amount) if by_amount is not None else None
        percent = _number("by_percent", by_percent) if by_percent is not None else None
        if target is not None and target <= 0:
            raise ValueError("to_value must be greater than zero")
        with engine().connect() as conn:
            plan = self._plan(conn, plan_version_code or self.default_plan)
            driver_name, base = self._driver(conn, plan["model_id"], driver_code)
            # Checked before anything that depends on published data: a
            # percentage typed as a whole number is wrong whatever the plan says.
            if base <= 1 and target is not None and target > 1:
                raise ValueError(f"{driver_code} is a ratio (base value {base:g}); pass {target / 100:g} for {target:g}%")
            if base <= 1 and amount is not None and abs(amount) > 1:
                raise ValueError(f"{driver_code} is a ratio (base value {base:g}); pass by_amount "
                                 f"{amount / 100:g} for {amount:g} percentage points, or by_percent for a relative change")
            chosen = self._companies(conn, country, companies or [])
            months = self._months(period, int(plan["plan_year"]))
            current = self._current_value(conn, plan["plan_version_id"], driver_code, base, chosen, months)
        if target is None:
            # Float noise turns 0.72 + 0.05 into 0.7700000000000001, which would
            # be stored, recomputed and shown to the planner as the target.
            target = round(current + amount if amount is not None else current * (1 + percent / 100), 10)
            if target <= 0:
                raise ValueError(f"that change takes {driver_code} from {current:g} to {target:g}, which is not a value it can hold")
        if abs(target - current) < 1e-12:
            raise ValueError(f"{driver_code} is already {current:g} for {_label(chosen, months)}")
        return Draft(
            plan_version_code=plan["plan_version_code"], driver_code=driver_code, driver_name=driver_name,
            from_value=round(current, 10), to_value=target, companies=chosen, months=months,
            scope_label=_label(chosen, months),
            change_basis="absolute" if to_value is not None else "points" if amount is not None else "relative",
        )

    # -- resolution steps --------------------------------------------------
    @staticmethod
    def _plan(conn, code: str) -> dict[str, Any]:
        row = conn.execute(
            text(
                f"SELECT plan_version_id::text, plan_version_code, model_id::text, plan_year, state, supersedes_plan_version_id "
                f"FROM {SCHEMA}.plan_version WHERE plan_version_code = :c"
            ),
            {"c": code},
        ).mappings().first()
        if row is None:
            raise ValueError(f"no plan version {code!r}")
        if row["supersedes_plan_version_id"] is not None:
            raise ValueError(f"{code} is itself a re-forecast; ask against the plan it re-forecasts")
        if row["state"] not in ("APPROVED", "LOCKED", "SUPERSEDED"):
            raise ValueError(f"{code} is {row['state']}; a re-forecast needs an APPROVED or LOCKED plan")
        return dict(row)

    @staticmethod
    def _driver(conn, model_id: str, code: str) -> tuple[str, float]:
        row = conn.execute(
            text(
                f"SELECT driver_name, formula FROM {SCHEMA}.plan_driver "
                "WHERE model_id = CAST(:m AS uuid) AND driver_code = :c AND status = 'ACTIVE' "
                "  AND effective_from <= current_date AND (effective_to IS NULL OR effective_to > current_date) "
                "ORDER BY effective_from DESC LIMIT 1"
            ),
            {"m": model_id, "c": code},
        ).first()
        if row is None:
            active = [r[0] for r in conn.execute(
                text(f"SELECT DISTINCT driver_code FROM {SCHEMA}.plan_driver WHERE model_id = CAST(:m AS uuid) "
                     "AND status = 'ACTIVE' ORDER BY 1"), {"m": model_id})]
            raise ValueError(f"{code!r} is not an active driver; active drivers are: {', '.join(active)}")
        try:
            return str(row[0]), float(Decimal(str(row[1]).strip()))
        except (InvalidOperation, ValueError):
            raise ValueError(
                f"{code} is computed ({row[1]}), not a single value, so it cannot be set directly; "
                "choose a driver with a fixed value"
            ) from None

    def _companies(self, conn, country: str, companies: list[str]) -> list[str]:
        chosen: set[str] = set()
        if country.strip():
            codes = {COUNTRY_NAMES.get(c.strip().lower(), c.strip().upper()) for c in re.split(r"[,/]| and ", country) if c.strip()}
            found = [r[0] for r in conn.execute(
                text(f"SELECT company_code FROM {SCHEMA}.dim_company WHERE country_code = ANY(:c) ORDER BY 1"),
                {"c": sorted(codes)},
            )]
            if not found:
                raise ValueError(f"no company in {country!r}")
            chosen.update(found)
        if companies:
            known = {r[0] for r in conn.execute(text(f"SELECT company_code FROM {SCHEMA}.dim_company"))}
            unknown = sorted(set(companies) - known)
            if unknown:
                raise ValueError(f"unknown companies: {', '.join(unknown)}")
            chosen.update(companies)
        outside = sorted(chosen - set(self.who.companies))
        if outside:
            raise ValueError(f"outside your entity scope: {', '.join(outside)}")
        return sorted(chosen)

    @staticmethod
    def _months(period: str, plan_year: int) -> list[str]:
        if not period.strip():
            return []
        from fpa_project.dsl.compiler import Compiler
        from fpa_project.dsl.parser import parse_query

        try:
            parsed = parse_query(f"SELECT services_revenue FOR PERIOD {period.strip()}").period
        except Exception as exc:  # noqa: BLE001 - the parser's message is the answer
            raise ValueError(f"period {period!r} is not a FinOpsExpr period (e.g. 2026-H2, 2026-Q3, 2026-07..2026-12): {exc}") from None
        start, end = Compiler.period_start(parsed.start), Compiler.period_end(parsed.end)
        months, cursor = [], start
        while cursor < end:
            months.append(cursor)
            cursor = Compiler.add_months(cursor, 1)
        outside = [m for m in months if int(m[:4]) != plan_year]
        if outside:
            raise ValueError(f"the plan covers {plan_year} only; {period} reaches {outside[0][:7]}")
        return months

    @staticmethod
    def _current_value(conn, plan_version_id: str, code: str, base: float, companies: list[str], months: list[str]) -> float:
        """The value the published plan currently applies to exactly this slice.

        The recompute composes every published move multiplicatively, so the
        current value is the base value times every published move on this
        driver that covers the whole slice. A move that covers only part of it
        would make "the current value" differ month by month or company by
        company; that case is refused rather than guessed.
        """
        shocks = conn.execute(
            text(
                f"SELECT shocks FROM {SCHEMA}.plan_publication WHERE plan_version_id = CAST(:pv AS uuid) "
                "AND state = 'COMMITTED' ORDER BY revision DESC LIMIT 1"
            ),
            {"pv": plan_version_id},
        ).scalar() or []
        current = base
        for item in shocks:
            shock = DriverShock.from_list(item)
            if shock.driver_code != code:
                continue
            if _covers(shock, companies, months):
                current *= shock.ratio()
            elif _overlaps(shock, companies, months):
                raise ValueError(
                    f"an earlier re-forecast moved {code} for {_label(shock.companies, shock.months)}, which only "
                    f"partly overlaps {_label(companies, months)}; ask for exactly that slice or one that contains it"
                )
        return current


def _number(name: str, value: Any) -> float:
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} {value!r} is not a number") from None


def _covers(shock: DriverShock, companies: list[str], months: list[str]) -> bool:
    by_company = not shock.companies or (bool(companies) and set(companies) <= set(shock.companies))
    by_month = not shock.months or (bool(months) and set(months) <= set(shock.months))
    return by_company and by_month


def _overlaps(shock: DriverShock, companies: list[str], months: list[str]) -> bool:
    by_company = not shock.companies or not companies or bool(set(companies) & set(shock.companies))
    by_month = not shock.months or not months or bool(set(months) & set(shock.months))
    return by_company and by_month


def _label(companies: list[str], months: list[str]) -> str:
    who = ", ".join(companies) if companies else "all companies"
    when = "all months" if not months else months[0][:7] if len(months) == 1 else f"{months[0][:7]} to {months[-1][:7]}"
    return f"{who}, {when}"


# ---------------------------------------------------------------------------
# The request record
# ---------------------------------------------------------------------------
def create(draft: Draft, question: str, evidence: dict[str, Any], who: Principal) -> str:
    """Store a draft as a PROPOSED request, as the planner who asked for it."""
    with engine().begin() as conn:
        set_actor(conn, who.user_id)
        request_id = conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.reforecast_request "
                "  (source_plan_version_id, driver_code, from_value, to_value, companies, period_months, scope_label, "
                "   question, evidence, requested_by) "
                f"SELECT plan_version_id, :driver, :from_value, :to_value, :companies, CAST(:months AS date[]), :label, "
                "       :question, CAST(:evidence AS jsonb), :who "
                f"FROM {SCHEMA}.plan_version WHERE plan_version_code = :plan "
                "RETURNING request_id::text"
            ),
            {"driver": draft.driver_code, "from_value": draft.from_value, "to_value": draft.to_value,
             "companies": draft.companies, "months": draft.months, "label": draft.scope_label,
             "question": question, "evidence": json.dumps(evidence, default=str), "who": who.user_id,
             "plan": draft.plan_version_code},
        ).scalar_one()
        record(conn, who.user_id, "reforecast_request", request_id, "PROPOSED", draft.as_dict())
    return request_id


_SELECT = (
    "SELECT r.request_id::text, v.plan_version_code AS plan_version_code, r.driver_code, r.from_value, r.to_value, "
    "       r.companies, r.period_months, r.scope_label, r.question, r.evidence, r.requested_by, r.state, "
    "       r.controller_decided_by, r.controller_decided_at, r.controller_comment, r.run_id, r.outcome_detail, "
    "       r.created_at, r.updated_at, "
    # The version the request's run drafted into: from its covenant checks once
    # submitted, before that the successor created while its run was open (a
    # plan has one run at a time, so that successor can only be this run's)
    f"      COALESCE((SELECT t.plan_version_code FROM {SCHEMA}.covenant_check c "
    f"                JOIN {SCHEMA}.plan_version t ON t.plan_version_id = c.plan_version_id "
    "                 WHERE c.request_id = r.request_id LIMIT 1), "
    f"               (SELECT t.plan_version_code FROM {SCHEMA}.recompute_run rr "
    f"                JOIN {SCHEMA}.plan_version t ON t.supersedes_plan_version_id = rr.plan_version_id "
    "                 WHERE rr.run_id = r.run_id AND t.created_at >= rr.started_at "
    "                   AND (rr.ended_at IS NULL OR t.created_at <= rr.ended_at) "
    "                 ORDER BY t.created_at LIMIT 1)) AS version_code "
    f"FROM {SCHEMA}.reforecast_request r JOIN {SCHEMA}.plan_version v ON v.plan_version_id = r.source_plan_version_id "
)


def list_requests(state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where, params = "", {"limit": limit}
    if state:
        where, params["state"] = "WHERE r.state = :state ", state
    with engine().connect() as conn:
        rows = conn.execute(text(_SELECT + where + "ORDER BY r.created_at DESC LIMIT :limit"), params).mappings().all()
    return [_shape(row) for row in rows]


def load(request_id: str) -> dict[str, Any]:
    with engine().connect() as conn:
        row = conn.execute(text(_SELECT + "WHERE r.request_id = CAST(:id AS uuid)"), {"id": request_id}).mappings().first()
        if row is None:
            raise Refused(f"no re-forecast request {request_id}")
        checks = conn.execute(
            text(
                f"SELECT c.rule_code, k.description, c.scenario_code, c.scope, c.metric, c.measure, c.before_value, "
                "       c.after_value, c.measured_value, c.comparator, c.threshold, c.passed, c.revision, "
                "       v.plan_version_code AS target_version_code, c.checked_at "
                f"FROM {SCHEMA}.covenant_check c JOIN {SCHEMA}.covenant_rule k USING (rule_code) "
                f"JOIN {SCHEMA}.plan_version v ON v.plan_version_id = c.plan_version_id "
                "WHERE c.request_id = CAST(:id AS uuid) ORDER BY c.rule_code, c.scenario_code"
            ),
            {"id": request_id},
        ).mappings().all()
    out = _shape(row)
    out["covenant_checks"] = [dict(c) for c in checks]
    out["target_version_code"] = checks[0]["target_version_code"] if checks else None
    return out


def _shape(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["period_months"] = [m.isoformat() if isinstance(m, date) else str(m) for m in (out.get("period_months") or [])]
    return out


def begin_confirmation(request_id: str, who: Principal, confirmed: bool, comment: str):
    """Open the planner's confirmation. Returns (connection, transaction, request row).

    Confirming starts the recompute; withdrawing closes the request. The
    caller starts the workflow while the transaction is still open, so a
    failed start leaves the request PROPOSED, and commits only once the run
    exists. The database refuses anyone but the human planner who asked.
    """
    conn = engine().connect()
    tx = conn.begin()
    try:
        set_actor(conn, who.user_id)
        row = conn.execute(
            text(
                f"SELECT r.request_id::text, r.state, r.requested_by, r.driver_code, r.from_value, r.to_value, "
                "       r.companies, r.period_months, v.plan_version_code, v.state AS plan_state "
                f"FROM {SCHEMA}.reforecast_request r JOIN {SCHEMA}.plan_version v "
                "  ON v.plan_version_id = r.source_plan_version_id "
                "WHERE r.request_id = CAST(:id AS uuid) FOR UPDATE OF r"
            ),
            {"id": request_id},
        ).mappings().first()
        if row is None:
            raise Refused(f"no re-forecast request {request_id}")
        if row["state"] != "PROPOSED":
            raise Refused(f"request is {row['state']}; only a PROPOSED request can be confirmed or withdrawn")
        if who.user_id != row["requested_by"]:
            raise Refused("only the planner who asked for this re-forecast confirms or withdraws it")
        if confirmed and row["plan_state"] not in ("APPROVED", "LOCKED", "SUPERSEDED"):
            raise Refused(f"{row['plan_version_code']} is {row['plan_state']}; it can no longer be re-forecast")
        conn.execute(
            text(f"UPDATE {SCHEMA}.reforecast_request SET state = :state WHERE request_id = CAST(:id AS uuid)"),
            {"state": "RUNNING" if confirmed else "CANCELLED", "id": request_id},
        )
        record(conn, who.user_id, "reforecast_request", request_id,
               "CONFIRMED" if confirmed else "WITHDRAWN", {"comment": comment})
        return conn, tx, dict(row)
    except Exception:
        tx.rollback()
        conn.close()
        raise


def bind_run(conn, request_id: str, run_id: str) -> None:
    """Link the request to the run it started, and catch up with the run's state."""
    conn.execute(
        text(f"UPDATE {SCHEMA}.reforecast_request SET run_id = :run WHERE request_id = CAST(:id AS uuid)"),
        {"run": run_id, "id": request_id},
    )
    conn.execute(
        text(
            f"UPDATE {SCHEMA}.reforecast_request SET state = {SCHEMA}.request_state_for_run(run_id) "
            "WHERE request_id = CAST(:id AS uuid) AND run_id IS NOT NULL "
            f"  AND {SCHEMA}.request_state_for_run(run_id) IS NOT NULL "
            f"  AND state IS DISTINCT FROM {SCHEMA}.request_state_for_run(run_id)"
        ),
        {"id": request_id},
    )


def shock_of(row: dict[str, Any]) -> DriverShock:
    return DriverShock(
        str(row["driver_code"]), float(row["from_value"]), float(row["to_value"]),
        sorted(row["companies"] or []),
        sorted(m.isoformat() if isinstance(m, date) else str(m) for m in (row["period_months"] or [])),
    )
