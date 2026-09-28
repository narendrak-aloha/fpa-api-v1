"""Ask history (migration 024): each person's own questions, and nobody else's.

Rows written here are deleted afterwards; the table has no guard against it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from db.config import database_url

pytestmark = pytest.mark.integration

try:
    _engine = create_engine(database_url(), connect_args={"connect_timeout": 2})
    with _engine.connect() as probe:
        probe.execute(text("SELECT 1 FROM fpa_governance.ask_history LIMIT 1"))
except Exception:  # noqa: BLE001
    pytest.skip("Postgres with migration 024 required", allow_module_level=True)

from fpa_project import ask_history  # noqa: E402
from fpa_project.identities import ANALYST_PL, PLANNER  # noqa: E402

POLAND = frozenset({"RTPL1", "RTPL2", "RTPL3"})
MARK = "ask-history-test"


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with _engine.begin() as conn:
        conn.execute(text("DELETE FROM fpa_governance.ask_history WHERE question LIKE :m"), {"m": f"{MARK}%"})


def answer(rows):
    return {"agent_response": {"execution_status": "SUCCESS", "cited_data_rows": rows}, "mode": "direct_dsl"}


def test_a_question_is_kept_and_read_back_by_its_owner_only():
    ask_id = ask_history.record(ANALYST_PL, f"{MARK} poland revenue", "claude-code", "direct_dsl", "SUCCESS",
                                POLAND, answer([{"company": "RTPL1", "services_revenue": 1.0}]))
    assert ask_id
    listed = ask_history.list_for(ANALYST_PL, POLAND, MARK)
    assert [row["ask_id"] for row in listed] == [ask_id] and listed[0]["withheld"] is False
    loaded = ask_history.load_for(ANALYST_PL, ask_id, POLAND)
    assert loaded["response"]["agent_response"]["cited_data_rows"][0]["company"] == "RTPL1"
    # Someone else: not listed, not loadable, even with a wider scope
    assert ask_history.load_for(PLANNER, ask_id, POLAND | {"RTUS1"}) is None
    assert all(row["ask_id"] != ask_id for row in ask_history.list_for(PLANNER, POLAND, MARK))


def test_figures_are_withheld_once_the_scope_they_were_computed_under_is_gone():
    ask_id = ask_history.record(ANALYST_PL, f"{MARK} wider question", "claude-code", "direct_dsl", "SUCCESS",
                                POLAND | {"RTUS1"}, answer([{"company": "RTUS1", "services_revenue": 2.0}]))
    listed = ask_history.list_for(ANALYST_PL, POLAND, MARK)
    assert listed[0]["withheld"] is True
    loaded = ask_history.load_for(ANALYST_PL, ask_id, POLAND)
    assert loaded["withheld"] is True and loaded["response"] is None
    assert loaded["question"] == f"{MARK} wider question"
    # With that company back in scope, the answer shows again
    assert ask_history.load_for(ANALYST_PL, ask_id, POLAND | {"RTUS1"})["response"] is not None


def test_newest_first_and_searchable():
    first = ask_history.record(ANALYST_PL, f"{MARK} alpha margin", "claude-code", "direct_dsl", "SUCCESS", POLAND, answer([]))
    second = ask_history.record(ANALYST_PL, f"{MARK} beta revenue", "claude-code", "direct_dsl", "SUCCESS", POLAND, answer([]))
    assert [row["ask_id"] for row in ask_history.list_for(ANALYST_PL, POLAND, MARK)] == [second, first]
    assert [row["ask_id"] for row in ask_history.list_for(ANALYST_PL, POLAND, f"{MARK} alpha")] == [first]
