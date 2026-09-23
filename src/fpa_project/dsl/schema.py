from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import DSLValidationError


DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "schema_snapshot.json"


def load_schema() -> dict[str, Any]:
    # The checked-in snapshot is the stable contract with the seeded cube.
    with DATA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class Schema:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or load_schema()
        # period_month is expressed through FOR PERIOD, not as a BY axis.
        self.dimensions = set(self.data["planning_dimensions"]) | set(self.data["separate_axes"]) - {"period_month"}
        self.metrics = self.data["metrics"]
        self.metric_names = set(self.metrics)
        self.driver_names = set(self.data.get("drivers", []))
        self.scenarios = set(self.data["scenarios"])

    def require_dimension(self, name: str) -> None:
        if name not in self.dimensions:
            raise DSLValidationError(f"unknown dimension: {name}")

    def require_metric(self, name: str) -> None:
        if name not in self.metric_names:
            raise DSLValidationError(f"unknown measure: {name}")
        if not self.metrics[name].get("available", True):
            raise DSLValidationError(f"measure is not available in the seed schema: {name}")

    def require_scenario(self, name: str) -> None:
        if name not in self.scenarios:
            raise DSLValidationError(f"unknown scenario: {name}")
