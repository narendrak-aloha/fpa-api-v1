"""The approval view's bridge: baseline against the staged re-forecast.

Only the part that does not need a database: pairing rows into bridge lines,
the decomposition tying, and which of the planner's reasons belong to a
revision.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fpa_project.governance import Refused
from fpa_project.reforecast_impact import _covers, bridge_from_rows


def row(account, company, month, signature, bq, bp, sq, sp, fx="0.25", kind="Revenue"):
    ba = round(Decimal(bq) * Decimal(bp), 2)
    sa = round(Decimal(sq) * Decimal(sp), 2)
    return (account, company, month, signature, bq, bp, ba, sq, sp, sa, fx, kind)


# A utilisation cut: fewer billable hours at the same rate, on two companies
UTILISATION_CUT = [
    row("41000", "RTPL1", "2026-04-01", "a" * 16, "100", "80", "98.67", "80"),
    row("41000", "RTPL1", "2026-05-01", "b" * 16, "120", "90", "118.4", "90"),
    row("41000", "RTPL2", "2026-04-01", "c" * 16, "60", "70", "59.2", "70"),
]


def test_a_quantity_shock_lands_in_volume_and_mix_and_every_level_ties():
    report = bridge_from_rows(UTILISATION_CUT, plan_version="PV-2026-0001-R2", revision=2, scenario="base")

    root = report["root"]
    assert report["ties"] is True
    assert report["rollup"] == ["account", "company"]
    assert report["line_count"] == 3
    # Same price both sides: nothing is price, and FX is pinned so nothing is FX
    assert Decimal(root["price"]) == 0
    assert Decimal(root["fx"]) == 0
    assert Decimal(root["volume"]) < 0
    # The legs explain the whole change from baseline to re-forecast
    legs = sum(Decimal(root[k]) for k in ("price", "volume", "mix", "fx", "rate", "efficiency"))
    assert abs(Decimal(root["gap"]) - legs) <= Decimal(root["tolerance"])
    assert [child["path"] for child in root["children"]] == [["41000"]]
    assert [grand["path"] for grand in root["children"][0]["children"]] == [["41000", "RTPL1"], ["41000", "RTPL2"]]
    # Not a ledger read, and not persisted: no report id until compute_variance writes one
    assert report["vintage"]["vintage"] is None
    assert report["report_id"] is None


def test_no_paired_rows_means_nothing_to_show_rather_than_a_zero_bridge():
    assert bridge_from_rows([], plan_version="PV-2026-0001-R2", revision=2, scenario="base") is None


def test_a_line_without_a_plan_rate_is_refused():
    bad = [row("41000", "RTPL1", "2026-04-01", "a" * 16, "100", "80", "99", "80", fx=None)]
    with pytest.raises(Refused, match="FX"):
        bridge_from_rows(bad, plan_version="PV-2026-0001-R2", revision=2, scenario="base")


def test_a_reason_belongs_to_the_revision_that_carries_its_shocks():
    revision = [["heads", 100, 98], ["utilisation", 0.75, 0.74]]
    assert _covers(revision, {"shocks": [["utilisation", 0.75, 0.74]]})
    assert not _covers(revision, {"shocks": [["utilisation", 0.75, 0.70]]})
    assert not _covers(revision, {"shocks": []})
