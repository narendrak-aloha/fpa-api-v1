"""Safe terminal logging for the agent-to-ClickHouse lifecycle."""

from __future__ import annotations

import json
import logging
import os

from fpa_project.config import external_audit_log
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .masking import mask_for_llm


LOGGER_NAME = "fpa_project.agent_team"
_HANDLER_MARKER = "_fpa_terminal_handler"


class _StderrHandler(logging.StreamHandler):
    """A stream handler that follows ``sys.stderr`` rather than capturing it.

    The handler is created once per process, but pytest's capture and
    uvicorn's reload both swap ``sys.stderr`` after that; binding the stream
    at construction time would send every later line to the old one.
    """

    def __init__(self) -> None:
        super().__init__(stream=None)

    @property
    def stream(self):  # type: ignore[override]
        import sys

        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def configure_terminal_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure one concise terminal handler without exposing sensitive data."""
    # Reuse one marked handler so repeated worker construction does not
    # duplicate every lifecycle event in the terminal.
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers):
        handler = _StderrHandler()
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit allow-listed operational metadata as one JSON object."""
    # Allow-list metadata instead of attempting to redact arbitrary payloads.
    safe_fields = {
        key: value for key, value in fields.items()
        if key in {"run_id", "status", "attempt", "max_attempts", "row_count", "estimated_rows", "code", "error_type", "member_count"}
    }
    logger.info("%s %s", event, json.dumps(safe_fields, sort_keys=True, default=str))


class ExternalAuditLogger:
    """Append redacted request/response envelopes for external-system calls."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = path or external_audit_log()
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, system: str, direction: str, payload: Any, *, run_id: str | None = None) -> None:
        # Audit envelopes are append-only JSONL and contain a sanitized shape
        # suitable for tracing a request without retaining secrets.
        envelope = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "system": system,
            "direction": direction,
            "run_id": run_id,
            "payload": self._safe_payload(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, default=str, sort_keys=True) + "\n")

    @staticmethod
    def _safe_payload(payload: Any) -> Any:
        # Normalize Pydantic and container values recursively before masking;
        # unknown objects are represented only by their type string.
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        if isinstance(payload, dict):
            return mask_for_llm(payload)
        if isinstance(payload, list):
            return [ExternalAuditLogger._safe_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return [ExternalAuditLogger._safe_payload(item) for item in payload]
        if isinstance(payload, (str, int, float, bool)) or payload is None:
            return payload
        return str(payload)
