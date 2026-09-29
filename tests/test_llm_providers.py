"""Subscription transport contracts and provider-independent application behavior."""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.exceptions import InputCheckError, ModelProviderError
from agno.models.message import Message
from fastapi.testclient import TestClient

import app as api
from fpa_project import ask_history, governance
from fpa_project.agent_team import FPATools, UserScope, build_agno_team
from fpa_project.agent_team.claude_code_model import ClaudeCodeModel
from fpa_project.agent_team.codex_model import CodexModel, _save_refreshed_login
from fpa_project.agent_team.security import AgentBoundary

DSL = "SELECT services_revenue BY company FOR PERIOD 2026-Q2"
PLAN = {"dsl": DSL, "explanation": "", "assumptions": []}
SCOPE = UserScope(user_id="analyst", allowed_companies=frozenset({"RTPL1"}))


@pytest.fixture
def codex_sdk(monkeypatch, tmp_path):
    import openai_codex
    from openai_codex.generated.v2_all import TurnStatus

    login = tmp_path / "login"
    login.mkdir()
    (login / "auth.json").write_text('{"tokens": {"access_token": "test-secret"}}')
    (login / "config.toml").write_text('developer_instructions = "Never inherit this"')
    monkeypatch.setenv("FPA_CODEX_HOME", str(login))
    state = SimpleNamespace(
        login=login, calls=[], account_type="chatgpt", fail=None, delay=0,
        response={"action": "respond", "tool_calls": [], "content": json.dumps(PLAN)},
        status=TurnStatus.completed, refreshed=None,
    )

    class SDK:
        def __init__(self, config):
            state.config = config
            state.home = Path(config.env["CODEX_HOME"])
            state.settings = tomllib.loads((state.home / "config.toml").read_text())

        async def __aenter__(self):
            assert (state.home / "auth.json").read_bytes() == (login / "auth.json").read_bytes()
            return self

        async def __aexit__(self, *args):
            if state.refreshed:
                (state.home / "auth.json").write_text(state.refreshed)

        async def account(self):
            return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(type=state.account_type)))

        async def thread_start(self, **options):
            state.calls.append(options)
            return self

        async def run(self, prompt, output_schema):
            import asyncio
            state.prompt, state.schema = prompt, output_schema
            if state.fail:
                raise state.fail
            await asyncio.sleep(state.delay)
            return SimpleNamespace(
                status=state.status, final_response=json.dumps(state.response),
                usage=SimpleNamespace(last=SimpleNamespace(input_tokens=100, output_tokens=20)),
            )

    monkeypatch.setattr(openai_codex, "AsyncCodex", SDK)
    return state


def test_codex_sdk_turn_preserves_transcript_and_isolates_native_tools(codex_sdk, monkeypatch):
    monkeypatch.setenv("FPA_CODEX_MODEL", "configured-model")
    messages = [Message(role="system", content="Original instructions"), Message(role="user", content="Revenue?")]
    model = CodexModel()
    response = model.invoke(messages, Message(role="assistant"))
    assert json.loads(response.content) == PLAN
    assert (response.input_tokens, response.output_tokens) == (100, 20)
    options = codex_sdk.calls[0]
    assert options["model"] == "configured-model"
    assert options["model_provider"] == "openai"
    assert options["sandbox"] == "read-only" and options["approval_mode"] == "deny_all"
    assert options["ephemeral"] is True
    assert options["base_instructions"] == ClaudeCodeModel()._render(messages, None)[0]
    assert codex_sdk.prompt == ClaudeCodeModel()._render(messages, None)[1]
    settings = codex_sdk.settings
    assert settings["forced_login_method"] == "chatgpt"
    assert settings["web_search"] == "disabled"
    assert settings["features"]["shell_tool"] is False
    assert settings["features"]["apps"] is False and settings["features"]["plugins"] is False
    assert "Never inherit" not in str(settings)
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert subprocess.run(hook["command"], shell=True, capture_output=True).returncode == 2
    assert codex_sdk.config.env["OPENAI_API_KEY"] == codex_sdk.config.env["CODEX_API_KEY"] == ""
    assert not codex_sdk.home.exists()
    assert codex_sdk.schema["additionalProperties"] is False


@pytest.mark.parametrize("mode", ["sync", "async", "stream", "async_stream"])
async def test_codex_returns_tool_calls_to_agno(codex_sdk, mode):
    codex_sdk.response = {"action": "call_tools", "content": "", "tool_calls": [
        {"name": "run_finops_query", "arguments_json": json.dumps({"dsl": DSL})},
    ]}
    model = CodexModel()
    args = ([Message(role="user", content="Revenue?")], Message(role="assistant"))
    if mode == "sync":
        response = model.invoke(*args)
    elif mode == "async":
        response = await model.ainvoke(*args)
    elif mode == "stream":
        response, = model.invoke_stream(*args)
    else:
        responses = [r async for r in model.ainvoke_stream(*args)]
        response, = responses
    assert response.content is None
    call, = response.tool_calls
    assert call["function"]["name"] == "run_finops_query"
    assert json.loads(call["function"]["arguments"]) == {"dsl": DSL}


@pytest.mark.parametrize("fault", ["sdk_error", "bad_arguments", "wrong_account", "incomplete", "timeout"])
def test_codex_failures_use_existing_provider_error_contract(codex_sdk, fault):
    model = CodexModel()
    if fault == "sdk_error":
        codex_sdk.fail = RuntimeError("sensitive provider payload test-secret")
    elif fault == "bad_arguments":
        codex_sdk.response = {"action": "call_tools", "tool_calls": [{"name": "x", "arguments_json": "[]"}]}
    elif fault == "wrong_account":
        codex_sdk.account_type = "apiKey"
    elif fault == "incomplete":
        from openai_codex.generated.v2_all import TurnStatus
        codex_sdk.status = TurnStatus.interrupted
    else:
        codex_sdk.delay, model.timeout_seconds = 1, 0.01
    with pytest.raises(ModelProviderError) as caught:
        model.invoke([Message(role="user", content="Revenue?")], Message(role="assistant"))
    assert "Codex SDK error:" in str(caught.value)
    assert "test-secret" not in str(caught.value)
    assert not codex_sdk.home.exists()
    if fault == "wrong_account":
        assert not codex_sdk.calls


def test_sdk_token_refresh_is_retained_but_external_login_changes_are_preserved(codex_sdk, tmp_path):
    source = codex_sdk.login / "auth.json"
    owner = source.stat()
    codex_sdk.refreshed = '{"tokens": {"access_token": "refreshed"}}'
    CodexModel().invoke([Message(role="user", content="Revenue?")], Message(role="assistant"))
    assert source.read_text() == codex_sdk.refreshed
    assert (source.stat().st_uid, source.stat().st_gid) == (owner.st_uid, owner.st_gid)
    assert source.stat().st_mode & 0o777 == 0o600
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "auth.json").write_text("new SDK credentials")
    _save_refreshed_login(isolated, source, b"old credentials")
    assert source.read_text() == codex_sdk.refreshed


def test_claude_subscription_sdk_options_and_response_remain_unchanged(monkeypatch):
    import claude_agent_sdk

    seen = []
    async def query(prompt, options):
        seen.append(options)
        yield claude_agent_sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="test", result=json.dumps(PLAN),
            structured_output={"action": "respond", "content": json.dumps(PLAN)},
            usage={"input_tokens": 100, "output_tokens": 20},
        )
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    response = ClaudeCodeModel().invoke([Message(role="user", content="Revenue?")], Message(role="assistant"))
    assert json.loads(response.content) == PLAN
    assert (response.input_tokens, response.output_tokens) == (100, 20)
    assert seen[0].tools == [] and seen[0].allowed_tools == [] and seen[0].setting_sources == []


@pytest.mark.parametrize("model_type", [ClaudeCodeModel, CodexModel])
def test_existing_disclosure_failure_blocks_both_providers(monkeypatch, model_type):
    async def forbidden(*args):
        pytest.fail("provider transport must not run before disclosure succeeds")
    monkeypatch.setattr(model_type, "_acall", forbidden)
    def fail(event):
        raise RuntimeError("disclosure store unavailable")
    model = AgentBoundary(FPATools(SCOPE), writer=fail).protect_model(model_type())
    with pytest.raises(InputCheckError):
        model.invoke([Message(role="user", content="Revenue?")], Message(role="assistant"))


@pytest.fixture
def client(monkeypatch):
    events, executed = [], []
    principal = governance.Principal("analyst", "Analyst", frozenset({"analyst"}), frozenset({"RTPL1"}), True)
    api.app.dependency_overrides[api.current_user] = lambda: principal
    monkeypatch.setattr(governance, "countries_of", lambda companies: frozenset({"PL"}))
    monkeypatch.setattr(ask_history, "record", lambda *args: 1)
    monkeypatch.setattr(api, "_reforecast_desk", lambda who: None)
    def execute(sql, params):
        executed.append((sql, params))
        assert "RTPL1" in params.values() and "RTUS1" not in params.values()
        return [{"company": "RTPL1", "services_revenue": 42}]
    monkeypatch.setattr(api, "scoped_tools", lambda scope, desk: FPATools(scope, executor=execute))
    def test_team(model, toolset):
        team = build_agno_team(model=model, toolset=toolset, disclosure_writer=events.append)
        team.telemetry = False
        for member in team.members:
            member.telemetry = False
        return team
    monkeypatch.setattr(api, "build_agno_team", test_team)
    monkeypatch.setattr(api, "provider_configured", lambda provider: True)
    async def scripted(self, system, prompt):
        if "AVAILABLE TOOLS:\n" in system and "delegate_task_to_member" in system:
            if "[TOOL RESULT" not in prompt:
                return {"action": "call_tools", "tool_calls": [{"name": "delegate_task_to_member", "arguments": {
                    "member_id": "fpa-query", "task": "Show services revenue by company",
                }}]}
        elif "[TOOL RESULT run_finops_query" not in prompt:
            return {"action": "call_tools", "tool_calls": [{"name": "run_finops_query", "arguments": {"dsl": DSL}}]}
        return {"action": "respond", "content": json.dumps(PLAN)}
    monkeypatch.setattr(ClaudeCodeModel, "_acall", scripted)
    monkeypatch.setattr(CodexModel, "_acall", scripted)
    with TestClient(api.app) as http:
        yield http, events, executed
    api.app.dependency_overrides.clear()


def test_full_agno_team_api_compiler_scope_and_rows_match_when_switching_providers(client):
    http, events, executed = client
    responses = [http.post("/api/v1/query", json={"query": "Show services revenue by company", "provider": provider,
                 "companies": ["RTPL1", "RTUS1"]}).json() for provider in ["claude-code", "codex"]]
    for response in responses:
        assert response["mode"] == "agno_team"
        assert response["agent_response"]["execution_status"] == "SUCCESS"
        assert response["agent_response"]["produced_by"] == "fpa-query"
        assert response["agent_response"]["cited_data_rows"] == [{"company": "RTPL1", "services_revenue": 42}]
    for response in responses:
        for key in ("provider", "duration_ms"):
            response.pop(key)
    assert responses[0] == responses[1]
    assert executed and events
    assert {event["model"] for event in events} >= {"claude-code", "codex"}


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_direct_dsl_and_unconfigured_provider_keep_existing_api_behavior(client, monkeypatch, provider):
    http, _, _ = client
    monkeypatch.setattr(api, "provider_configured", lambda _: False)
    direct = http.post("/api/v1/query", json={"query": DSL, "provider": provider}).json()
    assert direct["mode"] == "direct_dsl" and direct["agent_response"]["execution_status"] == "SUCCESS"
    missing = http.post("/api/v1/query", json={"query": "Show revenue", "provider": provider}).json()
    assert missing["mode"] == "not_run"
    assert missing["agent_response"]["execution_status"] == "VALIDATION_ERROR"
    assert "not configured" in missing["agent_response"]["error_message"]
    assert missing["sql"] is None


def test_provider_default_is_configurable_and_explicit_selection_wins(monkeypatch):
    monkeypatch.delenv("FPA_LLM_PROVIDER", raising=False)
    assert api.QueryRequest(query="Revenue?").provider == "claude-code"
    monkeypatch.setenv("FPA_LLM_PROVIDER", "codex")
    assert api.QueryRequest(query="Revenue?").provider == "codex"
    assert api.QueryRequest(query="Revenue?", provider="claude-code").provider == "claude-code"
    monkeypatch.setenv("FPA_LLM_PROVIDER", "typo")
    with pytest.raises(ValueError):
        api.QueryRequest(query="Revenue?")


def test_provider_readiness_and_existing_provider_constructors(codex_sdk, monkeypatch):
    import fpa_project.agent_team.codex_model as module
    assert api.provider_configured("codex")
    (codex_sdk.login / "auth.json").unlink()
    assert not api.provider_configured("codex")
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: None)
    assert not api.provider_configured("codex")
    assert isinstance(api.build_model("codex"), CodexModel)
    assert isinstance(api.build_model("claude-code"), ClaudeCodeModel)
    assert api.build_model("claude-api").provider == "Anthropic"
    assert api.build_model("gemini").provider == "Google"
    monkeypatch.setattr(api.shutil, "which", lambda _: "/bin/claude")
    assert api.provider_configured("claude-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert api.provider_configured("claude-api")


def test_provider_list_contract_and_invalid_selection(client):
    http, _, _ = client
    assert http.get("/api/v1/providers").json() == [
        {"id": provider, "configured": True} for provider in ["claude-code", "codex", "claude-api", "gemini"]
    ]
    assert http.post("/api/v1/query", json={"query": "Revenue?", "provider": "unknown"}).status_code == 422


@pytest.mark.parametrize(('provider', 'key'), [('claude-api', 'ANTHROPIC_API_KEY'), ('gemini', 'GOOGLE_API_KEY')])
def test_missing_api_key_is_reported_before_database_or_model_access(monkeypatch, provider, key):
    monkeypatch.setattr(api, 'provider_configured', lambda _: False)
    def no_database(*args):
        pytest.fail('missing API key must be checked before connecting to the database')
    monkeypatch.setattr(api, 'scoped_tools', no_database)
    monkeypatch.setattr(governance, 'countries_of', no_database)
    principal = governance.Principal('analyst', 'Analyst', frozenset({'analyst'}), frozenset({'RTPL1'}), True)
    request = SimpleNamespace(state=SimpleNamespace())
    response = api._answer(api.QueryRequest(query='Show revenue', provider=provider), request,
                           principal, principal.companies).model_dump()
    assert response['mode'] == 'not_run'
    assert 'API key is missing' in response['agent_response']['error_message']
    assert key in response['agent_response']['error_message']
    assert '.env' not in response['agent_response']['error_message']
    assert 'restart' not in response['agent_response']['error_message']
    assert response['sql'] is None


@pytest.mark.parametrize('provider', ['claude-api', 'gemini'])
def test_whitespace_only_api_keys_are_missing(monkeypatch, provider):
    for key in api.PROVIDER_KEYS[provider]:
        monkeypatch.setenv(key, '   ')
    assert not api.provider_configured(provider)
    monkeypatch.setenv(api.PROVIDER_KEYS[provider][0], 'test-key')
    assert api.provider_configured(provider)
