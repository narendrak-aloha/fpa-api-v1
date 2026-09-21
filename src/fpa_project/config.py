"""Every environment variable the application reads.

Host runs see the same .env compose uses; already-set variables win, so inside the
container compose stays authoritative. Values are read per call, not at import, so
a test can change one without reimporting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env", override=False)


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name}={value!r} is not an integer") from exc


def env_bool(name: str, default: bool = False) -> bool:
    """python-dotenv loads strings only, so the parsing lives here."""
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name}={value!r} is not a boolean")


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str
    password: str


def clickhouse() -> ClickHouseSettings:
    """Cube connection. Defaults to localhost for the documented host-run mode;
    compose overrides the host with the service name."""
    return ClickHouseSettings(
        host=env_str("CLICKHOUSE_HOST", "localhost"),
        port=env_int("CLICKHOUSE_PORT", 8123),
        user=env_str("CLICKHOUSE_USER", "default"),
        password=env_str("CLICKHOUSE_PASSWORD", "fpa"),
    )


@dataclass(frozen=True)
class TemporalSettings:
    host: str
    namespace: str
    task_queue: str


def temporal() -> TemporalSettings:
    """Where the recompute worker and the API's workflow client meet.

    Defaults to localhost for the documented host-run mode; compose overrides
    the host with the service name.
    """
    return TemporalSettings(
        host=env_str("TEMPORAL_HOST", "localhost:7233"),
        namespace=env_str("TEMPORAL_NAMESPACE", "default"),
        task_queue=env_str("TEMPORAL_TASK_QUEUE", "fpa-recompute"),
    )


def approval_timeout_hours() -> int:
    """How long the workflow parks on the approval signal before expiring.

    72h by default: an approval can legitimately span a weekend, and expiring
    is the safe outcome because nothing publishes. See the README for why this
    expires rather than escalates.
    """
    return env_int("FPA_APPROVAL_TIMEOUT_HOURS", 72)


def partition_size() -> int:
    """Dirty rows per child workflow. Also the evaluate checkpoint stride."""
    return env_int("FPA_PARTITION_SIZE", 5_000)


def commitment_base_url() -> str:
    return env_str("FPA_COMMITMENT_URL", "http://localhost:8100")


def commitment_failure_rate() -> float:
    """Injected failure probability for the Commitment Service, 0.0-1.0.

    Zero by default. Turn it up at runtime through POST /admin/failure-rate,
    or at start-up through FPA_COMMITMENT_FAILURE_RATE.
    """
    value = env_str("FPA_COMMITMENT_FAILURE_RATE", "0")
    try:
        rate = float(value)
    except ValueError as exc:
        raise ValueError(f"FPA_COMMITMENT_FAILURE_RATE={value!r} is not a number") from exc
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"FPA_COMMITMENT_FAILURE_RATE={value!r} is outside 0.0-1.0")
    return rate


def claude_api_model() -> str:
    return env_str("FPA_CLAUDE_MODEL", "claude-sonnet-5")


def claude_code_model() -> str | None:
    return env_str("FPA_CLAUDE_CODE_MODEL") or None


def gemini_model() -> str:
    return env_str("FPA_MODEL_ID", "gemini-2.5-flash")


def external_audit_log() -> str:
    return env_str("FPA_EXTERNAL_AUDIT_LOG", "logs/fpa_external_audit.jsonl")
