"""The bridge is an algebraic identity, and these tests hold it to that.

The property test at the bottom generates plan/actual pairs at random and
asserts the identity at every node of the rollup; the named tests above it
pin the conventions the module declares, so a change to the convention is a
change to a test and not a quiet drift.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from fpa_project.dsl.bridge import BridgeLine, Convention, decompose

D = Decimal
# The arithmetic runs at 34 significant digits and the averages are
# repeating decimals, so identities are asserted to well below a cent rather
# than to the last digit.
EXACT = D("1e-9")


def close(a, b):
    return abs(D(a) - D(b)) < EXACT


def line(path, pq, aq, pp, ap, pfx="1", afx="1", kind="Revenue", key=None):
    return BridgeLine(
        path=tuple(path), plan_quantity=D(pq), actual_quantity=D(aq),
        plan_unit_price=D(pp), actual_unit_price=D(ap), plan_fx=D(pfx), actual_fx=D(afx),
        account_type=kind, key=key,
    )


# ---------------------------------------------------------------------------
# The identity
# ---------------------------------------------------------------------------
def test_legs_sum_to_the_gap_and_volume_plus_mix_is_the_quantity_variance():
    result = decompose(
        [line(("PL",), 10, 12, 100, 110, "1.0", "1.1"), line(("PL",), 20, 18, 200, 190, "1.0", "1.1")],
        levels=("geo_country",),
    )
    root = result.root
    assert abs(root.residual) < D("0.000001")
    # Quantity variance at plan price: (12-10)*100 + (18-20)*200 = -200
    assert root.volume + root.mix == D("-200")
    assert root.gap == root.price + root.volume + root.mix + root.fx


def test_fx_is_kept_separate_from_the_operational_legs():
    result = decompose([line(("PL",), 10, 10, 100, 100, "1.0", "1.2")], levels=("geo_country",))
    root = result.root
    assert root.price == 0 and root.volume == 0 and root.mix == 0
    assert root.fx == D("200")          # 1000 local x (1.2 - 1.0)
    assert root.gap == root.fx


def test_operational_legs_use_the_assumed_rate_not_the_real_one():
    # Price moved and the rate moved. The price leg must be at the plan rate.
    result = decompose([line((), 10, 10, 100, 120, "2", "3")])
    root = result.root
    assert root.price == D("400")       # (120-100) x 10 x plan_fx 2
    assert root.fx == D("1200")         # actual local 1200 x (3 - 2)
    assert root.gap == D("3600") - D("2000") == root.price + root.fx


# ---------------------------------------------------------------------------
# The convention, demonstrated
# ---------------------------------------------------------------------------
def test_the_interaction_term_lands_in_price_by_default_and_both_conventions_tie():
    lines = [line((), 10, 12, 100, 110)]   # both price and quantity moved
    volume_first = decompose(lines, convention=Convention.VOLUME_FIRST).root
    price_first = decompose(lines, convention=Convention.PRICE_FIRST).root
    # Interaction = (12-10) x (110-100) = 20. It is inside price under the
    # default (price at actual quantity) and inside volume under the other.
    assert volume_first.price == D("120") and volume_first.volume == D("200")
    assert price_first.price == D("100") and price_first.volume == D("220")
    # Different splits, same gap, both tie.
    assert volume_first.gap == price_first.gap == D("320")
    assert volume_first.residual == 0 and price_first.residual == 0
    assert decompose(lines).convention is Convention.VOLUME_FIRST


# ---------------------------------------------------------------------------
# Nested mix
# ---------------------------------------------------------------------------
NESTED = [
    # practice, grade: a pyramid shift towards Analysts inside Cloud, and a
    # shift of hours from Data (dear) to Cloud (cheap) between practices.
    line(("Cloud", "Analyst"), 100, 160, 80, 80),
    line(("Cloud", "Partner"), 20, 10, 400, 400),
    line(("Data", "Analyst"), 100, 80, 120, 120),
    line(("Data", "Partner"), 40, 30, 500, 500),
]


def test_practice_mix_and_grade_mix_are_separate_legs_that_telescope():
    result = decompose(NESTED, levels=("practice", "grade"))
    root = result.root
    by_level = result.mix_by_level()
    assert set(by_level) == {"practice", "grade", "signature"}
    assert by_level["practice"] != 0
    assert by_level["grade"] != 0
    # Within a (practice, grade) cell there is one signature, so no mix there.
    assert by_level["signature"] == 0
    assert close(sum(by_level.values()), root.mix)
    assert close(root.volume + root.mix, sum(
        (ln.actual_quantity - ln.plan_quantity) * ln.plan_unit_price for ln in NESTED
    ))


def test_every_node_of_the_rollup_ties():
    result = decompose(NESTED, levels=("practice", "grade"))
    nodes = list(result.walk())
    assert [node.path for node in nodes] == [
        (), ("Cloud",), ("Cloud", "Analyst"), ("Cloud", "Partner"),
        ("Data",), ("Data", "Analyst"), ("Data", "Partner"),
    ]
    for node in nodes:
        assert abs(node.residual) < node.tol, f"breaks at {node.path}"
        assert close(node.volume + node.mix, node.quantity_variance)


def test_a_child_node_is_its_own_bridge():
    """The Cloud node decomposed alone equals the Cloud node inside the tree."""
    whole = decompose(NESTED, levels=("practice", "grade")).root.children[0]
    alone = decompose([ln for ln in NESTED if ln.path[0] == "Cloud"], levels=("practice", "grade")).root.children[0]
    assert whole.path == alone.path == ("Cloud",)
    for leg in ("price", "volume", "mix", "fx", "gap"):
        assert getattr(whole, leg) == getattr(alone, leg)


def test_between_mix_is_the_blend_shift_at_average_prices():
    # Two practices, no price change, no grade level: the whole mix is between.
    lines = [line(("Cheap",), 100, 150, 10, 10), line(("Dear",), 100, 50, 30, 30)]
    root = decompose(lines, levels=("practice",)).root
    avg = D(100 * 10 + 100 * 30) / D(200)           # 20
    assert root.volume == (150 + 50 - 200) * avg     # 0: total hours unchanged
    assert root.mix_between == (50 * (10 - avg)) + (-50 * (30 - avg))   # -1000
    assert root.mix_within == 0
    assert root.gap == D("-1000")


# ---------------------------------------------------------------------------
# The cost side and margin sign
# ---------------------------------------------------------------------------
def test_cost_lines_get_rate_and_efficiency():
    root = decompose([line((), 10, 12, 50, 55, kind="COGS")]).root
    assert root.price == 0 and root.volume == 0 and root.mix == 0
    assert root.rate == D("60")          # (55-50) x 12
    assert root.efficiency == D("100")   # (12-10) x 50
    assert root.gap == D("160") == root.rate + root.efficiency


def test_a_margin_bridge_carries_cost_with_negative_sign():
    lines = [line((), 10, 10, 100, 110), line((), 10, 10, 50, 60, kind="COGS")]
    root = decompose(lines).root
    # Revenue up 100, cost up 100: margin unchanged.
    assert root.price == D("100") and root.rate == D("-100")
    assert root.gap == 0 and root.residual == 0
    assert root.plan_amount == D("500") and root.actual_amount == D("500")


def test_a_pure_cost_bridge_keeps_its_own_sign():
    root = decompose([line((), 10, 10, 50, 60, kind="COGS")], margin=False).root
    assert root.gap == D("100") and root.rate == D("100")


# ---------------------------------------------------------------------------
# Tolerance and the stored amounts
# ---------------------------------------------------------------------------
def test_tolerance_is_a_fixed_floor_scaled_only_by_line_count():
    one = decompose([line((), 1, 1, 1, 1)]).root
    many = decompose([line((), 1, 1, 1, 1, key=i) for i in range(500)]).root
    assert one.tol == D("1.00")
    assert many.tol == D("5.00")


def test_stored_amounts_put_the_cubes_cent_rounding_into_the_residual():
    # q x p = 33.333..., the cube stored 33.33: the residual is that cent, no more.
    ln = BridgeLine(
        path=(), plan_quantity=D("3"), actual_quantity=D("3"), plan_unit_price=D("11.111"),
        actual_unit_price=D("11.111"), plan_fx=D(1), actual_fx=D(1),
        plan_amount=D("33.33"), actual_amount=D("33.33"),
    )
    root = decompose([ln]).root
    assert root.plan_amount == D("33.33") and root.gap == 0
    assert root.residual == 0


def test_zero_plan_quantity_is_deterministic_not_a_crash():
    root = decompose([line((), 0, 5, 100, 100)]).root
    assert root.volume == 0
    assert root.mix == D("500")
    assert root.residual == 0


def test_empty_input_is_an_error_not_an_empty_report():
    with pytest.raises(ValueError, match="nothing to bridge"):
        decompose([])


def test_a_line_that_does_not_fit_the_rollup_is_refused():
    with pytest.raises(ValueError, match="does not match"):
        decompose([line(("PL",), 1, 1, 1, 1)], levels=("practice", "grade"))


# ---------------------------------------------------------------------------
# The property: generated pairs, every node, both conventions
# ---------------------------------------------------------------------------
def generate(seed: int, n: int) -> list[BridgeLine]:
    rng = random.Random(seed)
    practices = ["Cloud", "Data", "Cyber"]
    grades = ["Analyst", "Consultant", "Manager", "Partner"]
    currencies = {"USD": ("1", "1"), "PLN": ("0.2545", "0.2414"), "GBP": ("1.27", "1.31"), "INR": ("0.012", "0.0117")}
    lines = []
    for i in range(n):
        ccy = rng.choice(list(currencies))
        pfx, afx = currencies[ccy]
        kind = "Revenue" if rng.random() < 0.6 else rng.choice(["COGS", "OpEx"])
        pq = D(str(round(rng.uniform(0, 300), 4))) if rng.random() > 0.05 else D(0)
        aq = D(str(round(rng.uniform(0, 300), 4))) if rng.random() > 0.05 else D(0)
        pp = D(str(round(rng.uniform(20, 600), 6)))
        ap = D(str(round(pp * D(str(rng.uniform(0.7, 1.3))), 6)))
        lines.append(BridgeLine(
            path=(rng.choice(practices), rng.choice(grades)),
            plan_quantity=pq, actual_quantity=aq, plan_unit_price=pp, actual_unit_price=ap,
            plan_fx=D(pfx), actual_fx=D(afx), account_type=kind, key=i,
            plan_amount=(pq * pp).quantize(D("0.01")), actual_amount=(aq * ap).quantize(D("0.01")),
        ))
    return lines


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("convention", list(Convention))
def test_the_identity_holds_for_generated_pairs_at_every_level(seed, convention):
    lines = generate(seed, 60 + seed * 7)
    result = decompose(lines, levels=("practice", "grade"), convention=convention)
    for node in result.walk():
        assert abs(node.residual) < node.tol, f"breaks at {node.path}: residual={node.residual}"
        assert close(node.volume + node.mix, node.quantity_variance)
    # Volume + mix is the whole quantity variance of the revenue lines, at
    # plan price and plan rate, with margin sign.
    expected = sum(
        (ln.actual_quantity - ln.plan_quantity)
        * (ln.plan_unit_price if convention is Convention.VOLUME_FIRST else ln.actual_unit_price)
        * ln.plan_fx
        for ln in lines if not ln.is_cost
    )
    assert abs((result.volume + result.mix) - expected) < result.tol
    # Nothing is suspiciously zero on a set this size.
    root = result.root
    assert all(leg != 0 for leg in (root.price, root.volume, root.mix, root.fx, root.rate, root.efficiency))
    assert close(sum(result.mix_by_level().values()), root.mix)


# ---------------------------------------------------------------------------
# The bridge across vintages
# ---------------------------------------------------------------------------
from fpa_project.dsl.bridge import vintage_delta  # noqa: E402


def keyed(key, path, pq, aq, pp, ap, kind="Revenue"):
    return BridgeLine(path=tuple(path), plan_quantity=D(pq), actual_quantity=D(aq), plan_unit_price=D(pp),
                      actual_unit_price=D(ap), plan_fx=D(1), actual_fx=D(1), account_type=kind, key=key)


def test_each_cause_alone_explains_the_whole_change():
    base = [keyed("a", (), 10, 10, 100, 100), keyed("b", (), 5, 5, 100, 100)]
    restated = vintage_delta(base, [keyed("a", (), 10, 12, 100, 100), base[1]])
    assert restated.change == D("200") and restated.restated == D("200") and restated.restated_lines == 1
    reversed_ = vintage_delta(base, [base[0]])
    # b's gap was zero, so reversing it changes nothing but is still counted.
    assert reversed_.change == 0 and reversed_.reversed_lines == 1
    new = vintage_delta(base, base + [keyed("c", (), 0, 3, 100, 100)])
    assert new.change == D("300") and new.new == D("300") and new.new_lines == 1
    for node in (restated, reversed_, new):
        assert node.residual == 0


def test_mixed_causes_tie_at_every_level_of_a_nested_rollup():
    left = [keyed("a", ("PL", "Cloud"), 10, 11, 100, 100), keyed("b", ("PL", "Data"), 5, 7, 80, 80),
            keyed("c", ("DE", "Cloud"), 4, 4, 50, 60)]
    right = [keyed("a", ("PL", "Cloud"), 10, 9, 100, 100), keyed("d", ("DE", "Cloud"), 0, 2, 50, 50),
             keyed("c", ("DE", "Cloud"), 4, 4, 50, 60)]
    root = vintage_delta(left, right, levels=("country", "practice"))
    assert [n.path for n in root.walk()] == [(), ("DE",), ("DE", "Cloud"), ("PL",), ("PL", "Cloud"), ("PL", "Data")]
    for node in root.walk():
        assert node.residual == 0, node.path
    assert root.restated == D("-200")            # a: 11 -> 9 hours at 100
    assert root.reversed == D("-160")            # b: +2 hours at 80, gone
    assert root.new == D("100")                  # d: 2 hours at 50, arrived
    assert root.change == root.right_gap - root.left_gap == D("-260")


def test_margin_sign_carries_through():
    left = [keyed("r", (), 10, 10, 100, 100), keyed("c", (), 10, 10, 50, 50, kind="COGS")]
    right = [keyed("r", (), 10, 10, 100, 100), keyed("c", (), 10, 12, 50, 50, kind="COGS")]
    root = vintage_delta(left, right)
    # Cost up 100 is margin down 100.
    assert root.change == D("-100") and root.restated == D("-100") and root.residual == 0


def test_vintage_delta_refuses_what_it_cannot_match():
    with pytest.raises(ValueError, match="ledger key"):
        vintage_delta([keyed(None, (), 1, 1, 1, 1)], [])
    with pytest.raises(ValueError, match="repeat a ledger key"):
        vintage_delta([keyed("a", (), 1, 1, 1, 1), keyed("a", (), 1, 1, 1, 1)], [])


@pytest.mark.parametrize("seed", range(15))
def test_the_vintage_identity_holds_for_generated_restatements(seed):
    rng = random.Random(seed)
    left = generate(seed, 80)
    right = []
    for line in left:
        roll = rng.random()
        if roll < 0.15:
            continue                                   # reversed
        if roll < 0.40:                                # restated
            line = BridgeLine(**{**line.__dict__, "actual_quantity": line.actual_quantity * D("1.1"),
                                 "actual_amount": None})
        right.append(line)
    for i in range(rng.randint(0, 6)):                 # new keys
        extra = generate(1000 + seed * 10 + i, 1)[0]
        right.append(BridgeLine(**{**extra.__dict__, "key": f"new-{i}"}))
    root = vintage_delta(left, right, levels=("practice", "grade"))
    for node in root.walk():
        assert abs(node.residual) < D("1e-9"), node.path
    # And the change is exactly the difference of the two bridges' own gaps.
    assert abs(root.change - (decompose(right, levels=("practice", "grade")).root.gap
                              - decompose(left, levels=("practice", "grade")).root.gap)) < D("1e-9")
