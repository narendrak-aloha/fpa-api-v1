"""The bridge on the real Poland Q2 cut, persisted to the real governance store.

The seed buries a story in Poland's Q2: rate erosion, a cost blowout, a shift
in the grade pyramid and a currency move, all at once. A correct bridge
separates them; a subtly wrong one still produces plausible numbers. These
tests check the former in the only way that means anything -- against the data.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from db.config import database_url
from fpa_project.config import clickhouse as clickhouse_settings
from fpa_project.dsl.compiler import SecurityContext

pytestmark = pytest.mark.integration


def _cube():
    import clickhouse_connect

    s = clickhouse_settings()
    return clickhouse_connect.get_client(host=s.host, port=s.port, username=s.user, password=s.password, connect_timeout=2)


try:
    _cube().query("SELECT 1")
    create_engine(database_url(), connect_args={"connect_timeout": 2}).connect().close()
except Exception:  # noqa: BLE001
    pytest.skip("needs the stack: run `make docker-local-run-d`", allow_module_level=True)

from fpa_project.agent_team.tools import clickhouse_executor  # noqa: E402
from fpa_project.bridge_service import StatusRefused, citations_for, load_report, run_bridge, set_status  # noqa: E402
from fpa_project.identities import AGENT, CFO, CONTROLLER, PLANNER, SERVICE  # noqa: E402

POLAND = (
    "SELECT services_revenue BY practice, grade WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 "
    "COMPARE PLAN pv='PV-2026-0001', scenario='base' TO ACTUAL BRIDGE"
)


@pytest.fixture(scope="module")
def executor():
    return clickhouse_executor(_cube())


@pytest.fixture(scope="module")
def poland(executor):
    return run_bridge(POLAND, executor, SecurityContext(), actor=CONTROLLER, persist=True)


@pytest.fixture(autouse=True, scope="module")
def cleanup():
    yield
    with create_engine(database_url()).begin() as conn:
        conn.execute(text("DELETE FROM fpa_governance.variance_report WHERE created_by IN ('e2a112d82c994c3ea08f65f1b78b4056', 'fd40d13f7e8f485ea53c6a389a939f99', '1dbad3bde4364851ac7e421ecd5b967a') AND dsl LIKE '%geo_country = ''PL''%'"))


def test_the_residual_is_inside_tolerance_at_every_level(poland):
    for node in poland.result.walk():
        assert abs(node.residual) < node.tol, f"breaks at {node.path}: {node.residual} >= {node.tol}"
    # The seeded cut matches roughly 400 lines (402 or 405 depending on the cube build); the
    # exact figure is the seed's business, the tie is ours.
    assert 380 <= poland.result.root.line_count <= 420


def test_volume_plus_mix_is_the_quantity_variance(poland):
    root = poland.result.root
    assert abs((root.volume + root.mix) - root.quantity_variance) < Decimal("1e-6")


def test_practice_mix_and_grade_mix_are_separate_non_zero_legs(poland):
    by_level = poland.result.mix_by_level()
    assert by_level["practice"] != 0
    assert by_level["grade"] != 0
    assert abs(sum(by_level.values()) - poland.result.root.mix) < Decimal("1e-6")


def test_every_leg_is_exercised_on_the_poland_cut(poland):
    """Price, volume, mix and FX all move on this cut, and FX is the real sum.

    A leg that is *zero* would mean it is not being computed at all, which is
    what the non-zero assertions guard.

    FX is checked by recomputing it, not by pinning its size. An earlier
    version asserted the leg netted under 1% of the gap, which held only for
    one particular seeded rate curve: the seeder phased its FX drift with
    hash(ccy), Python salts string hashing per process, and so every cube
    rebuild moved the rates and eventually broke the threshold. The identity
    below -- the leg equals the actual local amount times (real rate minus
    assumed rate), summed over the same matched lines -- is what the leg
    *means*, and holds whatever the rates happen to be.
    """
    root = poland.result.root
    for leg in ("price", "volume", "mix", "fx"):
        assert getattr(root, leg) != 0, f"the {leg} leg is not being computed"
    for leg in ("price", "volume", "mix"):
        assert abs(getattr(root, leg)) > Decimal("1000"), f"{leg} is suspiciously small: {getattr(root, leg)}"
    recomputed = sum(
        Decimal(str(c["actual_amount"])) * (Decimal(str(c["actual_fx"])) - Decimal(str(c["plan_fx"])))
        for c in poland.citations
    )
    assert root.fx == recomputed, "the FX leg is not the currency movement on the matched lines"
    # It is a miss.
    assert root.gap < 0


def test_the_report_names_its_vintage_and_persists_with_cited_lines(poland):
    assert poland.vintage == 2 and "restatement" in poland.vintage_note
    stored = load_report(poland.report_id)
    assert stored is not None
    assert stored["as_of_vintage"] == 2 and stored["vintage_closed_at"] is not None
    assert stored["rollup"] == ["practice", "grade"]
    assert stored["ties"] is True
    assert len(stored["lines"]) == len(list(poland.result.walk()))
    # The stored root ties to the cent, by the check constraint and by arithmetic.
    root = stored["lines"][0]
    legs = sum(root[k] for k in ("price_variance", "volume_variance", "mix_variance", "fx_variance", "rate_variance", "efficiency_variance", "residual"))
    assert legs == root["gap"]


def test_drill_through_cites_the_cube_rows_with_the_vintage(poland):
    everything = citations_for(poland.report_id, [])
    assert len(everything) == poland.result.root.line_count
    one_practice = citations_for(poland.report_id, ["Cloud Migration"])
    assert 0 < len(one_practice) < len(everything)
    assert all(row["vintage"] == 2 for row in one_practice)
    assert all(len(row["dim_signature_hash"]) == 16 for row in one_practice)


@pytest.mark.parametrize("leg", ["price", "volume", "mix", "fx"])
def test_clicking_a_leg_shows_the_rows_that_add_up_to_it(poland, leg):
    """Drill-through by leg: every cited row carries its share, and the shares are the leg."""
    for node in (poland.result.root, poland.result.root.children[0]):
        rows = citations_for(poland.report_id, list(node.path), leg=leg)
        assert len(rows) == node.line_count
        assert all(row["contribution"] is not None and row["vintage"] == 2 for row in rows)
        assert all(row["plan_quantity"] is not None and row["actual_unit_price"] is not None for row in rows)
        total = sum(row["contribution"] for row in rows)
        # Each share is rounded to the cent, so the sum may drift by a cent a row.
        assert abs(total - getattr(node, leg)) <= Decimal("0.01") * node.line_count, (leg, node.path, total)
        # Largest contribution first: the row that explains most of the leg is on top.
        assert abs(rows[0]["contribution"]) >= abs(rows[-1]["contribution"])


def test_persisted_report_requires_all_cited_companies(poland):
    from fpa_project.bridge_service import report_in_scope

    companies = frozenset(row["company_code"] for row in citations_for(poland.report_id, []))
    assert companies
    assert report_in_scope(poland.report_id, companies)
    assert not report_in_scope(poland.report_id, companies - {next(iter(companies))})
    assert not report_in_scope(poland.report_id, frozenset({"RTUS1"}))


def test_the_same_quarter_at_the_july_close_is_a_different_report(executor):
    july = run_bridge(POLAND.replace("FOR PERIOD 2026-Q2 ", "FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00' "), executor, SecurityContext(), actor=CONTROLLER, persist=False)
    august = run_bridge(POLAND, executor, SecurityContext(), actor=CONTROLLER, persist=False)
    assert july.vintage == 1 and august.vintage == 2
    assert all(node.ties for node in july.result.walk())
    # Revenue was not restated in August, so the two revenue bridges agree;
    # the point is that each one says which books it read.
    assert july.vintage_closed_at != august.vintage_closed_at


def test_a_cost_bridge_has_rate_and_efficiency_and_ties(executor):
    report = run_bridge(
        "SELECT delivery_cost BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 "
        "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE",
        executor, SecurityContext(), actor=CONTROLLER, persist=False,
    )
    root = report.result.root
    assert root.rate != 0 and root.efficiency != 0
    assert root.price == 0 and root.volume == 0
    assert all(node.ties for node in report.result.walk())


def test_a_margin_bridge_carries_both_sides_and_ties(executor):
    report = run_bridge(
        "SELECT gross_margin BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 "
        "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE",
        executor, SecurityContext(), actor=CONTROLLER, persist=False,
    )
    root = report.result.root
    assert root.price != 0 and root.rate != 0 and root.efficiency != 0
    assert all(node.ties for node in report.result.walk())


def test_scope_decides_what_the_bridge_may_run_over(executor, poland):
    from fpa_project.bridge_service import BridgeError

    # Every matched Poland revenue line is RTPL1's, so that scope sees the
    # whole cut and any other entity's scope sees none of it.
    scoped = run_bridge(POLAND, executor, SecurityContext(frozenset({"RTPL1"})), actor=CONTROLLER, persist=False)
    assert scoped.result.root.line_count == poland.result.root.line_count
    with pytest.raises(BridgeError, match="nothing to bridge"):
        run_bridge(POLAND, executor, SecurityContext(frozenset({"RTUS1"})), actor=CONTROLLER, persist=False)


def test_a_material_gap_is_escalated_and_cannot_be_downgraded(executor):
    report = run_bridge(POLAND, executor, SecurityContext(), actor=CONTROLLER, materiality=Decimal("1000"))
    assert report.status == "ESCALATED"
    with pytest.raises(StatusRefused, match="cannot be downgraded"):
        set_status(report.report_id, "OPEN", CONTROLLER)


def test_an_agent_may_investigate_but_only_a_human_closes(executor):
    report = run_bridge(POLAND, executor, SecurityContext(), actor=CONTROLLER, materiality=Decimal("1e12"))
    assert report.status == "OPEN"
    assert set_status(report.report_id, "INVESTIGATING", AGENT)["status"] == "INVESTIGATING"
    with pytest.raises(StatusRefused, match="only a human closes"):
        set_status(report.report_id, "CLOSED", AGENT)
    with pytest.raises(StatusRefused, match="only a human closes"):
        set_status(report.report_id, "CLOSED", SERVICE)
    with pytest.raises(StatusRefused, match="controller or cfo"):
        set_status(report.report_id, "CLOSED", PLANNER)
    closed = set_status(report.report_id, "CLOSED", CFO)
    assert closed["status"] == "CLOSED" and closed["closed_by"] == CFO
    with pytest.raises(StatusRefused, match="cannot be reopened"):
        set_status(report.report_id, "OPEN", CFO)


def test_a_report_that_reads_no_ledger_close_persists_with_a_null_vintage():
    """Found live: the re-forecast's baseline-vs-forecast report passed vintage=0,
    which the as_of_vintage foreign key refuses, failing every approved run at
    its last step. A report that reads no close has no vintage: NULL."""
    from dataclasses import replace as dc_replace

    from fpa_project.bridge_service import _persist
    from fpa_project.dsl.bridge import BridgeLine, decompose

    line = BridgeLine((), Decimal(1), Decimal(2), Decimal(10), Decimal(10), Decimal(1), Decimal(1),
                      key=("RTPL1", "2026-04-01", "41000", "0123456789abcdef"),
                      plan_amount=Decimal(10), actual_amount=Decimal(20))
    report = run_bridge(POLAND, clickhouse_executor(_cube()), SecurityContext(), actor=CONTROLLER, persist=False)
    report = dc_replace(report, result=decompose([line]), vintage=None, vintage_closed_at=None, dsl="POLAND null-vintage probe",
                        citations=[{"company_code": "RTPL1", "period_month": "2026-04-01", "account_code": "41000",
                                    "dim_signature_hash": "0123456789abcdef", "plan_amount": Decimal(10),
                                    "actual_amount": Decimal(20), "path": ()}])
    report_id = _persist(report, CONTROLLER)
    try:
        assert load_report(report_id)["as_of_vintage"] is None
    finally:
        with create_engine(database_url()).begin() as conn:
            conn.execute(text("DELETE FROM fpa_governance.variance_report WHERE variance_report_id = CAST(:i AS uuid)"), {"i": report_id})
