"""The recompute arithmetic. No I/O, no clock, no randomness.

Everything here is a pure function of its arguments, which is why the workflow
is allowed to call it directly and why the unit tests need no database. The
activities in ``activities.py`` do the reading and writing around it.

How a driver reaches a plan line
--------------------------------
``planning_model.calc_order_dag`` says which drivers depend on which. It does
not say which plan lines a driver moves, so ``plan_driver_binding`` carries
that: a binding names an account, the column it scales (``quantity`` or
``unit_price``) and an elasticity.

A shock gives a driver an absolute old and new value, so its ratio is
``to / from``. Every driver downstream of it in the DAG is stale and is
recomputed too. The ratio a downstream driver inherits is the shock's ratio:
this codebase has the dependency edges but not the numeric form of each
dependent's formula, so the strength of the response lives in the binding's
elasticity rather than being invented here. That assumption is written into
every line's derivation trace, so an auditor reads it rather than guessing it.

When the snapshot carries the driver formulas, a downstream driver's ratio is
not assumed: it is evaluated. ``evaluate_ratios`` runs each dirty driver's
formula twice, once at the baseline values and once with the shocks applied,
and divides. ``billable_hours = available_hours * utilisation`` then moves by
exactly utilisation's ratio, and ``heads = PRIOR(heads, 1) * (1 - attrition /
12)`` moves by ``(1 - 0.18/12) / (1 - 0.12/12)`` rather than by attrition's
own 1.5. ``PRIOR`` and the other time operators read a period that the shock
does not touch, so they are held at their baseline (a one-period effect); that
assumption is written into the trace. Without formulas (histories recorded
before they were snapshotted) the ratio is inherited as before.

A binding turns a ratio into a factor:

    factor = 1 + elasticity * (ratio - 1)

Elasticity 1.0 is a proportional move, 0.0 is no response, and anything in
between damps it. Factors for the same account and column compose by
multiplication, so two stale drivers hitting one account both count.
"""

from __future__ import annotations

from collections import deque
from decimal import ROUND_HALF_UP, Decimal

from fpa_project.dsl.formula import Binary, Function, Number, Reference, Unary, parse_formula

from .models import AccountFactor, DriverBinding, DriverShock

# Matches plan_version_line.quantity / unit_price, which are Numeric(20, 6).
QUANTUM_6 = Decimal("0.000001")
# Matches amount_functional, Numeric(20, 2), and the check constraint that ties
# it to round(quantity * unit_price, 2).
QUANTUM_2 = Decimal("0.01")


def merge_shocks(committed: list[DriverShock], requested: list[DriverShock]) -> list[DriverShock]:
    """The cumulative shock set a run applies: what is already published, plus this request.

    Every run recomputes from the frozen baseline, so it has to apply every
    driver move that is currently in the published plan, not just its own --
    otherwise approving heads after utilisation publishes a plan without the
    utilisation move, and the earlier approval is silently undone.

    A driver is carried as (baseline value, latest target). A request for a
    driver that is already moved keeps the baseline and replaces the target.
    A move back to the baseline stays in the set as a ratio of one: dropping
    it would leave nothing to recompute while the cube still showed the old
    move. The result is sorted, because it is hashed into the idempotency key
    and written into workflow history.
    """
    # Keyed by driver *and* scope: moving Poland's utilisation and moving
    # everyone's are two different moves, and both stay in the set.
    merged: dict[tuple, DriverShock] = {(s.driver_code, s.scope_key()): s for s in committed}
    for shock in requested:
        key = (shock.driver_code, shock.scope_key())
        earlier = merged.get(key)
        baseline = earlier.from_value if earlier else shock.from_value
        merged[key] = DriverShock(shock.driver_code, baseline, shock.to_value,
                                  list(key[1][0]), list(key[1][1]))
    return [merged[key] for key in sorted(merged)]


def dependents(calc_order_dag: list[dict], driver_code: str) -> list[str]:
    """Every driver downstream of ``driver_code``, in dependency order.

    The DAG arrives as ``[{"driver": "x", "depends_on": ["y"]}, ...]``, which
    is the *upstream* direction, so this walks it backwards. The order matters
    only for readability in the trace, but a deterministic order is worth
    having when the result ends up in workflow history.
    """
    children: dict[str, list[str]] = {}
    order: list[str] = []
    for node in calc_order_dag:
        code = node["driver"]
        order.append(code)
        for parent in node.get("depends_on") or []:
            children.setdefault(parent, []).append(code)

    rank = {code: index for index, code in enumerate(order)}
    seen: set[str] = set()
    queue = deque([driver_code])
    while queue:
        current = queue.popleft()
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sorted(seen, key=lambda code: rank.get(code, len(rank)))


def dirty_drivers(
    calc_order_dag: list[dict], shocks: list[DriverShock], formulas: dict[str, str] | None = None,
) -> dict[str, float]:
    """The stale drivers and the ratio each of them carries.

    With formulas, each ratio is evaluated (``evaluate_ratios``). Without, a
    shocked driver carries its own ratio and a downstream driver inherits the
    ratio of whatever shocked it; when two shocks reach the same driver the
    ratios compose, because both moves are real.
    """
    if formulas:
        return {code: entry["ratio"] for code, entry in evaluate_ratios(calc_order_dag, shocks, formulas).items()}
    ratios: dict[str, float] = {}
    for shock in shocks:
        for code in [shock.driver_code, *dependents(calc_order_dag, shock.driver_code)]:
            ratios[code] = ratios.get(code, 1.0) * shock.ratio()
    return ratios


# Held at baseline by the ratio evaluation: they read another period, and a
# shock to this period does not reach back into it.
_HELD_FUNCTIONS = frozenset({"PRIOR", "LEAD", "YOY", "CAGR", "YTD", "QTD", "MTD", "ROLLING", "SUM", "AVG", "BY", "WHERE"})


def _evaluate(node, values: dict[str, float], drivers: frozenset[str] = frozenset()) -> float:
    """A driver formula's value. Arithmetic only: no eval, no I/O, no clock."""
    if isinstance(node, Number):
        return float(node.value)
    if isinstance(node, Reference):
        if node.name in values:
            return values[node.name]
        if node.name in drivers:
            # Evaluated out of order: defaulting would silently make it 1.0.
            raise KeyError(f"driver {node.name!r} read before it was evaluated")
        # A metric (not a driver) is not moved by a driver shock: held at 1.0,
        # which cancels in the ratio exactly as a held baseline value would.
        return 1.0
    if isinstance(node, Unary):
        return -_evaluate(node.operand, values, drivers)
    if isinstance(node, Binary):
        left, right = _evaluate(node.left, values, drivers), _evaluate(node.right, values, drivers)
        if node.operator == "+":
            return left + right
        if node.operator == "-":
            return left - right
        if node.operator == "*":
            return left * right
        if node.operator == "/":
            if right == 0:
                raise ZeroDivisionError("division by zero in a driver formula")
            return left / right
        if node.operator == "^":
            return left ** right
        raise ValueError(f"unknown operator {node.operator!r}")
    if isinstance(node, Function):
        if node.name in _HELD_FUNCTIONS:
            return 1.0
        args = [_evaluate(argument, values, drivers) for argument in node.arguments]
        if node.name == "MIN":
            return min(args)
        if node.name == "MAX":
            return max(args)
        if node.name == "ABS":
            return abs(args[0])
        if node.name == "ROUND":
            return float(round(args[0], int(args[1]) if len(args) > 1 else 0))
        raise ValueError(f"unknown function {node.name!r}")
    raise TypeError(f"not a formula node: {node!r}")


def evaluate_ratios(
    calc_order_dag: list[dict], shocks: list[DriverShock], formulas: dict[str, str],
) -> dict[str, dict]:
    """Each dirty driver's old value, new value and ratio, from its formula.

    Shocked drivers are pinned: at ``from_value`` in the baseline pass and at
    ``to_value`` in the shocked pass. Every other driver is its formula,
    evaluated in DAG order on the values before it. The result carries the
    formula, the two values and the ratio, which is what the derivation trace
    records. A dependent whose baseline evaluates to zero has no ratio; it
    inherits its inputs' ratio and the trace says so, rather than dividing by
    zero or silently dropping the move.
    """
    pinned_old = {s.driver_code: s.from_value for s in shocks}
    pinned_new = {s.driver_code: s.to_value for s in shocks}
    dirty: set[str] = set()
    for shock in shocks:
        dirty.update([shock.driver_code, *dependents(calc_order_dag, shock.driver_code)])

    order = _topological(calc_order_dag, formulas)
    drivers = frozenset(formulas) | frozenset(pinned_old)
    old: dict[str, float] = {}
    new: dict[str, float] = {}
    out: dict[str, dict] = {}
    for code in order:
        formula = formulas.get(code)
        if code in pinned_old:
            old[code], new[code] = pinned_old[code], pinned_new[code]
        elif formula is None:
            continue
        else:
            node = parse_formula(formula)
            try:
                old[code], new[code] = _evaluate(node, old, drivers), _evaluate(node, new, drivers)
            except ZeroDivisionError:
                old[code] = new[code] = 0.0
        if code not in dirty:
            continue
        entry = {"formula": formula or "", "from": old[code], "to": new[code],
                 "shocked_directly": code in pinned_old}
        if old[code] != 0:
            entry["ratio"] = new[code] / old[code]
        else:
            upstream = [out[p]["ratio"] for p in _parents(calc_order_dag, code) if p in out]
            ratio = 1.0
            for value in upstream:
                ratio *= value
            entry["ratio"] = ratio
            entry["note"] = "baseline evaluates to zero; ratio inherited from its inputs"
        out[code] = entry
    # Drivers the DAG names but that have no formula (not in the snapshot)
    # still have to move if a shock reaches them: inherit, as before.
    for shock in shocks:
        for code in dependents(calc_order_dag, shock.driver_code):
            if code not in out:
                out[code] = {"formula": "", "ratio": shock.ratio(), "shocked_directly": False,
                             "note": "no formula in the snapshot; ratio inherited"}
    return out


def _topological(calc_order_dag: list[dict], formulas: dict[str, str]) -> list[str]:
    """Every driver after the drivers it depends on.

    ``calc_order_dag`` is stored in whatever order the model was saved in (the
    seeded one is alphabetical), so it is sorted here rather than trusted.
    Ties keep the declared order, because the result shapes workflow history.
    A cycle cannot reach here -- saving the model refuses one -- but if it
    did, the remaining drivers are appended rather than dropped.
    """
    declared = [node["driver"] for node in calc_order_dag]
    declared += sorted(set(formulas) - set(declared))
    parents = {code: set(_parents(calc_order_dag, code)) & set(declared) for code in declared}
    done: list[str] = []
    placed: set[str] = set()
    while len(done) < len(declared):
        ready = [code for code in declared if code not in placed and parents[code] <= placed]
        if not ready:
            done += [code for code in declared if code not in placed]
            break
        done += ready
        placed.update(ready)
    return done


def _parents(calc_order_dag: list[dict], code: str) -> list[str]:
    for node in calc_order_dag:
        if node["driver"] == code:
            return list(node.get("depends_on") or [])
    return []


def account_factors(ratios: dict[str, float], bindings: list[DriverBinding]) -> list[AccountFactor]:
    """Collapse the stale drivers onto the accounts and columns they move.

    The workflow sends this list to every child, so it is sorted: an unordered
    map would make two identical runs produce different workflow histories.
    """
    composed: dict[tuple[str, str], float] = {}
    sources: dict[tuple[str, str], list[dict]] = {}
    for binding in bindings:
        ratio = ratios.get(binding.driver_code)
        if ratio is None:
            continue
        factor = 1.0 + binding.elasticity * (ratio - 1.0)
        key = (binding.account_code, binding.target)
        composed[key] = composed.get(key, 1.0) * factor
        sources.setdefault(key, []).append({
            "driver": binding.driver_code, "elasticity": binding.elasticity,
            "ratio": round(ratio, 10), "factor": round(factor, 10),
        })
    return [
        AccountFactor(account_code=account, target=target, factor=factor, sources=sources[(account, target)])
        for (account, target), factor in sorted(composed.items())
    ]


def plan_factors(
    calc_order_dag: list[dict], shocks: list[DriverShock], bindings: list[DriverBinding],
    formulas: dict[str, str] | None = None,
) -> tuple[list[AccountFactor], dict[str, dict]]:
    """Factors for a whole shock set, each carrying the scope it applies to.

    Shocks are grouped by scope, and each group goes through exactly the
    ``dirty_drivers`` -> ``account_factors`` path an unscoped shock set always
    went through, so a set with no scoped shock gives the same factors, in the
    same order, as before scoping existed. Also returns the per-driver trace
    written into every line's derivation trace.
    """
    groups: dict[tuple, list[DriverShock]] = {}
    for shock in shocks:
        groups.setdefault(shock.scope_key(), []).append(shock)
    factors: list[AccountFactor] = []
    trace: dict[str, dict] = {}
    for scope in sorted(groups):
        companies, months = list(scope[0]), list(scope[1])
        members = groups[scope]
        evaluated = evaluate_ratios(calc_order_dag, members, formulas) if formulas else {}
        ratios = ({code: entry["ratio"] for code, entry in evaluated.items()}
                  if formulas else dirty_drivers(calc_order_dag, members))
        label = _scope_label(companies, months)
        for factor in account_factors(ratios, bindings):
            factor.companies, factor.months = companies, months
            for source in factor.sources:
                source["trace_key"] = f"{source['driver']} [{label}]" if label else source["driver"]
            factors.append(factor)
        for code, ratio in sorted(ratios.items()):
            entry = {"ratio": round(ratio, 10), "shocked_directly": any(s.driver_code == code for s in members)}
            if code in evaluated:
                detail = evaluated[code]
                entry["formula"] = detail.get("formula", "")
                if "from" in detail:
                    entry["from"], entry["to"] = round(detail["from"], 10), round(detail["to"], 10)
                if "note" in detail:
                    entry["note"] = detail["note"]
            if label:
                entry["scope"] = {"companies": companies, "months": months}
            trace[f"{code} [{label}]" if label else code] = entry
    return factors, trace


def dirty_scopes(factors: list[AccountFactor]) -> list[tuple[list[str], list[str], list[str]]]:
    """The dirty set as (accounts, companies, months) groups, one per scope.

    A line is dirty when some factor applies to it: its account is bound and it
    sits inside that factor's companies and months (empty means all). Grouped
    by scope and sorted, because the result is an activity argument.
    """
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], set[str]] = {}
    for factor in factors:
        key = (tuple(sorted(factor.companies)), tuple(sorted(factor.months)))
        grouped.setdefault(key, set()).add(factor.account_code)
    return [(sorted(accounts), list(companies), list(months)) for (companies, months), accounts in sorted(grouped.items())]


def _scope_label(companies: list[str], months: list[str]) -> str:
    parts = []
    if companies:
        parts.append(",".join(companies))
    if months:
        parts.append(months[0][:7] if len(months) == 1 else f"{months[0][:7]}..{months[-1][:7]}")
    return " ".join(parts)


def affected_accounts(factors: list[AccountFactor]) -> list[str]:
    """The accounts whose lines are dirty. A factor of exactly 1.0 still counts:
    it means a binding matched and the trace should say so."""
    return sorted({factor.account_code for factor in factors})


def recompute_line(
    quantity: float, unit_price: float, factors: list[AccountFactor], account_code: str,
    company: str | None = None, month: str | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """One line's new quantity, unit price and amount.

    Returns ``Decimal`` at the column's own precision rather than floats,
    because ``plan_version_line`` has a check constraint tying the amount to
    ``round(quantity * unit_price, 2)``. Rounding the inputs first and then
    multiplying is what makes the constraint hold; multiplying the unrounded
    floats and rounding once does not.
    """
    quantity_factor = 1.0
    price_factor = 1.0
    for factor in factors:
        if factor.account_code != account_code or not factor.applies(company, month):
            continue
        if factor.target == "quantity":
            quantity_factor *= factor.factor
        elif factor.target == "unit_price":
            price_factor *= factor.factor

    new_quantity = Decimal(str(quantity * quantity_factor)).quantize(QUANTUM_6, rounding=ROUND_HALF_UP)
    new_price = Decimal(str(unit_price * price_factor)).quantize(QUANTUM_6, rounding=ROUND_HALF_UP)
    amount = (new_quantity * new_price).quantize(QUANTUM_2, rounding=ROUND_HALF_UP)
    return new_quantity, new_price, amount


def derivation_trace(
    account_code: str,
    factors: list[AccountFactor],
    shock_trace: dict[str, dict],
    company: str | None = None,
    month: str | None = None,
    baseline: tuple[float, float] | None = None,
    result: tuple[Decimal, Decimal, Decimal] | None = None,
) -> dict:
    """What went into this line: the driver, the formula and the inputs.

    ``driver`` names the shocked driver(s) whose move reaches this line,
    ``formula`` is the arithmetic that produced it, and ``inputs`` holds the
    line's own baseline quantity and price, each driver's formula and its
    before/after values, and every binding (elasticity, ratio, factor) that
    applied. Migration 016 refuses a trace without those three keys, so a line
    that cannot say where its number came from cannot be saved.
    """
    applied = [f for f in factors if f.account_code == account_code and f.applies(company, month)]
    composed: dict[str, float] = {}
    bindings: list[dict] = []
    for factor in applied:
        composed[factor.target] = composed.get(factor.target, 1.0) * factor.factor
        for source in factor.sources:
            # "via" names the move, scope included: two moves of one driver
            # (a whole-plan one and a Poland-only one) are two bindings.
            binding = {"target": factor.target, **{k: v for k, v in source.items() if k != "trace_key"}}
            binding["via"] = source.get("trace_key", source["driver"])
            bindings.append(binding)

    # The scope groups that reached this line, and every driver of those
    # groups: the bound ones and the shocked ones upstream of them.
    keys = {s["trace_key"] for f in applied for s in f.sources if "trace_key" in s}
    labels = {_label_of(key) for key in keys} if keys else {_label_of(key) for key in shock_trace}
    drivers = {key: entry for key, entry in sorted(shock_trace.items()) if _label_of(key) in labels}
    shocked = sorted({key.split(" [")[0] for key, entry in drivers.items() if entry.get("shocked_directly")})
    parts = []
    for target in ("quantity", "unit_price"):
        legs = [f"(1 + {b['elasticity']:g} x (ratio of {b['via']} - 1))" for b in bindings if b["target"] == target]
        parts.append(f"{target} = baseline_{target}" + (" x " + " x ".join(legs) if legs else ""))
    parts.append("amount = round(quantity, 6) x round(unit_price, 6)")

    inputs: dict = {"drivers": drivers, "bindings": bindings,
                    "applied_factors": {target: round(value, 10) for target, value in composed.items()}}
    if any("formula" in entry for entry in drivers.values()):
        inputs["assumption"] = ("each dependent's ratio is its formula evaluated at the shocked values over the "
                                "baseline values; PRIOR and the other time operators read an untouched period and "
                                "are held at 1.0, so from/to of a driver built on them are relative, the ratio exact")
    if baseline is not None:
        inputs["baseline"] = {"quantity": baseline[0], "unit_price": baseline[1]}
    trace = {
        "method": "driver_elasticity",
        "account": account_code,
        "driver": ", ".join(shocked) if shocked else ", ".join(sorted({b["driver"] for b in bindings})) or "none",
        "formula": "; ".join(parts),
        "inputs": inputs,
    }
    if result is not None:
        trace["result"] = {"quantity": str(result[0]), "unit_price": str(result[1]), "amount": str(result[2])}
    return trace


def _label_of(trace_key: str) -> str:
    return trace_key.split(" [", 1)[1] if " [" in trace_key else ""


def partition_plan(
    scenario_codes: list[str],
    period_months: list[str],
    row_counts: dict[tuple[str, str], int],
    target_size: int,
) -> list[tuple[str, list[str], int]]:
    """Group (scenario, month) cells into partitions of roughly ``target_size``.

    Months are the natural seam: a plan line belongs to exactly one, so no two
    partitions can write the same row and the children never contend. A cell
    bigger than the target becomes its own partition rather than being split
    further, because splitting inside a month would mean two children sharing
    a key.
    """
    partitions: list[tuple[str, list[str], int]] = []
    for scenario in scenario_codes:
        current: list[str] = []
        current_rows = 0
        for month in period_months:
            rows = row_counts.get((scenario, month), 0)
            if rows == 0:
                continue
            if current and current_rows + rows > target_size:
                partitions.append((scenario, current, current_rows))
                current, current_rows = [], 0
            current.append(month)
            current_rows += rows
        if current:
            partitions.append((scenario, current, current_rows))
    return partitions
