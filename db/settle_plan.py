"""Take the seeded plan from DRAFT to LOCKED, so a planner can re-forecast it.

The seeded plan starts DRAFT, and db.import_plan_lines fills in its base lines
while it is still writable (migration 016 freezes a LOCKED version's lines), so
this has to run after that import. A re-forecast needs its source plan settled
-- APPROVED, LOCKED or SUPERSEDED -- which meant that on every fresh stack the
first thing anyone had to do was walk PV-2026-0001 through its three gates by
hand (`make plan-lock`) before the product could be used at all.

The walk goes through the same governance calls the API serves, as the same
three people, so every role check, the covenant gate, segregation of duties and
the audit trail apply exactly as they would from the Plans tab. Nothing here
can move a re-forecast: those belong to their run's gates.

Idempotent: a version part-way through resumes from wherever it is, and one
already LOCKED or SUPERSEDED is left alone.

    python -m db.settle_plan [--plan PV-2026-0001]
"""

from __future__ import annotations

import argparse

from fpa_project import governance
from fpa_project.identities import CFO, CONTROLLER, PLANNER

SETTLED = ("APPROVED", "LOCKED", "SUPERSEDED")
NOTE = "seeded plan: covenants reviewed at bootstrap"


def settle(plan: str) -> str:
    state = governance.describe(plan)["state"]
    if state in SETTLED:
        print(f"==> {plan} is already {state}; nothing to settle")
        return state
    if state == "REJECTED":
        # There is no way back to DRAFT, so a new plan is the only way on.
        print(f"==> {plan} is REJECTED and stays rejected; not settling it")
        return state

    if state == "DRAFT":
        state = governance.transition(plan, "IN_REVIEW", actor=PLANNER, note=NOTE)["state"]
        print(f"==> {plan} DRAFT -> IN_REVIEW (planner submitted)")
    if state == "IN_REVIEW":
        # The covenant gate: an original plan has nothing recomputed for the
        # system to measure, so a controller records the verdict by hand.
        governance.set_covenant(plan, covenant_ok=True, note=NOTE, actor=CONTROLLER)
        state = governance.transition(plan, "APPROVED", actor=CONTROLLER, note=NOTE)["state"]
        print(f"==> {plan} IN_REVIEW -> APPROVED (controller approved, covenant recorded)")
    if state == "APPROVED":
        # A different person than the one who approved, as the lock demands.
        state = governance.transition(plan, "LOCKED", actor=CFO, note=NOTE)["state"]
        print(f"==> {plan} APPROVED -> LOCKED (cfo locked); it can now be re-forecast")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", default="PV-2026-0001")
    args = parser.parse_args()
    settle(args.plan)


if __name__ == "__main__":
    main()
