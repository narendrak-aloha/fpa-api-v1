"""Framework-neutral safety hooks used around Agno calls and tools."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .masking import mask_for_llm


class MaskingGateHook:
    """Masks model/tool payloads and records a minimal disclosure audit event."""

    def __init__(self, disclosure_log: list[dict[str, Any]] | None = None):
        self.disclosure_log = disclosure_log if disclosure_log is not None else []

    def before_model(self, payload: Any, *, user_id: str) -> Any:
        # Mask at the framework boundary so every model call gets the same
        # protection regardless of which caller constructed the payload.
        masked = mask_for_llm(payload)
        self._record("model_input", user_id)
        return masked

    def before_tool(self, payload: Any, *, user_id: str) -> Any:
        masked = mask_for_llm(payload)
        self._record("tool_input", user_id)
        return masked

    def after_tool(self, payload: Any, *, user_id: str) -> Any:
        masked = mask_for_llm(payload)
        self._record("tool_output", user_id)
        return masked

    def _record(self, event: str, user_id: str) -> None:
        self.disclosure_log.append({
            "event": event,
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sensitive_values_disclosed": False,
        })


UNTRACEABLE = "narrative contains untraceable numeric claims"
SCOPE_OVERREACH = "narrative describes a filtered figure as the caller's whole scope"
MISATTRIBUTED = "narrative attributes a figure to the wrong measure or direction"

# What a narrative word refers to, and the result-row keys that carry it. The
# multi-word phrases come first in spirit: when two labels end at the same
# place ("exchange rate" / "rate"), the longer one wins.
_LEGS = {
    "price": {"price", "price_variance"},
    "volume": {"volume", "volume_variance"},
    "mix": {"mix", "mix_variance", "mix_between", "mix_between_variance", "mix_within"},
    "fx": {"fx", "fx_variance"},
    "rate": {"rate", "rate_variance"},
    "efficiency": {"efficiency", "efficiency_variance"},
    "gap": {"gap", "total_gap", "change", "delta"},
    "residual": {"residual"},
}
_LEVEL_WORDS = {"revenue": "revenue", "cost": "cost", "margin": "margin"}
_LABELS = [
    ("fx", r"exchange rates?|currenc(?:y|ies)|fx|z[lł]oty|translation"),
    ("price", r"bill rates?|pric(?:e|es|ing)|discount\w*|rate erosion"),
    ("rate", r"cost rates?|rates?"),
    ("volume", r"volume"),
    ("mix", r"mix|blend|pyramid"),
    ("efficiency", r"efficiency|productivity"),
    ("residual", r"residual|unexplained"),
    ("gap", r"gap|miss(?:ed|es)?|shortfall|overall change|total change"),
    ("revenue", r"revenues?|sales"),
    ("cost", r"costs?|spend"),
    ("margin", r"margins?"),
]
_LABEL_RE = [(name, re.compile(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", re.I)) for name, pattern in _LABELS]
_DOWN = re.compile(r"(?<![A-Za-z])(fell|fall(?:s|ing)?|dropped|drops?|declin\w*|decreas\w*|down|lower|shrank|"
                   r"miss(?:ed|es)?|short|lost|loss|reduc\w*|cut|negative|below)(?![A-Za-z])", re.I)
_UP = re.compile(r"(?<![A-Za-z])(rose|ris(?:e|es|ing)|grew|grow\w*|increas\w*|up|higher|gain\w*|beat|"
                 r"above|positive|improv\w*|added)(?![A-Za-z])", re.I)
_CHANGE_VERB = re.compile(r"\b(by|of)\s*$", re.I)


# Only the claim that the *figures* span the scope. Merely naming the scope
# is not the error: "anything outside the caller's scope is not visible here"
# is the model doing as it is told, and an earlier version of this pattern
# matched the phrase rather than the claim and rejected it on every run.
_SCOPE_OVERREACH = re.compile(
    r"alongside other entit"
    r"|not (?:isolated|restricted|limited|filtered) to"
    r"|full (?:result|entity|query) scope"
    r"|reflect(?:s|ing)? the full"
    r"|(?:figures?|results?|numbers?|totals?|revenue|cost)[^.]{0,80}"
    r"\bfor the (?:caller'?s|your)[^.]{0,30}scope",
    re.I,
)


class ScopeClaimGuard:
    """Rejects a narrative that widens the slice the query actually read.

    A dimension filter in the DSL narrows the result before the caller's
    entity scope is applied, so a filtered figure is not the caller's whole
    scope and must not be described as one. The arithmetic check cannot catch
    this: the digits are right, what they are attached to is not, and a reader
    acting on a country figure they believe is group-wide is the failure.
    """

    def verify(self, narrative: str | None, dsl: str | None) -> tuple[bool, str | None]:
        if not narrative or not dsl or "WHERE" not in dsl.upper():
            return True, None
        found = _SCOPE_OVERREACH.search(narrative)
        if found:
            return False, f"{SCOPE_OVERREACH}: {found.group(0)!r}"
        return True, None


class ArithmeticVerificationPostHook:
    """Rejects a narrative figure that the cited result set does not support.

    Two checks, in order. Every figure must equal a value in the returned rows
    (or, unlabelled, a number of the executed DSL: the year in ``FOR PERIOD
    2026-Q2`` is not an invented figure). And a figure that the narrative
    attaches to a measure must be *that* measure's value, with the sign the
    words give it: "FX caused -0.7M" fails when the fx leg is -0.5M even if
    -0.7M sits in some other column, and "revenue fell by 11.4M" fails when
    11.4M is the revenue level and no change of -11.4M was returned. A number
    that exists but has had its meaning changed is as invented as one that
    does not exist.
    """

    _number = re.compile(
        r"(?<![A-Za-z0-9_])(?P<value>-?(?:\d{1,3}(?:,\d{3})+|\d+|\.\d+)(?:\.\d+)?)"
        r"(?P<scale>\s*(?:billion|million|thousand|[kKmMbB]|%))?(?![A-Za-z0-9_])"
    )
    _factors = {"k": Decimal(1000), "thousand": Decimal(1000), "m": Decimal(1000000),
                "million": Decimal(1000000), "b": Decimal(1000000000), "billion": Decimal(1000000000),
                "%": Decimal("0.01"), "": Decimal(1)}

    def verify(self, narrative: str | None, rows: list[dict[str, Any]], context: str = "") -> tuple[bool, str | None]:
        if not narrative:
            return True, None
        evidence: list[tuple[str, Decimal]] = []
        for row in rows:
            self._collect(row, "", evidence)
        context_numbers = {abs(Decimal(m.group("value").replace(",", "")))
                           for m in self._number.finditer(context or "") if not m.group("scale")}
        # A year the executed query or the draft's months already name is a
        # period reference, not a claimed figure. It needs its own admission
        # because the one above holds only when no measure word precedes the
        # number, and "delivery cost across 2026-01 to 2026-06" puts one there.
        context_years = {value for value in context_numbers
                         if value == value.to_integral_value() and 1900 <= value <= 2200}

        missing: list[str] = []
        misattributed: list[str] = []
        boundary = 0
        for match in self._number.finditer(narrative):
            window = narrative[boundary:match.start()]
            window = re.split(r"[.;:\n]", window)[-1]
            boundary = match.end()
            raw = match.group("value").replace(",", "")
            scale = (match.group("scale") or "").strip().lower()
            magnitude = abs(Decimal(raw)) * self._factors[scale]
            explicit_negative = raw.startswith("-")
            label = self._label(window)
            direction = self._direction(window)
            change_claim = bool(direction) and bool(_CHANGE_VERB.search(window.rstrip()))
            signed = -magnitude if explicit_negative else (
                -magnitude if direction == "down" else magnitude if direction == "up" else None)

            # A period reference is not a measure claim at all, so it skips the
            # attribution check rather than being held against whichever measure
            # the surrounding words happen to name: "delivery cost across
            # 2026-01" would otherwise read 2026 as a claim about cost.
            if not scale and magnitude in context_years:
                continue
            if (self._supported(magnitude, evidence)
                    or (magnitude in context_numbers and label is None)):
                keys = self._keys_for(label, change_claim, evidence)
                if keys is None:
                    continue
                pool = [value for key, value in evidence if key in keys]
                if signed is not None and (explicit_negative or change_claim or label in _LEGS):
                    ok = any(value == signed for value in pool)
                else:
                    ok = any(abs(value) == magnitude for value in pool)
                if not ok:
                    misattributed.append(f"{match.group(0).strip()} ({label or 'figure'}"
                                         f"{', ' + direction if direction else ''})")
            else:
                missing.append(str(magnitude if not explicit_negative else -magnitude))
        if missing:
            return False, f"{UNTRACEABLE}: " + ", ".join(sorted(set(missing)))
        if misattributed:
            return False, f"{MISATTRIBUTED}: " + ", ".join(misattributed)
        return True, None

    @staticmethod
    def _label(window: str) -> str | None:
        best: tuple[int, int, str] | None = None
        for name, pattern in _LABEL_RE:
            for found in pattern.finditer(window):
                candidate = (found.end(), found.end() - found.start(), name)
                if best is None or candidate > best:
                    best = candidate
        return best[2] if best else None

    @staticmethod
    def _direction(window: str) -> str | None:
        down = [m.end() for m in _DOWN.finditer(window)]
        up = [m.end() for m in _UP.finditer(window)]
        if not down and not up:
            return None
        return "down" if max(down or [-1]) > max(up or [-1]) else "up"

    @staticmethod
    def _keys_for(label: str | None, change_claim: bool, evidence: list[tuple[str, Decimal]]) -> set[str] | None:
        """The row keys a labelled figure must come from; None when unconstrained."""
        present = {key for key, _ in evidence}
        if label in _LEGS:
            keys = _LEGS[label] & present
            return keys or None
        if label in _LEVEL_WORDS:
            if change_claim:
                # "revenue fell by X": X is a change, so it must be a gap or a
                # leg the query returned, never the level itself.
                deltas = set().union(*_LEGS.values()) & present
                return deltas or {"__no_change_returned__"}
            keys = {key for key in present if _LEVEL_WORDS[label] in key and not key.startswith(("plan_unit", "actual_unit"))}
            return keys or None
        if change_claim:
            deltas = set().union(*_LEGS.values()) & present
            return deltas or None
        return None

    @staticmethod
    def _supported(magnitude: Decimal, evidence: list[tuple[str, Decimal]]) -> bool:
        return any(abs(value) == magnitude for _, value in evidence)

    def _collect(self, value: Any, key: str, output: list[tuple[str, Decimal]]) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float, Decimal)):
            try:
                output.append((key, Decimal(str(value))))
            except InvalidOperation:
                pass
        elif isinstance(value, str):
            # Decimal amounts are serialized as strings by tools/providers.
            if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                output.append((key, Decimal(value)))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._collect(item, key, output)
        elif isinstance(value, dict):
            for item_key, item in value.items():
                self._collect(item, str(item_key).lower(), output)
