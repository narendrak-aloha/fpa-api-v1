"""Persisted Agno confirmation, independent of Temporal plan approval."""
from __future__ import annotations

import copy
import json
from sqlalchemy import text
from agno.run.team import TeamRunOutput
from fpa_project.governance import Refused, engine, record, set_actor
from fpa_project.dsl.formula import parse_formula, validate_formula
from .models import UserScope

TABLE = "fpa_governance.agent_proposal"


def save_pause(output, scope: UserScope, provider: str, schema) -> str:
    drafts = []
    for requirement in output.requirements or []:
        execution = requirement.tool_execution
        if not requirement.needs_confirmation or execution.tool_name != "propose_driver":
            raise Refused("unsupported agent pause")
        args = execution.tool_args
        validate_formula(parse_formula(args["expr_dsl"]), schema)
        drafts.append({"name": args.get("name", "draft_driver"), "expr_dsl": args["expr_dsl"], "status": "DRAFT"})
    if not drafts:
        raise Refused("paused run has no draft driver confirmation")
    with engine().begin() as conn:
        proposal_id = conn.execute(text(
            f"INSERT INTO {TABLE} (run_id, requested_by, provider, scope, drafts, paused_run) "
            "VALUES (:run, :user, :provider, CAST(:scope AS jsonb), CAST(:drafts AS jsonb), CAST(:snapshot AS jsonb)) "
            "ON CONFLICT (run_id) DO UPDATE SET run_id = EXCLUDED.run_id RETURNING proposal_id"
        ), {"run": output.run_id, "user": scope.user_id, "provider": provider,
            "scope": scope.model_dump_json(), "drafts": json.dumps(drafts),
            "snapshot": json.dumps(output.to_dict(), default=str)}).scalar()
        record(conn, scope.user_id, "agent_proposal", str(proposal_id), "DRAFT_PROPOSED", {"run_id": output.run_id})
    return str(proposal_id)


def load(proposal_id):
    with engine().connect() as conn:
        row = conn.execute(text(f"SELECT * FROM {TABLE} WHERE proposal_id = CAST(:id AS uuid)"), {"id": proposal_id}).mappings().first()
    if row is None:
        raise Refused("no agent proposal")
    return dict(row)


def decide(proposal_id, actor, approved):
    with engine().begin() as conn:
        set_actor(conn, actor)
        row = conn.execute(text(f"SELECT * FROM {TABLE} WHERE proposal_id = CAST(:id AS uuid) FOR UPDATE"), {"id": proposal_id}).mappings().first()
        if row is None:
            raise Refused("no agent proposal")
        if actor == row["requested_by"]:
            raise Refused("self-approval is not allowed")
        state = "APPROVED" if approved else "REJECTED"
        if row["state"] != "PENDING":
            if row["state"] != state or row["decided_by"] != actor:
                raise Refused("proposal already decided")
            return
        conn.execute(text(f"UPDATE {TABLE} SET state=:state, decided_by=:actor, decided_at=now() WHERE proposal_id=CAST(:id AS uuid)"),
                     {"id": proposal_id, "state": state, "actor": actor})
        record(conn, actor, "agent_proposal", proposal_id, state, {"run_id": row["run_id"]})


def resume_snapshot(row, team):
    if row["state"] == "PENDING":
        raise Refused("a human decision is required before resuming")
    if row["resumed_run"] is not None:
        return row["resumed_run"]
    output = TeamRunOutput.from_dict(copy.deepcopy(row["paused_run"]))
    for requirement in output.requirements:
        if row["state"] == "APPROVED":
            requirement.confirm()
        else:
            requirement.reject("Rejected by human reviewer")
        # Restore the nested member pause as well as the leader pause.
        for member in output.member_responses:
            if member.run_id == requirement.member_run_id:
                requirement._member_run_response = member
    result = team.continue_run(output, user_id=row["requested_by"], dependencies={"scope": row["scope"]})
    if result.is_paused:
        raise Refused("continuation requested another approval; start a new proposal")
    snapshot = result.to_dict()
    with engine().begin() as conn:
        conn.execute(text(f"UPDATE {TABLE} SET resumed_run=CAST(:result AS jsonb) WHERE proposal_id=CAST(:id AS uuid) AND resumed_run IS NULL"),
                     {"id": str(row["proposal_id"]), "result": json.dumps(snapshot, default=str)})
    return snapshot
