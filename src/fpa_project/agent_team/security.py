"""Agno run/tool controls plus the final provider egress boundary.

Run pre-hooks alone do not cover a model's next turn after a tool call. Provider
invocations are gated as well, including async and streaming variants. Any gate
failure raises an Agno check error (ordinary hook exceptions can be logged and
ignored by the framework).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import wraps

from agno.exceptions import InputCheckError, OutputCheckError
from agno.guardrails.base import BaseGuardrail
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.run.agent import RunInput
from sqlalchemy import text

from .hooks import ArithmeticVerificationPostHook
from .masking import SENSITIVE_KEYS, mask_for_llm, mask_request_text
from .models import UserScope


class InjectionGuardrail(BaseGuardrail):
    """Reject explicit attempts to replace instructions or widen permissions.

    This is defence in depth; authorization never depends on matching prose.
    Returned personal dimension values are tokenized before this check.
    """
    pattern = re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|"
        r"(?:reveal|print|expose)\s+(?:the\s+)?(?:system\s+prompt|api\s+key|password)|"
        r"(?:bypass|disable|override)\s+(?:the\s+)?(?:scope|permissions|guardrails)|"
        r"(?:system|developer)\s*:\s*|<\|(?:system|im_start)\|>", re.I,
    )

    def check(self, run_input):
        if self.pattern.search(run_input.input_content_string()):
            raise InputCheckError("instruction override attempt refused")

    async def async_check(self, run_input):
        self.check(run_input)


def require_scope(run_context, expected: UserScope) -> UserScope:
    try:
        scope = UserScope.model_validate((run_context.dependencies or {})["scope"])
        if scope.user_id != expected.user_id or not scope.allowed_companies:
            raise ValueError("missing authenticated identity or entity scope")
        if not scope.allowed_companies <= (expected.allowed_companies or frozenset()):
            raise ValueError("dependency scope exceeds authenticated scope")
        if scope.max_estimated_rows > expected.max_estimated_rows:
            raise ValueError("dependency budget exceeds authenticated budget")
        return scope
    except Exception as exc:
        raise InputCheckError("authenticated scope dependency required") from exc


def persist_disclosure(event):
    from fpa_project.governance import engine
    with engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO fpa_governance.llm_disclosure_log "
            "(user_id, scope, field_classes, model_name, payload_sha256) "
            "VALUES (:user_id, CAST(:scope AS jsonb), :classes, :model, :digest)"
        ), event)


# Agno's own team tools. A delegation returns the member's run as an event
# stream, not data, so there is nothing here to classify: the member run has
# its own pre-hooks, tool hooks and egress-gated model, and what it returns
# reaches the leader's model only through gate_messages, which masks and logs
# it before the send. Masking the stream itself fails closed on the generator
# and silently blocked every delegation.
DELEGATION_TOOLS = frozenset({"delegate_task_to_member", "delegate_task_to_members"})


class HistoricalQueryGuardrail(BaseGuardrail):
    """Enforce the trusted request constraint on member calls and final plans."""

    def __init__(self, tools):
        self.tools = tools

    def validate(self, dsl):
        constraint = self.tools.historical_constraint
        if constraint is not None:
            try:
                constraint.validate(dsl)
            except Exception as exc:
                raise OutputCheckError(str(exc)) from exc

    def check(self, run_output):
        content = run_output.content
        if hasattr(content, 'model_dump'):
            content = content.model_dump()
        if isinstance(content, dict) and not content.get('out_of_scope'):
            self.validate(content.get('dsl', ''))

    async def async_check(self, run_output):
        self.check(run_output)


class AgentBoundary:
    def __init__(self, tools, writer=persist_disclosure):
        self.tools = tools
        self.writer = writer
        self.pii = PIIDetectionGuardrail(mask_pii=True)
        self.injection = InjectionGuardrail()
        self.historical = HistoricalQueryGuardrail(tools)
        self.evidence = []
        self.executed_dsl = []
        self.classes = set()

    def pre(self, run_input, run_context):
        require_scope(run_context, self.tools.scope)
        try:
            run_input.input_content = self.sanitize(run_input.input_content)
            self.injection.check(run_input)
            # A leader may omit context when delegating. Every member still
            # receives the same verified constraint from the backend toolset.
            constraint = self.tools.historical_constraint
            if constraint and isinstance(run_input.input_content, str):
                run_input.input_content += '\n\n' + constraint.instruction
        except InputCheckError:
            raise
        except Exception as exc:
            raise InputCheckError("input classification failed; model call blocked") from exc

    def sanitize(self, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            self.classes.update(k.lower() for k in value if isinstance(k, str) and k.lower() in SENSITIVE_KEYS)
            return {k: self.sanitize(v) for k, v in mask_for_llm(value).items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(v) for v in value]
        if isinstance(value, str):
            # Tool content is commonly JSON serialized by the framework.
            try:
                structured = json.loads(value)
            except (ValueError, TypeError):
                structured = None
            if isinstance(structured, (dict, list)):
                return json.dumps(self.sanitize(structured), default=str)
            original = value
            value = mask_request_text(value)
            run_input = RunInput(input_content=value)
            self.pii.check(run_input)
            if original != run_input.input_content:
                self.classes.add("free_text_pii")
            return run_input.input_content
        return mask_for_llm(value)  # rejects unknown types rather than stringify

    def tool(self, function_name, function_call, arguments, run_context):
        scope = require_scope(run_context, self.tools.scope)
        # A dependency may narrow scope, but must not leave a wider tool closure.
        if scope != self.tools.scope:
            raise InputCheckError("build a new toolset for a narrowed dependency scope")
        if function_name in DELEGATION_TOOLS:
            return function_call(**arguments)
        if function_name == "run_finops_query":
            self.historical.validate(arguments.get('dsl', ''))
        if function_name in {'propose_driver', 'propose_reforecast'} and self.tools.historical_constraint:
            raise OutputCheckError('A historical evidence request cannot change a driver or draft a re-forecast')
        try:
            result = function_call(**arguments)
            safe = self.sanitize(result)
            if function_name == "run_finops_query" and isinstance(safe, dict):
                self.evidence.extend(safe.get("rows", []))
                self.executed_dsl.append(str(arguments.get("dsl", "")))
                self.evidence.extend(safe.get("drift_flags", []))
            if function_name == "propose_reforecast" and isinstance(safe, dict) and safe.get("status") == "DRAFT":
                # The draft's own values and scope may be cited back to the planner.
                self.evidence.append({"from_value": safe.get("from_value"), "to_value": safe.get("to_value")})
                self.executed_dsl.append(" ".join([*safe.get("companies", []), *safe.get("months", [])]))
            self.record(safe, "tool:" + function_name)
            return safe
        except InputCheckError:
            raise
        except Exception as exc:
            raise InputCheckError("tool classification or disclosure failed; output blocked") from exc

    def post(self, run_output):
        self.historical.check(run_output)
        content = run_output.content
        if hasattr(content, "model_dump"):
            content = content.model_dump()
        narrative = content.get("explanation", "") if isinstance(content, dict) else str(content or "")
        ok, reason = ArithmeticVerificationPostHook().verify(narrative, self.evidence, " ".join(self.executed_dsl))
        if not ok:
            raise OutputCheckError(reason)

    def record(self, payload, model):
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        self.writer({
            "user_id": self.tools.scope.user_id,
            "scope": json.dumps({"companies": sorted(self.tools.scope.allowed_companies or []),
                                 "methods": ["dimension_tokenization", "free_text_redaction"],
                                 "max_estimated_rows": self.tools.scope.max_estimated_rows}),
            "classes": sorted(self.classes), "model": model,
            "digest": hashlib.sha256(encoded.encode()).hexdigest(),
        })

    def gate_messages(self, messages, model):
        try:
            if not self.tools.scope.allowed_companies:
                raise ValueError("no scope")
            safe = []
            for message in messages:
                clone = copy.copy(message)
                clone.content = self.sanitize(message.content)
                # Also mask serialized tool arguments in assistant tool calls.
                if getattr(message, "tool_calls", None):
                    clone.tool_calls = self.sanitize(message.tool_calls)
                safe.append(clone)
            self.record([m.to_dict() for m in safe], model)
            return safe
        except Exception as exc:
            raise InputCheckError("model egress classification/disclosure failed; nothing sent") from exc

    def protect_model(self, model):
        # Each team receives its own provider object. Methods wrap the last
        # point before transport, so tool-followup turns cannot evade logging.
        protected = copy.copy(model)
        for name in ("invoke", "ainvoke", "invoke_stream", "ainvoke_stream"):
            original = getattr(model, name)
            def prepare(args, kwargs):
                if "messages" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs["messages"] = self.gate_messages(kwargs["messages"], model.id)
                elif args:
                    args = (self.gate_messages(args[0], model.id), *args[1:])
                else:
                    raise InputCheckError("provider invocation has no messages")
                return args, kwargs
            if name == "ainvoke":
                async def wrapped(*args, _original=original, **kwargs):
                    args, kwargs = prepare(args, kwargs)
                    return await _original(*args, **kwargs)
            elif name == "ainvoke_stream":
                async def wrapped(*args, _original=original, **kwargs):
                    args, kwargs = prepare(args, kwargs)
                    async for item in _original(*args, **kwargs):
                        yield item
            else:
                def wrapped(*args, _original=original, **kwargs):
                    args, kwargs = prepare(args, kwargs)
                    return _original(*args, **kwargs)
            setattr(protected, name, wraps(original)(wrapped))
        return protected
