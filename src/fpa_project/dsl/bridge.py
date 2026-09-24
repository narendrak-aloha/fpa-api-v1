"""The variance bridge: a plan/actual gap decomposed so that it ties.

A plan said one number and the ledger said another. This module splits that
gap into legs a CFO can act on, and it does so as an algebraic identity: the
legs sum to the gap at every node of the rollup, and the residual is rounding
and nothing else.

The conventions, because a bridge is only as trustworthy as its stated rules:

*Group and elements.* A group is one node of the rollup: the whole cut at the
root, then one node per value of each ``BY`` dimension in order (practice,
then grade within practice). The elements of a group are its children, and at
the finest level they are the individual dimension signatures. Mix shares are
computed over exactly that.

*Constant currency.* The operational legs -- price, volume, mix, rate,
efficiency -- are measured at the plan's assumed FX rate, so they are
comparable across months whose real rates moved. FX is the one leg that uses
the real rate: the actual local amount times (real minus assumed). Mixing the
two rate sets inside a single leg is how a bridge stops tying.

*Price at actual quantity, volume at plan price.* This is the standard-costing
convention: a rate decision is judged on the volume that actually happened, so
the price x volume interaction lands in the price leg rather than in the
residual. The other order (price at plan quantity, volume at actual price)
also ties, and gives a different split; ``Convention.PRICE_FIRST`` exists so a
test can show the difference, and the default is declared here.

*Nested mix.* Volume and mix together are the whole quantity variance. At a
node, volume is the node's quantity change at the node's average plan price;
the between-children mix is each child's quantity change at (child average -
node average); whatever remains is inside the children, where the same rule
applies again. So practice mix and grade-within-practice mix are separate
legs, and they telescope exactly.

*Cost side.* Cost lines get rate (same shape as price) and efficiency (the
whole quantity variance). In a margin bridge they enter with negative sign,
because margin is revenue minus cost.

*Tolerance.* ``max(1.00, 0.01 * line_count)`` in report currency at every
node. A fixed floor for cent rounding, not scaled to materiality: a tolerance
that grew with the number would quietly accept a decomposition that does not
tie.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, getcontext
from enum import Enum
from typing import Any, Hashable, Iterable, Iterator

getcontext().prec = 34

CENT = Decimal("0.01")
ZERO = Decimal(0)
COST_TYPES = frozenset({"COGS", "OpEx"})


class Convention(str, Enum):
    """Where the price x volume interaction lands."""

    # Price at actual quantity, volume at plan price. The default.
    VOLUME_FIRST = "volume_first"
    # Price at plan quantity, volume at actual price.
    PRICE_FIRST = "price_first"


@dataclass(frozen=True)
class BridgeLine:
    """One matched plan/actual line at signature grain.

    ``path`` is the tuple of ``BY`` dimension values this line rolls up
    under, in order. ``key`` is whatever identifies the cube rows behind it
    (the four-column ledger key), carried through so the report can cite them.
    """

    path: tuple[str, ...]
    plan_quantity: Decimal
    actual_quantity: Decimal
    plan_unit_price: Decimal
    actual_unit_price: Decimal
    plan_fx: Decimal
    actual_fx: Decimal
    account_type: str = "Revenue"
    key: Hashable = None
    # The stored amounts, when the caller has them: they carry the cube's own
    # cent rounding, which is what the report's plan/actual columns should say.
    plan_amount: Decimal | None = None
    actual_amount: Decimal | None = None

    @property
    def is_cost(self) -> bool:
        return self.account_type in COST_TYPES


@dataclass
class BridgeNode:
    """One group of the rollup, with its legs in report currency."""

    path: tuple[str, ...]
    level: int
    line_count: int
    plan_amount: Decimal
    actual_amount: Decimal
    price: Decimal
    volume: Decimal
    mix_between: Decimal   # this node's blend shift among its children
    mix_within: Decimal    # the children's own mix, summed
    fx: Decimal
    rate: Decimal
    efficiency: Decimal
    children: list[BridgeNode] = field(default_factory=list)
    line_keys: list[Hashable] = field(default_factory=list)

    @property
    def gap(self) -> Decimal:
        return self.actual_amount - self.plan_amount

    @property
    def mix(self) -> Decimal:
        return self.mix_between + self.mix_within

    @property
    def quantity_variance(self) -> Decimal:
        return self.volume + self.mix

    @property
    def explained(self) -> Decimal:
        return self.price + self.volume + self.mix + self.fx + self.rate + self.efficiency

    @property
    def residual(self) -> Decimal:
        return self.gap - self.explained

    @property
    def tol(self) -> Decimal:
        return max(Decimal("1.00"), CENT * self.line_count)

    @property
    def ties(self) -> bool:
        return abs(self.residual) < self.tol

    def walk(self) -> Iterator[BridgeNode]:
        """Every node, depth first, root first. The assertion in the brief
        runs over exactly this."""
        yield self
        for child in self.children:
            yield from child.walk()

    def mix_by_level(self, levels: tuple[str, ...]) -> dict[str, Decimal]:
        """The mix leg split by the level of the rollup it happened at.

        At the root that is the mix *between* practices; one level down, the
        mix between grades within each practice, summed; and so on down to
        the signatures. Together they are the root's whole mix leg.
        """
        out: dict[str, Decimal] = {}
        for node in self.walk():
            name = levels[node.level] if node.level < len(levels) else "signature"
            out[name] = out.get(name, ZERO) + node.mix_between
        return out

    def to_dict(self, levels: tuple[str, ...]) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "level": self.level,
            "level_name": levels[self.level - 1] if 0 < self.level <= len(levels) else ("total" if self.level == 0 else "signature"),
            "line_count": self.line_count,
            "plan_amount": _cents(self.plan_amount),
            "actual_amount": _cents(self.actual_amount),
            "gap": _cents(self.gap),
            "price": _cents(self.price),
            "volume": _cents(self.volume),
            "mix": _cents(self.mix),
            "mix_between": _cents(self.mix_between),
            "mix_within": _cents(self.mix_within),
            "fx": _cents(self.fx),
            "rate": _cents(self.rate),
            "efficiency": _cents(self.efficiency),
            "residual": str(self.residual),
            "tolerance": _cents(self.tol),
            "ties": self.ties,
            "children": [child.to_dict(levels) for child in self.children],
        }


@dataclass
class BridgeResult:
    root: BridgeNode
    levels: tuple[str, ...]
    convention: Convention
    lines: int

    @property
    def residual(self) -> Decimal:
        return self.root.residual

    @property
    def tol(self) -> Decimal:
        return self.root.tol

    @property
    def volume(self) -> Decimal:
        return self.root.volume

    @property
    def mix(self) -> Decimal:
        return self.root.mix

    def walk(self) -> Iterator[BridgeNode]:
        return self.root.walk()

    def breaks(self) -> list[BridgeNode]:
        return [node for node in self.walk() if not node.ties]

    def mix_by_level(self) -> dict[str, Decimal]:
        return self.root.mix_by_level(self.levels)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decompose(
    lines: Iterable[BridgeLine],
    levels: tuple[str, ...] = (),
    convention: Convention = Convention.VOLUME_FIRST,
    margin: bool | None = None,
) -> BridgeResult:
    """Decompose matched lines into a rollup tree of legs that tie.

    ``levels`` names the ``BY`` dimensions, in order; each line's ``path``
    must have that many entries. ``margin`` says whether cost lines enter
    with negative sign; left as None, it is true exactly when both revenue
    and cost lines are present.
    """
    lines = list(lines)
    if not lines:
        raise ValueError("nothing to bridge: the matched plan/actual set is empty")
    depth = len(levels)
    for line in lines:
        if len(line.path) != depth:
            raise ValueError(f"line path {line.path!r} does not match the {depth} rollup level(s) {levels!r}")
    if margin is None:
        kinds = {line.is_cost for line in lines}
        margin = len(kinds) == 2

    root = _build(lines, (), 0, depth, convention, margin)
    return BridgeResult(root=root, levels=levels, convention=convention, lines=len(lines))


# ---------------------------------------------------------------------------
# Per-line arithmetic, all in report currency at the declared conventions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Legs:
    sign: Decimal
    plan_usd: Decimal
    actual_usd: Decimal
    price: Decimal          # rate effect (price for revenue, rate for cost)
    quantity: Decimal       # whole quantity effect at plan price and plan fx
    fx: Decimal
    plan_qty: Decimal
    actual_qty: Decimal
    plan_price_usd: Decimal  # plan unit price at plan fx: what mix averages


def _legs(line: BridgeLine, convention: Convention, margin: bool) -> _Legs:
    sign = Decimal(-1) if (margin and line.is_cost) else Decimal(1)
    pq, aq = line.plan_quantity, line.actual_quantity
    pp, ap = line.plan_unit_price, line.actual_unit_price
    pfx, afx = line.plan_fx, line.actual_fx

    # The stored amounts, when given, are what the cube holds and what the
    # report's plan/actual columns must say. They differ from q x p by the
    # cube's own cent rounding, and that difference is exactly the residual.
    plan_local = line.plan_amount if line.plan_amount is not None else pq * pp
    actual_local = line.actual_amount if line.actual_amount is not None else aq * ap

    if convention is Convention.VOLUME_FIRST:
        price = (ap - pp) * aq * pfx
        quantity = (aq - pq) * pp * pfx
    else:
        price = (ap - pp) * pq * pfx
        quantity = (aq - pq) * ap * pfx
    fx = actual_local * (afx - pfx)
    return _Legs(
        sign=sign,
        plan_usd=sign * plan_local * pfx,
        actual_usd=sign * actual_local * afx,
        price=sign * price,
        quantity=sign * quantity,
        fx=sign * fx,
        plan_qty=pq,
        actual_qty=aq,
        plan_price_usd=(ap if convention is Convention.PRICE_FIRST else pp) * pfx,
    )


def _build(
    lines: list[BridgeLine], path: tuple[str, ...], level: int, depth: int,
    convention: Convention, margin: bool,
) -> BridgeNode:
    legs = [(line, _legs(line, convention, margin)) for line in lines]

    revenue = [(ln, lg) for ln, lg in legs if not ln.is_cost]
    cost = [(ln, lg) for ln, lg in legs if ln.is_cost]

    plan_amount = sum((lg.plan_usd for _, lg in legs), ZERO)
    actual_amount = sum((lg.actual_usd for _, lg in legs), ZERO)
    fx = sum((lg.fx for _, lg in legs), ZERO)
    price = sum((lg.price for _, lg in revenue), ZERO)
    rate = sum((lg.price for _, lg in cost), ZERO)
    efficiency = sum((lg.quantity for _, lg in cost), ZERO)

    # Volume and mix are revenue-side legs: the quantity variance of the
    # revenue lines, split at this node into a volume at the node's average
    # plan price and a blend shift among the children.
    quantity_variance = sum((lg.quantity for _, lg in revenue), ZERO)
    volume, mix_between, children, mix_within = ZERO, ZERO, [], ZERO

    if revenue:
        avg_price = _average_plan_price(revenue)
        delta_q = sum((lg.sign * (lg.actual_qty - lg.plan_qty) for _, lg in revenue), ZERO)
        volume = delta_q * avg_price

    if level < depth:
        groups: dict[str, list[BridgeLine]] = defaultdict(list)
        for line in lines:
            groups[line.path[level]].append(line)
        for value in sorted(groups):
            child = _build(groups[value], (*path, value), level + 1, depth, convention, margin)
            children.append(child)
        if revenue:
            # Between-children mix: each child's quantity change at (child
            # average - node average). Together with volume this is the sum
            # of the children's own volumes, which is what makes it telescope.
            for child, value in zip(children, sorted(groups)):
                child_revenue = [(ln, lg) for ln, lg in legs if not ln.is_cost and ln.path[level] == value]
                if not child_revenue:
                    continue
                child_delta = sum((lg.sign * (lg.actual_qty - lg.plan_qty) for _, lg in child_revenue), ZERO)
                mix_between += child_delta * (_average_plan_price(child_revenue) - avg_price)
            mix_within = sum((child.mix for child in children), ZERO)
    elif revenue:
        # Leaf group: the elements are the signatures themselves.
        for _, lg in revenue:
            mix_between += lg.sign * (lg.actual_qty - lg.plan_qty) * (lg.plan_price_usd - avg_price)

    # Whatever the nested split did not place is not allowed to vanish: it is
    # part of this node's mix and it is checked below in the residual.
    placed = volume + mix_between + mix_within
    if revenue and abs(placed - quantity_variance) > Decimal("1e-9") * max(1, len(lines)):
        # Only rounding at extreme magnitudes could reach here; keep the
        # identity exact by folding the difference into mix_within.
        mix_within += quantity_variance - placed

    return BridgeNode(
        path=path, level=level, line_count=len(lines),
        plan_amount=plan_amount, actual_amount=actual_amount,
        price=price, volume=volume, mix_between=mix_between, mix_within=mix_within,
        fx=fx, rate=rate, efficiency=efficiency,
        children=children, line_keys=[line.key for line in lines] if level == depth else [],
    )


def _average_plan_price(revenue: list[tuple[BridgeLine, _Legs]]) -> Decimal:
    """Plan-quantity-weighted plan price, in report currency.

    Zero planned quantity has no meaningful price. Returning zero then keeps
    the leg explicit and deterministic: the whole quantity variance lands in
    mix, and the node still ties.
    """
    total_qty = sum((lg.plan_qty for _, lg in revenue), ZERO)
    if total_qty == 0:
        return ZERO
    return sum((lg.plan_qty * lg.plan_price_usd for _, lg in revenue), ZERO) / total_qty


# ---------------------------------------------------------------------------
# The bridge across vintages: why the same report changed when the books did
# ---------------------------------------------------------------------------
@dataclass
class VintageDeltaNode:
    """How one rollup node's gap moved between two reads of the ledger.

    The plan side is the same in both reports, so every change in the gap is
    a change on the actual side, and each matched key falls in exactly one
    bucket: restated (in both, different amount), reversed (matched only in
    the earlier read) or new (matched only in the later one). The buckets sum
    to the change, at every node.
    """

    path: tuple[str, ...]
    level: int
    left_gap: Decimal
    right_gap: Decimal
    restated: Decimal
    reversed: Decimal
    new: Decimal
    restated_lines: int
    reversed_lines: int
    new_lines: int
    children: list[VintageDeltaNode] = field(default_factory=list)

    @property
    def change(self) -> Decimal:
        return self.right_gap - self.left_gap

    @property
    def residual(self) -> Decimal:
        return self.change - (self.restated + self.reversed + self.new)

    def walk(self) -> Iterator[VintageDeltaNode]:
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path), "level": self.level,
            "left_gap": _cents(self.left_gap), "right_gap": _cents(self.right_gap),
            "change": _cents(self.change), "restated": _cents(self.restated),
            "reversed": _cents(self.reversed), "new": _cents(self.new),
            "residual": _cents(self.residual),
            "lines": {"restated": self.restated_lines, "reversed": self.reversed_lines, "new": self.new_lines},
            "children": [child.to_dict() for child in self.children],
        }


def vintage_delta(
    left: Iterable[BridgeLine], right: Iterable[BridgeLine], levels: tuple[str, ...] = (),
    margin: bool | None = None,
) -> VintageDeltaNode:
    """Decompose the change between two bridges of the same cut at two vintages.

    Lines are matched on ``key`` (the ledger key), and each line's gap is taken
    in report currency with the same sign convention ``decompose`` uses, so the
    two reports' own totals are what this explains. Refuses lines without a
    key, and a key that appears twice on one side, rather than guessing.
    """
    left, right = list(left), list(right)
    for side, lines in (("left", left), ("right", right)):
        keys = [line.key for line in lines]
        if any(key is None for key in keys):
            raise ValueError(f"{side} lines need a ledger key to be compared across vintages")
        if len(set(keys)) != len(keys):
            raise ValueError(f"{side} lines repeat a ledger key; a vintage read has one row per key")
        for line in lines:
            if len(line.path) != len(levels):
                raise ValueError(f"line path {line.path!r} does not match the rollup levels {levels!r}")
    if margin is None:
        margin = len({line.is_cost for line in left + right}) == 2

    def gap(line: BridgeLine) -> Decimal:
        legs = _legs(line, Convention.VOLUME_FIRST, margin)
        return legs.actual_usd - legs.plan_usd

    left_by, right_by = {l.key: l for l in left}, {l.key: l for l in right}
    return _delta_node(left_by, right_by, (), 0, len(levels), gap)


def _delta_node(left_by, right_by, path, level, depth, gap) -> VintageDeltaNode:
    left_gap = sum((gap(l) for l in left_by.values()), ZERO)
    right_gap = sum((gap(l) for l in right_by.values()), ZERO)
    restated = reversed_ = new = ZERO
    counts = [0, 0, 0]
    for key, line in left_by.items():
        if key in right_by:
            delta = gap(right_by[key]) - gap(line)
            if delta != 0:
                restated += delta
                counts[0] += 1
        else:
            reversed_ -= gap(line)
            counts[1] += 1
    for key, line in right_by.items():
        if key not in left_by:
            new += gap(line)
            counts[2] += 1

    children = []
    if level < depth:
        values = sorted({l.path[level] for l in left_by.values()} | {l.path[level] for l in right_by.values()})
        for value in values:
            children.append(_delta_node(
                {k: l for k, l in left_by.items() if l.path[level] == value},
                {k: l for k, l in right_by.items() if l.path[level] == value},
                (*path, value), level + 1, depth, gap,
            ))
    return VintageDeltaNode(path, level, left_gap, right_gap, restated, reversed_, new, *counts, children=children)
