"""The vintage bridge service, against a fake executor: no stack needed.

The executor answers the vintage lookup with a close and the compiled bridge
query with matched lines, choosing the lines by which close the query asked
for. That is enough to check the service does what the plan said: compile
each side at its own close with the caller's scope, refuse what it cannot
answer, and return a delta that ties.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fpa_project.bridge_service import BridgeError, vintage_bridge
from fpa_project.dsl.compiler import SecurityContext

DSL = ("SELECT delivery_cost BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 "
       "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE")
JULY, AUGUST = "2026-07-05T18:00:00", "2026-08-12T09:30:00"


def row(key, practice, actual_qty, account_type="COGS"):
    return {
        "a.company": "RTPL1", "a.period_month": dt.date(2026, 4, 1), "a.account": "51100",
        "a.dim_signature_hash": key, "a.practice": practice, "account_type": account_type,
        "plan_quantity": 10, "plan_unit_price": 50, "plan_amount": 500,
        "actual_quantity": actual_qty, "actual_unit_price": 50, "actual_amount": actual_qty * 50,
        "plan_fx": 0.25, "actual_fx": 0.25,
    }


LINES = {
    JULY: [row("aaaaaaaaaaaaaaaa", "Cloud", 10), row("bbbbbbbbbbbbbbbb", "Data", 10)],
    # August: a restated accrual on a, b reversed, c new.
    AUGUST: [row("aaaaaaaaaaaaaaaa", "Cloud", 14), row("cccccccccccccccc", "Data", 4)],
}


def make_executor(closes=True, seen=None):
    def execute(sql, params):
        if "dim_ledger_vintage" in sql and "LIMIT 1" in sql and "fact_gl_actual" not in sql:
            instant = params.get("as_of")
            if not closes:
                return []
            return [{"vintage": 1 if instant == JULY else 2, "closed_at": instant, "note": "close"}]
        instant = next(v for v in params.values() if v in (JULY, AUGUST))
        if seen is not None:
            seen.append((instant, sql))
        return LINES[instant]
    return execute


def test_each_side_is_read_at_its_own_close_and_the_change_ties():
    seen = []
    result = vintage_bridge(DSL, JULY, AUGUST, make_executor(seen=seen), SecurityContext(frozenset({"RTPL1"})))
    assert [instant for instant, _ in seen] == [JULY, AUGUST]
    assert all("company IN" in sql for _, sql in seen), "the caller's scope reaches both reads"
    assert result["left"]["vintage"] == "1" and result["right"]["vintage"] == "2"
    root = result["root"]
    assert result["ties"] and str(root["residual"]) == "0.00"
    # Cost rows, bridged alone, keep their own sign: cost up is a positive gap.
    assert str(root["restated"]) == "50.00"      # a: +4 units x 50 x 0.25
    assert str(root["reversed"]) == "0.00"       # b had no gap to remove
    assert str(root["new"]) == "-75.00"          # c: 4 against a plan of 10
    assert str(root["change"]) == "-25.00"
    assert [c["path"] for c in root["children"]] == [["Cloud"], ["Data"]]


def test_a_dsl_that_names_its_own_close_is_refused():
    with pytest.raises(BridgeError, match="leave AS OF out"):
        vintage_bridge(DSL.replace("FOR PERIOD 2026-Q2 ", f"FOR PERIOD 2026-Q2 AS OF '{JULY}' "),
                       JULY, AUGUST, make_executor(), SecurityContext())


def test_a_close_that_did_not_exist_is_an_error_not_an_empty_answer():
    with pytest.raises(BridgeError, match="did not exist yet"):
        vintage_bridge(DSL, JULY, AUGUST, make_executor(closes=False), SecurityContext())


def test_a_non_bridge_query_is_refused():
    with pytest.raises(BridgeError, match="needs COMPARE PLAN"):
        vintage_bridge("SELECT delivery_cost FOR PERIOD 2026-Q2", JULY, AUGUST, make_executor(), SecurityContext())
