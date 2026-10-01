from types import SimpleNamespace
from datetime import datetime

import pytest
from agno.exceptions import OutputCheckError
from agno.run.base import RunContext
from agno.run.agent import RunInput

from fpa_project.agent_team.historical import resolve_historical
from fpa_project.agent_team.models import PlanningRequest, UserScope
from fpa_project.agent_team.planner import FPAOrchestrator
from fpa_project.agent_team.security import AgentBoundary
from fpa_project.agent_team.tools import FPATools

JULY = '2026-07-05T18:00:00'
QUESTION = 'Why did Poland miss its number in Q2 2026 as of the July close?'
DSL = 'SELECT services_revenue FOR PERIOD 2026-Q2'
SCOPE = UserScope(user_id='test', allowed_companies=frozenset({'RTPL1'}))


def executor_with_log(seen, closes=None):
    def execute(sql, params):
        seen.append((sql, params))
        if sql.startswith('SELECT vintage, closed_at, note'):
            return closes if closes is not None else [{'vintage': 1, 'closed_at': JULY, 'note': 'original Q2 close'}]
        return [{'services_revenue': 100}]
    return execute


def test_named_close_uses_parameterized_metadata_lookup():
    seen = []
    constraint = resolve_historical(QUESTION, executor_with_log(seen))
    assert constraint.vintage == 1
    assert constraint.as_of == JULY
    sql, params = seen[0]
    assert '{start:DateTime}' in sql and '2026-07' not in sql
    assert params == {'start': '2026-07-01T00:00:00', 'end': '2026-08-01T00:00:00'}
    constraint.validate(DSL + f" AS OF '{JULY}'")
    with pytest.raises(ValueError, match='omitted or changed'):
        constraint.validate(DSL)
    with pytest.raises(ValueError, match='omitted or changed'):
        constraint.validate(DSL + " AS OF '2026-08-12T09:30:00'")


@pytest.mark.parametrize('text', [
    'revenue as of July close',
    'revenue in 2025 and 2026 as of July close',
    'revenue as of last close',
    'revenue in 2026 before July close',
    'compare revenue as of 2026-07-05T18:00:00 and as of 2026-08-12T09:30:00',
    'compare July 2026 close with revenue as of 2026-08-12T09:30:00',
    'revenue as of 2026-07-05T18:00:00+25:00',
    'revenue as of 2026-07-05T18:00:00.123',
    'revenue as of 2026-07-05T18:00:00garbage',
])
def test_unclear_close_requires_clarification(text):
    with pytest.raises(ValueError):
        resolve_historical(text, executor_with_log([]))


@pytest.mark.parametrize('closes', [[], [
    {'vintage': 1, 'closed_at': JULY}, {'vintage': 2, 'closed_at': '2026-07-30T18:00:00'}]])
def test_missing_or_ambiguous_month_never_falls_back(closes):
    with pytest.raises(ValueError, match='uniquely verified'):
        resolve_historical(QUESTION, executor_with_log([], closes))


def test_current_question_needs_no_lookup():
    assert resolve_historical('Why did Poland miss its number in Q2 2026?', None) is None


def test_database_datetime_close_is_normalized_to_dsl_timestamp():
    constraint = resolve_historical(QUESTION, executor_with_log([], [
        {'vintage': 1, 'closed_at': datetime(2026, 7, 5, 18), 'note': 'July close'}]))
    assert constraint.as_of == JULY
    constraint.validate(DSL + f" AS OF '{JULY}'")


def test_explicit_timestamp_and_timezone_are_preserved():
    constraint = resolve_historical('revenue as of 2026-07-05T20:00:00+02:00', executor_with_log([]))
    assert constraint.as_of == JULY
    constraint.validate(DSL + f" AS OF '{JULY}'")


def test_final_gate_blocks_omitted_historical_constraint_before_fact_read():
    seen = []
    tools = FPATools(SCOPE, executor=executor_with_log(seen))
    result = FPAOrchestrator(tools).finalize(PlanningRequest(request=QUESTION), {'dsl': DSL})
    assert result.execution_status == 'VALIDATION_ERROR'
    assert 'requires AS OF' in result.error_message
    assert all(sql.startswith('SELECT vintage, closed_at, note') for sql, _ in seen)


def test_member_hook_blocks_missing_or_wrong_close_before_tool_execution():
    tools = FPATools(SCOPE)
    tools.historical_constraint = resolve_historical(QUESTION, executor_with_log([]))
    boundary = AgentBoundary(tools, writer=lambda event: None)
    context = RunContext(run_id='r', session_id='s', dependencies={'scope': SCOPE.model_dump()})
    member_input = RunInput('Investigate the revenue miss')
    boundary.pre(member_input, context)
    assert f"AS OF '{JULY}'" in member_input.input_content
    calls = []
    for dsl in (DSL, DSL + " AS OF '2026-08-12T09:30:00'"):
        with pytest.raises(OutputCheckError):
            boundary.tool('run_finops_query', lambda **args: calls.append(args), {'dsl': dsl}, context)
    assert calls == []
    boundary.tool('run_finops_query', lambda **args: {'rows': []}, {'dsl': DSL + f" AS OF '{JULY}'"}, context)
    with pytest.raises(OutputCheckError):
        boundary.post(SimpleNamespace(content={'dsl': DSL, 'explanation': ''}))


def test_one_repair_gets_verified_close_and_executes_only_historical_query():
    seen, prompts = [], []
    class Team:
        members = []
        def run(self, prompt, **kwargs):
            prompts.append(prompt)
            return SimpleNamespace(content={'dsl': DSL if len(prompts) == 1 else DSL + f" AS OF '{JULY}'"})
    tools = FPATools(SCOPE, executor=executor_with_log(seen))
    result = FPAOrchestrator(tools).run_with_team(Team(), PlanningRequest(request=QUESTION))
    assert result.execution_status == 'SUCCESS'
    assert len(prompts) == 2
    assert all(f"AS OF '{JULY}'" in prompt for prompt in prompts)
    facts = [(sql, params) for sql, params in seen if not sql.startswith('SELECT vintage, closed_at, note')]
    assert len(facts) == 1
    assert 'closed_at <=' in facts[0][0]
    assert JULY in facts[0][1].values()


def test_unresolved_close_never_calls_model():
    class Team:
        def run(self, *args, **kwargs):
            pytest.fail('unverified close must not reach model')
    tools = FPATools(SCOPE, executor=executor_with_log([], []))
    result = FPAOrchestrator(tools).run_with_team(Team(), PlanningRequest(request=QUESTION))
    assert result.execution_status == 'VALIDATION_ERROR'


def test_failed_repair_never_executes_current_data():
    seen, prompts = [], []
    class Team:
        members = []
        def run(self, prompt, **kwargs):
            prompts.append(prompt)
            return SimpleNamespace(content={'dsl': DSL})
    tools = FPATools(SCOPE, executor=executor_with_log(seen))
    result = FPAOrchestrator(tools).run_with_team(Team(), PlanningRequest(request=QUESTION))
    assert result.execution_status == 'VALIDATION_ERROR'
    assert len(prompts) == 2
    assert all(sql.startswith('SELECT vintage, closed_at, note') for sql, _ in seen)
