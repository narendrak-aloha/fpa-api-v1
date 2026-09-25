"""What a re-forecast does to the plan, for the person about to approve it.

A successor version (``PV-…-R<n>``) is drafted by a re-forecast of the version
it supersedes. Before anyone approves it, this answers the approver's question
with numbers rather than a covenant flag: which drivers moved, why the planner
said they moved them, and what that does to the plan, decomposed into price,
volume and mix with a residual that has to tie.

It reads, and never writes. The pairing is the one ``compute_variance`` uses
once the revision is published: the frozen baseline on the left, the staged
revision on the right, both from the cube and matched on the plan grain. The
decomposition is ``dsl.bridge.decompose``, unchanged. FX is the same pinned
plan rate on both sides, so its leg is zero by construction.

After a publish ``compute_variance`` has also persisted the same comparison as
a variance report; when that report exists its id is returned, so the page can
drill through to the cited rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import text

from fpa_project.bridge_service import DEFAULT_MATERIALITY, BridgeReport, postgres
from fpa_project.dsl.bridge import BridgeLine, decompose
from fpa_project.governance import Refused

SCHEMA = "fpa_governance"
LEVELS = ("account", "company")
SCENARIOS = ("base", "stretch", "downside")

# account, company, period_month, dim_signature_hash,
# baseline quantity, unit_price, amount, staged quantity, unit_price, amount,
# plan fx rate, account_type
Row = Sequence[Any]
RowFetcher = Callable[[str, int, str], Iterable[Row]]

_PAIRED_SQL = (
    "SELECT b.account, b.company, toString(b.period_month), b.dim_signature_hash, "
    "b.quantity, b.unit_price, b.amount_functional, s.quantity, s.unit_price, s.amount_functional, "
    "fx.rate, d.account_type "
    "FROM (SELECT * FROM fpa_cube.fact_plan_line_baseline FINAL "
    "      WHERE plan_version = {pv:String} AND scenario_id = {scenario:String}) b "
    "INNER JOIN (SELECT * FROM fpa_cube.fact_plan_line_staged FINAL "
    "      WHERE plan_version = {pv:String} AND revision = {revision:UInt32} AND scenario_id = {scenario:String}) s "
    "ON b.company = s.company AND b.period_month = s.period_month AND b.account = s.account "
    "AND b.dim_signature_hash = s.dim_signature_hash "
    "LEFT JOIN (SELECT * FROM fpa_cube.dim_fx_plan WHERE plan_version = {pv:String}) fx "
    "ON fx.period_month = b.period_month AND fx.from_currency = b.functional_currency "
    "INNER JOIN fpa_cube.dim_account d ON d.account = b.account "
    "ORDER BY b.account, b.company, b.period_month, b.dim_signature_hash"
)


def cube_rows(source_code: str, revision: int, scenario: str) -> list[Row]:
    """Baseline and staged lines for one revision, paired on the plan grain."""
    from fpa_project.recompute.stores import cube

    return cube().query(
        _PAIRED_SQL, parameters={"pv": source_code, "revision": revision, "scenario": scenario},
    ).result_rows


def bridge_from_rows(rows: Iterable[Row], *, plan_version: str, revision: int, scenario: str) -> dict[str, Any] | None:
    """Decompose paired baseline/staged rows. None when there are none."""
    lines = []
    for account, company, _month, signature, bq, bp, ba, sq, sp, sa, fx, kind in rows:
        rate = Decimal(str(fx or 0))
        if rate <= 0:
            raise Refused(f"no plan FX rate for a {company} line; the impact cannot be put in USD")
        signature = signature.decode() if isinstance(signature, bytes) else str(signature)
        lines.append(BridgeLine(
            (str(account), str(company)),
            Decimal(str(bq)), Decimal(str(sq)), Decimal(str(bp)), Decimal(str(sp)),
            rate, rate, account_type=str(kind), key=(company, _month, account, signature),
            plan_amount=Decimal(str(ba)), actual_amount=Decimal(str(sa)),
        ))
    if not lines:
        return None
    result = decompose(lines, levels=LEVELS)
    report = BridgeReport(
        report_id=None, result=result, vintage=None, vintage_closed_at=None,
        vintage_note=f"Baseline versus re-forecast revision {revision}; not a ledger vintage",
        dsl="", measure="reforecast_change", plan_version=plan_version, scenario=scenario,
        status="ESCALATED" if abs(result.root.gap) >= DEFAULT_MATERIALITY else "OPEN",
        materiality_threshold=DEFAULT_MATERIALITY, citations=[],
    )
    return report.to_dict()


def reforecast_impact(plan_version_code: str, scenario: str = "base", fetch: RowFetcher = cube_rows) -> dict[str, Any]:
    """The shocks, the planner's reasons and the bridge for one successor version."""
    if scenario not in SCENARIOS:
        raise Refused(f"unknown scenario {scenario!r}; expected one of {', '.join(SCENARIOS)}")
    with postgres().connect() as conn:
        version = conn.execute(
            text(
                "SELECT v.plan_version_id, v.state, v.revision, s.plan_version_id AS source_id, "
                "       s.plan_version_code AS source_code, v.requested_by "
                f"FROM {SCHEMA}.plan_version v "
                f"LEFT JOIN {SCHEMA}.plan_version s ON s.plan_version_id = v.supersedes_plan_version_id "
                "WHERE v.plan_version_code = :code"
            ),
            {"code": plan_version_code},
        ).mappings().first()
        if version is None:
            raise Refused(f"no plan version {plan_version_code!r}")
        if version["source_id"] is None:
            raise Refused(f"{plan_version_code} is not a re-forecast; it supersedes nothing, so there is no before to compare")
        publication = conn.execute(
            text(
                f"SELECT state, shocks, workflow_id, created_at FROM {SCHEMA}.plan_publication "
                "WHERE plan_version_id = :source AND revision = :revision "
                "ORDER BY publication_id DESC LIMIT 1"
            ),
            {"source": version["source_id"], "revision": version["revision"]},
        ).mappings().first()
        reasons = conn.execute(
            text(
                f"SELECT occurred_at, actor_user_id, payload FROM {SCHEMA}.audit_event "
                "WHERE entity_type = 'reforecast' AND entity_id = :source AND action = 'REASON' "
                "ORDER BY audit_event_id DESC LIMIT 50"
            ),
            {"source": version["source_code"]},
        ).mappings().all()
        # A question-driven request's reason is the question itself
        asked = conn.execute(
            text(
                f"SELECT DISTINCT r.question, r.requested_by, r.created_at FROM {SCHEMA}.reforecast_request r "
                f"JOIN {SCHEMA}.covenant_check c ON c.request_id = r.request_id "
                "WHERE c.plan_version_id = CAST(:pv AS uuid)"
            ),
            {"pv": str(version["plan_version_id"])},
        ).mappings().all()
        report_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"fpa:forecast:{version['plan_version_id']}:{version['revision']}:{scenario}",
        ))
        persisted = conn.execute(
            text(f"SELECT 1 FROM {SCHEMA}.variance_report WHERE variance_report_id = CAST(:id AS uuid)"),
            {"id": report_id},
        ).first() is not None

    shocks = [list(s) for s in (publication["shocks"] if publication else [])]
    revision = int(version["revision"])
    bridge = bridge_from_rows(
        fetch(version["source_code"], revision, scenario),
        plan_version=plan_version_code, revision=revision, scenario=scenario,
    )
    if bridge is not None and persisted:
        bridge["report_id"] = report_id
    return {
        "plan_version_code": plan_version_code,
        "state": version["state"],
        "source_plan_version_code": version["source_code"],
        "revision": revision,
        "scenario": scenario,
        "requested_by": version["requested_by"],
        "publication_state": publication["state"] if publication else None,
        "shocks": [_shock(s) for s in shocks if len(s) >= 3],
        "reasons": [
            *({"reason": a["question"], "by": a["requested_by"], "at": a["created_at"].isoformat(), "action": "ASKED"}
              for a in asked),
            *(_reason(r) for r in reasons if _covers(shocks, r["payload"])),
        ],
        "bridge": bridge,
        "note": None if bridge else (
            f"no staged lines for revision {revision}: the run has not staged them yet, "
            "or they were discarded when it was rejected, cancelled or failed"
        ),
    }


def _shock(item: list[Any]) -> dict[str, Any]:
    from fpa_project.recompute.models import DriverShock

    shock = DriverShock.from_list(item)
    out = {"driver_code": shock.driver_code, "from_value": shock.from_value, "to_value": shock.to_value,
           "companies": shock.companies, "months": shock.months}
    if shock.scoped:
        who = ", ".join(shock.companies) if shock.companies else "all companies"
        when = (f"{shock.months[0][:7]} to {shock.months[-1][:7]}" if len(shock.months) > 1
                else shock.months[0][:7] if shock.months else "all months")
        out["scope_label"] = f"{who}, {when}"
    return out


def _covers(shocks: list[list[Any]], payload: dict[str, Any]) -> bool:
    """A reason belongs here when every shock it was given for is in this revision."""
    asked = [list(s)[:3] for s in payload.get("shocks") or []]
    have = [list(s)[:3] for s in shocks]
    return bool(asked) and all(s in have for s in asked)


def _reason(row: Any) -> dict[str, Any]:
    payload = row["payload"] or {}
    return {
        "reason": payload.get("reason", ""),
        "by": row["actor_user_id"],
        "at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
        "shocks": payload.get("shocks", []),
        "action": payload.get("action"),
    }
