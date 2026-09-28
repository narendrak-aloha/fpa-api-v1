"""Optional Agno construction; importing the core package does not require Agno."""

from __future__ import annotations

from .models import AgentPlan

FINOPSEXPR_GUIDE = """FinOpsExpr grammar (this is not SQL; never write SUM(), FROM, GROUP BY or quotes around names):
SELECT <measure>[ AS alias][, ...] [BY <dimension>, ...]
  [WHERE <dimension> = 'v' | <dimension> IN ('a','b') | <dimension> NOT IN (...) [AND|OR ...]]
  [FOR PERIOD 2026 | 2026-H1 | 2026-Q2 | 2026-04 | 2026-Q1..2026-Q2]
  [AS OF 'YYYY-MM-DDTHH:MM:SS']
  [COMPARE PLAN pv='PV-2026-0001'[, scenario='base'|'stretch'|'downside'] TO ACTUAL [BRIDGE]]
  [LIMIT n]
Measures are aggregated by the compiler. Time functions: YOY(measure), PRIOR(measure, n), ROLLING(expr, n).
Semi-additive measures (headcount) need a single closing period. AS OF selects the sealed ledger vintage, including for plan comparisons.
Actuals cover 2025-2026; plan PV-2026-0001 covers 2026 only. Call list_metrics and list_dimensions for valid names.
Examples, as intent -> query. Match the question to the closest pattern; the intent line is a comment, never part of dsl.
# plain figure for a slice
SELECT services_revenue BY practice FOR PERIOD 2026-Q2
# a gap and what drove it (price, volume, mix, fx, rate, efficiency legs): BRIDGE, never separate queries
SELECT services_revenue BY practice, geo_country WHERE geo_country = 'PL' AND engine = 'Services' FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE
# what the books said at an earlier close, before later restatements: AS OF
SELECT delivery_cost BY company, practice WHERE geo_region = 'EMEA' FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00'
# ratio measures; the compiler recomputes them from their components, never averages them
SELECT utilisation, gross_margin_pct BY practice WHERE geo_country = 'PL' AND delivery_shore != 'Offshore' FOR PERIOD 2026-Q2 LIMIT 50
# growth against the same period last year
SELECT YOY(services_revenue) BY practice WHERE engine = 'Services' FOR PERIOD 2026-Q1
# intercompany trade, by entity and account
SELECT services_revenue, subcontractor_cost BY company, account WHERE intercompany_flag = 'Yes' FOR PERIOD 2026-H1
# ranking entities on a cost, several values on one dimension
SELECT subcontractor_cost BY company, geo_region WHERE geo_region IN ('EMEA', 'AMER') FOR PERIOD 2026-H1 LIMIT 20
# trailing average across a period range, against a named scenario
SELECT ROLLING(bookings, 3) BY practice WHERE practice = 'Data Platform' FOR PERIOD 2026-Q1..2026-Q4 COMPARE PLAN pv='PV-2026-0001', scenario='downside' TO ACTUAL
# splitting a cost by how and by whom it was delivered
SELECT delivery_cost BY delivery_shore, grade WHERE geo_region = 'EMEA' FOR PERIOD 2026-Q2
# actual against a non-base scenario
SELECT services_revenue BY customer WHERE engine = 'Services' FOR PERIOD 2026-H1 COMPARE PLAN pv='PV-2026-0001', scenario='stretch' TO ACTUAL LIMIT 25
# excluding values on a dimension; the fx leg of a BRIDGE is where currency movement lands
SELECT services_revenue, gross_margin BY company, geo_country WHERE geo_country NOT IN ('US') FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE
Amounts are in functional currency; there is no currency dimension and no reporting-currency measure, so a question
asking to restate into another currency is out of scope. A question about currency *impact* is the BRIDGE fx leg."""


REFORECAST_GUIDE = """

Re-forecast requests: when the user asks to change a driver's value and re-run or re-forecast the plan
(e.g. "drop Poland utilisation to 74% and re-run the second half"), call propose_reforecast once with
driver_code (e.g. utilisation), country as a code (Poland -> PL) and period in FinOpsExpr form
(second half of 2026 -> 2026-H2, third quarter -> 2026-Q3), plus exactly one of:
- to_value, when the user names the new value ("to 74%" -> to_value=0.74; ratios are fractions).
- by_amount, when the user names a change in the driver's own units. On a ratio driver "increase by 5%"
  means five percentage points: by_amount=0.05. "cut by 2 points" is by_amount=-0.02.
- by_percent, when the user asks for a relative change ("5% higher than it is now" -> by_percent=5).
You do not know the driver's current value, so never turn a change into a new value yourself: pass
by_amount or by_percent and let the desk resolve it against the plan.
If it returns errors, fix what they name and call it again, or explain the problem without a draft.
Then write a dsl that shows the current figures for that slice, for example
SELECT services_revenue, delivery_cost, gross_margin_pct WHERE geo_country = 'PL' FOR PERIOD 2026-H1,
and return an AgentPlan with proposed_reforecast=true. In explanation, say what was drafted using only the
from_value and to_value the draft came back with and the query's rows, never a value you worked out,
and that nothing changes until the planner confirms it; after that the recomputed lines are submitted,
checked against the covenants, approved by a controller and locked by the CFO.
Never claim the re-forecast has run."""


def build_agno_team(model=None, toolset=None, *, disclosure_writer=None, single=False):
    # Agno is optional. Keep model construction in this adapter so the core
    # planner remains usable offline and tests can inject deterministic plans.
    """Build the NL interpreter/DSL critic/formatter team.

    Agno is intentionally optional. The returned team has no tools capable of
    SQL generation, filesystem access, or database access.
    """
    try:
        from agno.agent import Agent
        from agno.team.team import Team
    except ImportError as exc:
        raise RuntimeError("Agno is optional; install the project's agno extra to build the team") from exc

    from .security import AgentBoundary, persist_disclosure

    if toolset is None or not toolset.scope.allowed_companies:
        raise ValueError("an authenticated scoped toolset is required")
    boundary = AgentBoundary(toolset, writer=disclosure_writer or persist_disclosure)
    if model is None:
        raise ValueError("an explicit model is required for the egress boundary")
    model = boundary.protect_model(model)
    controls = dict(pre_hooks=[boundary.pii, boundary.injection, boundary.pre],
                    post_hooks=[boundary.post], tool_hooks=[boundary.tool])
    common = (
        "You are a FinOpsExpr planner. Output only a typed AgentPlan. "
        "For queries the dsl field must contain FinOpsExpr, never SQL, Python, or raw queries. For driver proposals call propose_driver; after confirmation set proposed_driver=true and leave dsl empty. "
        "A nonempty dsl value must be plain text beginning with SELECT: no square brackets, JSON, Markdown fences, or commentary inside dsl. "
        "Use only metrics and dimensions from the supplied planning registry. "
        "Never reveal personal employee data. Driver changes must use propose_driver, which pauses for human confirmation; never activate a driver. "
        "Every number in explanation must be copied exactly from rows returned by run_finops_query; otherwise use no digits. "
        "If the question cannot be answered from the FP&A cube (for example sports, weather, news, general knowledge, or a metric that does not exist), "
        "set out_of_scope=true, leave dsl empty, do not call run_finops_query, and briefly say what you can answer instead. "
        "Never invent a placeholder query. "
        "Always write and run the dsl for what was asked, even if it names companies or countries the caller may not see: "
        "the compiler limits every result to the caller's entity scope and you cannot change it. "
        "Do not say in explanation which entities, countries or scope the figures cover, and do not mention the "
        "caller's scope at all: you cannot see the entity list or the filters the compiler applied, and a "
        "country-filtered figure called the caller's whole scope is a much larger number than the one returned. "
        "Write countries by name, never by code: say Germany, not DE; Poland, not PL; the United Kingdom, not UK. "
        "Never quote a dimension filter back as written (no \"geo_country = DE\"); say what it means in words. "
        "Coverage is stated for you in the assumptions. Describe only what the figures are, and never describe "
        "anything as zero, empty or having no activity unless a returned row says so.\n\n"
        + FINOPSEXPR_GUIDE
    )
    from agno.tools.function import Function
    proposal_tool = Function.from_callable(toolset.propose_driver)
    proposal_tool.requires_confirmation = True
    # The leader keeps the proposal tools, whose confirmation has to surface on
    # the team run for the human pause to work, and holds no read tool: a
    # question it cannot answer alone is one it has to delegate, which is what
    # puts a named member in the trace.
    proposal_tools = [proposal_tool]
    tool_functions = [toolset.list_metrics, toolset.list_dimensions, toolset.run_finops_query, proposal_tool]
    if toolset.reforecast_desk is not None:
        # Only a human planner's team can draft a re-forecast; for anyone else
        # the tool does not exist, so the model cannot even try.
        tool_functions.append(toolset.propose_reforecast)
        proposal_tools.append(toolset.propose_reforecast)
        common += REFORECAST_GUIDE
    else:
        common += "\n\nIf asked to change a driver or re-run/re-forecast the plan, say that only a planner can request a re-forecast; set out_of_scope=true."
    if single:
        # One agent with the team's guardrails, hooks, tools and instructions:
        # the baseline the team's token and latency cost is measured against
        # (scripts/measure_team_cost.py). The API always builds the team.
        return Agent(**controls, id="fpa-single", tool_call_limit=6, name="SingleAgent", model=model, tools=tool_functions, output_schema=AgentPlan, instructions=[common, "Translate analytical intent into a minimal query DSL plan and return an AgentPlan. It must have a non-empty dsl field, proposed_driver=true after a proposal, proposed_reforecast=true after a propose_reforecast draft, or out_of_scope=true with an empty dsl when the question is not about the FP&A cube. Execution and the final AgentFPAResponse are handled by the orchestrator."])
    interpreter = Agent(**controls, id="fpa-query", tool_call_limit=6, name="QueryAgent", model=model, instructions=[common, "Translate analytical intent into a minimal query DSL plan."], tools=tool_functions, output_schema=AgentPlan)
    critic = Agent(**controls, id="fpa-variance", tool_call_limit=6, name="VarianceAgent", model=model, instructions=[common, "Validate bridge and plan-versus-actual requests; do not explain unexecuted numbers."], tools=tool_functions, output_schema=AgentPlan)
    planner = Agent(**controls, id="fpa-planning", tool_call_limit=6, name="PlanningAgent", model=model, instructions=[common, "Draft driver expressions only through DRAFT proposals, and driver value changes only through propose_reforecast."], tools=tool_functions, output_schema=AgentPlan)
    return Team(**controls, tools=proposal_tools, id="fpa-team", tool_call_limit=6, name="FPATeam", mode="coordinate", model=model, members=[interpreter, critic, planner], output_schema=AgentPlan, instructions=[common, "Coordinate the specialists and return an AgentPlan. It must have a non-empty dsl field, proposed_driver=true after a proposal, proposed_reforecast=true after a propose_reforecast draft, or out_of_scope=true with an empty dsl when the question is not about the FP&A cube. Execution and the final AgentFPAResponse are handled by the orchestrator."])
