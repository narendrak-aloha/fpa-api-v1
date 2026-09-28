"""Environment-backed, round-robin API key rotation for model calls."""

from __future__ import annotations

import os
from threading import Lock
from typing import Any, Iterable


class APIKeyRotator:
    """Select API keys in round-robin order and apply them to an Agno team."""

    def __init__(self, keys: Iterable[str]):
        self._keys = tuple(key.strip() for key in keys if key and key.strip())
        self._index = 0
        self._lock = Lock()

    @classmethod
    def from_env(cls) -> "APIKeyRotator":
        """Load comma- or newline-separated keys from the environment."""
        list_names = (
            "LLM_API_KEYS", "OPENAI_API_KEYS", "ANTHROPIC_API_KEYS",
            "GOOGLE_API_KEYS", "GEMINI_API_KEYS",
        )
        value = next((os.getenv(name) for name in list_names if os.getenv(name)), "")
        if not value:
            singular_names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")
            value = next((os.getenv(name) for name in singular_names if os.getenv(name)), "")
        return cls(value.replace("\n", ",").split(","))

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    def next_key(self) -> str | None:
        if not self._keys:
            return None
        with self._lock:
            key = self._keys[self._index]
            self._index = (self._index + 1) % len(self._keys)
            return key

    def apply_to_team(self, team: Any) -> str | None:
        """Apply the next key to a team and its model members."""
        key = self.next_key()
        if key is None:
            return None
        models = []
        team_model = getattr(team, "model", None)
        if team_model is not None:
            models.append(team_model)
        for member in getattr(team, "members", ()) or ():
            member_model = getattr(member, "model", None)
            if member_model is not None:
                models.append(member_model)
        for model in models:
            self._apply_to_model(model, key)
        return key

    @staticmethod
    def _apply_to_model(model: Any, key: str) -> None:
        for attribute in ("api_key", "openai_api_key", "anthropic_api_key", "google_api_key"):
            if hasattr(model, attribute):
                setattr(model, attribute, key)
        client = getattr(model, "client", None)
        if client is not None and hasattr(client, "api_key"):
            setattr(client, "api_key", key)
        if not any(hasattr(model, attribute) for attribute in ("api_key", "openai_api_key", "anthropic_api_key", "google_api_key")):
            setattr(model, "api_key", key)
