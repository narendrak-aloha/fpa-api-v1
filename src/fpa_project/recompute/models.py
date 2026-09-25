"""Everything that crosses the workflow/activity boundary.

Temporal's default JSON converter handles dataclasses of primitives, so these
carry ``str``/``float``/``int`` only. Dates travel as ISO strings and money
becomes ``Decimal`` again inside the activity that writes it, where the
precision has to match the column.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Workflow phases, in the order the parent moves through them. The progress
# query returns one of these; the UI renders it without knowing the internals.
PHASES = (
    "STARTING",
    "SNAPSHOT",
    "RESOLVING_DIRTY_SET",
    "RECOMPUTING",
    "SAVING_DRAFT",
    "COVENANT_CHECK",
    "AWAITING_APPROVAL",
    "PUBLISHING",
    "COMMITTING",
    "COMPENSATING",
    "VARIANCE",
    "DONE",
)


@dataclass
class DriverShock:
    """One driver moving from one value to another.

    Both ends are absolute, never a delta. That is what makes a re-run of the
    same shock produce the same numbers instead of compounding: the recompute
    always starts from the frozen baseline, so ``to_value`` is the answer
    regardless of how many times it has been applied.
    """

    driver_code: str
    from_value: float
    to_value: float
    # Optional scope. Empty means every company / every month, which is what
    # every shock meant before scoping existed; an unscoped shock serialises
    # exactly as it always did, so its idempotency key and recorded
    # histories are unchanged. Months are first-of-month ISO dates.
    companies: list[str] = field(default_factory=list)
    months: list[str] = field(default_factory=list)

    def ratio(self) -> float:
        return self.to_value / self.from_value

    @property
    def scoped(self) -> bool:
        return bool(self.companies or self.months)

    def scope_key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple(sorted(self.companies)), tuple(sorted(self.months))

    def request_key(self) -> list[Any]:
        """Which move this is, whatever its values: driver and scope."""
        return [self.driver_code, sorted(self.companies), sorted(self.months)]

    def as_list(self) -> list[Any]:
        """The shape stored in plan_publication.shocks and hashed into the key."""
        head: list[Any] = [self.driver_code, self.from_value, self.to_value]
        return head + [sorted(self.companies), sorted(self.months)] if self.scoped else head

    @classmethod
    def from_list(cls, item: list[Any]) -> "DriverShock":
        code, start, end, *scope = item
        companies, months = (scope + [[], []])[:2]
        return cls(str(code), float(start), float(end), list(companies or []), list(months or []))


@dataclass
class RecomputeInput:
    plan_version_code: str
    shocks: list[DriverShock]
    requested_by: str
    scenario_codes: list[str] = field(default_factory=lambda: ["base", "stretch", "downside"])
    # Both of these are settings, and both travel in the input rather than
    # being read from the environment inside the workflow. A config value that
    # could differ between the original run and its replay is exactly the kind
    # of thing that breaks determinism; carried here, it is pinned in history.
    approval_timeout_hours: int = 72
    partition_size: int = 5_000
    # Zero preserves old histories; positive values force a bounded history.
    continue_after_partitions: int = 0
    # Set for a question-driven request, whose shock is fixed once confirmed
    # (no second shock is folded in). Every run's covenants are checked by the
    # system when the planner submits; the covenant scope is the request's own,
    # carried separately because the shock set above becomes cumulative once
    # merged. Empty on a manual run: the whole plan is measured.
    covenant_gate: bool = False
    request_id: str = ""
    covenant_companies: list[str] = field(default_factory=list)
    covenant_months: list[str] = field(default_factory=list)
    # [driver, companies, months] of each shock this run was asked for, as
    # opposed to the ones merged in from the published plan. The dirty set is
    # their scope: lines only an earlier, committed shock moved are already
    # live at the right value and are not rewritten. Set on the first run and
    # carried through continue-as-new, so a resumed run cuts the same
    # partitions; empty (every older history) means every shock is requested.
    requested_keys: list[list[Any]] = field(default_factory=list)
    # Set only by continue-as-new; a fresh run always starts empty.
    resume: ResumeState | None = None

    def idempotency_key(self) -> str:
        """Stable across retries, worker restarts and continue-as-new.

        Derived from the inputs alone, so the same re-forecast asked for twice
        reserves the same cube revision and republishes the same rows instead
        of stacking a second one on top.
        """
        payload = json.dumps(
            {
                "plan_version": self.plan_version_code,
                "scenarios": sorted(self.scenario_codes),
                "shocks": sorted(
                    [s.as_list() for s in self.shocks],
                    key=lambda item: (item[0], json.dumps(item[3:])),
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass
class ResumeState:
    """What a continue-as-new run carries forward.

    Only the cursor and the counters: everything else is re-derived, which
    keeps the carried payload small enough that it can never itself become the
    reason history grows.
    """

    revision: int
    target_version_code: str
    partitions_done: int
    processed_rows: int
    dirty_rows: int
    # How many times this logical run has continued as new so far.
    continued_runs: int = 0


@dataclass
class PlanContext:
    """The governance facts the workflow needs, read once and then frozen."""

    plan_version_id: str
    plan_version_code: str
    model_id: str
    plan_year: int
    state: str
    requested_by: str
    calc_order_dag: list[dict]


@dataclass
class TargetVersion:
    """The successor plan version this run drafts into, and what state it is in.

    Both states are read in the same activity because the workflow's next move
    depends on the pair: a publication that is already COMMITTED means this
    exact re-forecast has been done, and a successor that is already LOCKED
    while nothing was published means an earlier run stopped somewhere it
    should not have.
    """

    plan_version_code: str
    plan_version_id: str
    version_state: str
    publication_state: str
    published_by: str


@dataclass
class DriverSnapshot:
    """The driver library frozen at the effective date, plus the bindings.

    ``bindings`` is a flat list rather than a nested map because Temporal has
    to serialize it and a flat list diffs readably in workflow history.
    """

    effective_date: str
    driver_codes: list[str]
    bindings: list[DriverBinding]
    # driver_code -> FinOpsExpr formula, frozen with the rest of the library.
    # Empty in histories recorded before formulas were snapshotted; the engine
    # then falls back to inheriting ratios down the DAG, as it always did.
    formulas: dict[str, str] = field(default_factory=dict)


@dataclass
class DriverBinding:
    driver_code: str
    account_code: str
    target: str
    elasticity: float


@dataclass
class Partition:
    """One slice of the dirty set, sized so a child workflow finishes quickly."""

    index: int
    scenario_code: str
    period_months: list[str]
    row_count: int


@dataclass
class PartitionInput:
    plan_version_code: str
    target_version_id: str
    revision: int
    partition: Partition
    factors: list[AccountFactor]
    # Mixed values (a ratio and a flag), so not typed as float: the converter
    # would otherwise coerce True into 1.0 and the trace would say so forever.
    shock_trace: dict[str, dict[str, Any]]
    # The dirty set as [accounts, companies, months] groups. Empty (older
    # histories) means every line some factor applies to.
    scopes: list[list[list[str]]] = field(default_factory=list)


@dataclass
class AccountFactor:
    """The composed multiplier for one account and one target column."""

    account_code: str
    target: str
    factor: float
    # Empty means the factor applies to every line of the account.
    companies: list[str] = field(default_factory=list)
    months: list[str] = field(default_factory=list)
    # The bindings this factor was composed from, for the derivation trace:
    # [{"driver", "elasticity", "ratio", "factor"}, ...].
    sources: list[dict[str, Any]] = field(default_factory=list)

    def applies(self, company: str | None, month: str | None) -> bool:
        if self.companies and company not in self.companies:
            return False
        if self.months and (month or "")[:10] not in self.months:
            return False
        return True


@dataclass
class PartitionResult:
    index: int
    rows_written: int


@dataclass
class Progress:
    """What the progress query returns while the run is still going."""

    phase: str
    dirty_rows: int
    processed_rows: int
    partitions_total: int
    partitions_done: int
    revision: int
    target_version_code: str
    approval_state: str
    # [driver, from, to] or, when scoped, [driver, from, to, companies, months]
    shocks: list[list[Any]]
    continued_runs: int
    # Decisions refused while parked, newest last. The run is still waiting.
    refusals: list[str] = field(default_factory=list)


@dataclass
class ApprovalDecision:
    """The payload of the approval signal."""

    approved: bool
    decided_by: str
    comment: str = ""


@dataclass
class RecomputeResult:
    outcome: str
    revision: int
    target_version_code: str
    dirty_rows: int
    published_rows: int
    commitment_ids: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class CovenantInput:
    target_version_id: str
    plan_version_code: str
    revision: int
    scenario_codes: list[str]
    run_id: str
    workflow_id: str
    request_id: str
    companies: list[str]
    months: list[str]


@dataclass
class CovenantVerdict:
    passed: bool
    checks: int
    failures: list[str]
