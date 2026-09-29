"""Agno model turns backed by the subscription-authenticated Codex Python SDK.

Reuse the existing subscription adapter's transcript and response conversion.
Agno owns all delegation, tools, hooks and AgentPlan parsing. Codex gets an
ephemeral thread and an isolated home containing only the login credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from agno.exceptions import ModelProviderError

from fpa_project.config import codex_login_home, codex_model
from .claude_code_model import ClaudeCodeModel

# OpenAI's strict schema requires declared object properties. Tool arguments
# vary with Agno's catalog, so transport them as JSON text and decode them back
# to the same argument dictionaries used by Claude before returning to Agno.
_TURN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["call_tools", "respond"]},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
            },
        },
        "content": {"type": "string"},
    },
    "required": ["action", "tool_calls", "content"],
}

_WIRE_FORMAT = (
    "For this transport's output schema, encode each tool's arguments object "
    "as JSON text in arguments_json. Use an empty content string when calling "
    "tools, and an empty tool_calls array when responding. Agno executes the "
    "listed tools; do not use native Codex tools."
)

# Disable native capabilities. The deny hook also blocks local tools that
# cannot be removed from the catalog (such as apply_patch). Read-only sandbox
# and deny_all approvals remain a separate boundary.
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "js_repl", "code_mode", "code_mode_host",
    "multi_agent", "multi_agent_v2", "apps", "plugins", "memory_tool",
    "memories", "browser_use", "computer_use", "view_image", "image_generation",
    "goals", "tool_search", "tool_suggest", "current_time_reminder", "sleep_tool",
    "request_permissions_tool", "send_message_to_user_async", "deferred_executor",
)


def codex_configured() -> bool:
    # Like Claude's readiness check, this checks setup, not a live model call.
    # The SDK/account check reports expired, invalid or API-key-only logins.
    return importlib.util.find_spec("openai_codex") is not None and (codex_login_home() / "auth.json").is_file()


def _prepare_home(root: Path) -> tuple[Path, Path]:
    home, workspace = root / "home", root / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    shutil.copyfile(codex_login_home() / "auth.json", home / "auth.json")
    (home / "auth.json").chmod(0o600)
    deny = "import sys; sys.stderr.write('Native tools disabled; return tool calls to Agno.'); sys.exit(2)"
    command = shlex.join([sys.executable, "-c", deny])
    settings = [
        'forced_login_method = "chatgpt"',
        'cli_auth_credentials_store = "file"',
        'web_search = "disabled"',
        'project_doc_max_bytes = 0',
        'include_environment_context = false',
        'include_apps_instructions = false',
        'include_collaboration_mode_instructions = false',
        '[tools.experimental_request_user_input]',
        'enabled = false',
        '[tools.update_plan]',
        'enabled = false',
        '[features]',
        'hooks = true',
        'skip_host_skill_discovery = true',
        *(f'{feature} = false' for feature in _DISABLED_FEATURES),
        '[[hooks.PreToolUse]]',
        'matcher = ".*"',
        '[[hooks.PreToolUse.hooks]]',
        'type = "command"',
        f'command = {json.dumps(command)}',
        'timeout = 5',
    ]
    (home / "config.toml").write_text("\n".join(settings) + "\n")
    return home, workspace


def _save_refreshed_login(home: Path, source: Path, original: bytes) -> None:
    """Retain SDK token refreshes without copying runtime settings or history.

    Don't replace credentials if the user has signed in/out in the meantime.
    Atomic replacement keeps simultaneous readers from seeing partial JSON.
    """
    refreshed = (home / "auth.json").read_bytes()
    if refreshed == original or not source.is_file() or source.read_bytes() != original:
        return
    owner = source.stat()
    with NamedTemporaryFile(dir=source.parent, prefix=".fpa-auth-", delete=False) as output:
        temporary = Path(output.name)
        output.write(refreshed)
    try:
        # The container runs as root; retain host ownership so a token refresh
        # does not make the host user's existing Codex login unreadable.
        os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class CodexModel(ClaudeCodeModel):
    id: str = "codex"
    name: str = "Codex"
    provider: str = "OpenAI Codex SDK"
    codex_model: str | None = None
    timeout_seconds: float = 120

    async def _acall(self, system: str, prompt: str) -> dict[str, Any]:
        try:
            from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

            with TemporaryDirectory(prefix="fpa-codex-") as directory:
                home, workspace = _prepare_home(Path(directory))
                original = (home / "auth.json").read_bytes()
                config = CodexConfig(cwd=str(workspace), env={
                    "CODEX_HOME": str(home), "CODEX_API_KEY": "", "OPENAI_API_KEY": "",
                })
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        async with AsyncCodex(config=config) as codex:
                            account = await codex.account()
                            if account.account is None or account.account.root.type != "chatgpt":
                                raise ValueError("ChatGPT subscription login required")
                            thread = await codex.thread_start(
                                model=self.codex_model or codex_model(),
                                model_provider="openai",
                                base_instructions=system,
                                developer_instructions=_WIRE_FORMAT,
                                cwd=str(workspace), ephemeral=True,
                                sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all,
                            )
                            result = await thread.run(prompt, output_schema=_TURN_SCHEMA)
                finally:
                    _save_refreshed_login(home, codex_login_home() / "auth.json", original)
                if result.status.value != "completed" or not result.final_response:
                    raise ValueError("Codex turn did not complete")
                data = json.loads(result.final_response)
                if not isinstance(data, dict) or data.get("action") not in {"call_tools", "respond"}:
                    raise ValueError("invalid Codex turn")
                for call in data.get("tool_calls") or []:
                    call["arguments"] = json.loads(call.pop("arguments_json"))
                    if not isinstance(call["arguments"], dict):
                        raise ValueError("tool arguments must be an object")
                usage = result.usage.last if result.usage else None
                data["_usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens} if usage else {}
                return data
        except Exception as exc:
            # SDK errors can contain transport data. Keep the application's
            # existing provider-error path without exposing that payload.
            raise ModelProviderError(
                f"Codex SDK error: {type(exc).__name__}", model_name=self.name, model_id=self.id,
            ) from None
