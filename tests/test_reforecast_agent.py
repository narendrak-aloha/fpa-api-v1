"""The agent side of a question-driven re-forecast, with a fake desk and no database."""

from __future__ import annotations

from fpa_project.agent_team import FPAOrchestrator, FPATools, PlanningRequest, UserScope
from fpa_project.agent_team.models import AgentPlan
from fpa_project.reforecast_requests import Draft
from fpa_project.identities import PLANNER

SCOPE = UserScope(user_id=PLANNER, allowed_companies=frozenset({"RTPL1", "RTPL2", "RTPL3"}))
H2 = ["2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"]


class Desk:
    """Resolves like the real desk, from fixed data."""

    calls: list = []

    def resolve(self, driver_code, to_value=None, country="", companies=(), period="", plan_version_code="",
                *, by_amount=None, by_percent=None):
        self.calls.append((driver_code, to_value, country, period, by_amount, by_percent))
        if driver_code != "utilisation":
            raise ValueError(f"{driver_code!r} is not an active driver")
        if len([v for v in (to_value, by_amount, by_percent) if v is not None]) != 1:
            raise ValueError("give exactly one of to_value, by_amount or by_percent")
        if to_value is not None and to_value > 1:
            raise ValueError(f"utilisation is a ratio (currently 0.75); pass {to_value / 100:g} for {to_value:g}%")
        if to_value is None:
            to_value = round(0.75 + by_amount if by_amount is not None else 0.75 * (1 + by_percent / 100), 10)
        return Draft("PV-2026-0001", "utilisation", "Delivery utilisation", 0.75, to_value,
                     ["RTPL1", "RTPL2", "RTPL3"] if country == "PL" else [], H2 if period == "2026-H2" else [],
                     "RTPL1, RTPL2, RTPL3, 2026-07 to 2026-12")


def rows_executor(sql, params):
    return [{"services_revenue": 5399000.0, "gross_margin_pct": 0.3349}]


def test_a_planner_gets_a_validated_draft():
    tools = FPATools(SCOPE, reforecast_desk=Desk())
    proposal = tools.propose_reforecast("utilisation", 0.74, country="PL", period="2026-H2")
    assert proposal.status == "DRAFT"
    assert (proposal.from_value, proposal.to_value) == (0.75, 0.74)
    assert proposal.companies == ["RTPL1", "RTPL2", "RTPL3"] and proposal.months == H2


def test_a_change_reaches_the_desk_as_a_change_not_a_guessed_target():
    """The agent never does the arithmetic: "up 5 points" goes down as by_amount."""
    tools = FPATools(SCOPE, reforecast_desk=Desk())
    proposal = tools.propose_reforecast("utilisation", country="PL", period="2026-H2", by_amount=0.05)
    assert proposal.status == "DRAFT"
    assert (proposal.from_value, proposal.to_value) == (0.75, 0.80)


def test_the_desks_refusal_comes_back_as_a_reason_not_a_draft():
    tools = FPATools(SCOPE, reforecast_desk=Desk())
    proposal = tools.propose_reforecast("utilisation", 74, country="PL", period="2026-H2")
    assert proposal.status == "INVALID"
    assert "pass 0.74 for 74%" in proposal.errors[0].message


def test_without_a_desk_nobody_can_draft():
    proposal = FPATools(SCOPE).propose_reforecast("utilisation", 0.74)
    assert proposal.status == "INVALID" and proposal.errors[0].code == "NOT_A_PLANNER"


def test_the_orchestrator_returns_the_tool_draft_with_its_evidence():
    tools = FPATools(SCOPE, executor=rows_executor, reforecast_desk=Desk())
    tools.propose_reforecast("utilisation", 0.74, country="PL", period="2026-H2")
    plan = AgentPlan(
        dsl="SELECT services_revenue, gross_margin_pct WHERE geo_country = 'PL' FOR PERIOD 2026-H1",
        explanation="Drafted utilisation 0.75 to 74% for Poland; revenue now 5399000.",
        proposed_reforecast=True,
    )
    result = FPAOrchestrator(tools).finalize(PlanningRequest(request="drop Poland utilisation to 74%"), plan, plan.explanation)
    assert result.execution_status == "REFORECAST_PROPOSED"
    # The draft is the tool's, not the model's prose
    assert result.reforecast_draft["to_value"] == 0.74 and result.reforecast_draft["months"] == H2
    assert result.cited_data_rows
    assert any("you confirm the draft" in a for a in result.assumptions)
    assert any(a.startswith("Figures cover") and "RTPL" in a for a in result.assumptions)


def test_a_narrative_number_outside_the_draft_and_rows_is_refused():
    tools = FPATools(SCOPE, executor=rows_executor, reforecast_desk=Desk())
    tools.propose_reforecast("utilisation", 0.74, country="PL", period="2026-H2")
    plan = AgentPlan(dsl="", explanation="This will cut revenue by 1.3%.", proposed_reforecast=True)
    result = FPAOrchestrator(tools).finalize(PlanningRequest(request="q"), plan, plan.explanation)
    assert result.execution_status == "VALIDATION_ERROR"


def test_no_valid_draft_means_no_proposal():
    tools = FPATools(SCOPE, reforecast_desk=Desk())
    tools.propose_reforecast("heads", 98)
    plan = AgentPlan(dsl="", explanation="", proposed_reforecast=True)
    result = FPAOrchestrator(tools).finalize(PlanningRequest(request="q"), plan, "")
    assert result.execution_status == "VALIDATION_ERROR"
    assert "not an active driver" in result.error_message


def test_the_tool_is_only_on_a_planners_team():
    import pytest

    pytest.importorskip("agno")
    from agno.models.base import Model

    from fpa_project.agent_team.team import build_agno_team

    class Quiet(Model):
        def invoke(self, *a, **k): ...
        async def ainvoke(self, *a, **k): ...
        def invoke_stream(self, *a, **k): yield
        async def ainvoke_stream(self, *a, **k): yield
        def _parse_provider_response(self, r, **k): return r
        def _parse_provider_response_delta(self, r): return r

    def tool_names(tools):
        team = build_agno_team(Quiet(id="q", name="q", provider="test"), tools, disclosure_writer=lambda e: None)
        return {getattr(t, "__name__", getattr(t, "name", "")) for t in team.tools}

    assert "propose_reforecast" in tool_names(FPATools(SCOPE, reforecast_desk=Desk()))
    assert "propose_reforecast" not in tool_names(FPATools(SCOPE))
