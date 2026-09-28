"""Real Agno pause -> durable DB decision -> reconstructed continuation."""
import uuid
import pytest
from sqlalchemy import create_engine, text
from agno.models.base import Model
from agno.models.response import ModelResponse
from db.config import database_url
from fpa_project.agent_team.models import UserScope
from fpa_project.agent_team.tools import FPATools
from fpa_project.agent_team.team import build_agno_team
from fpa_project.agent_team import proposals
from fpa_project.governance import Refused
from fpa_project.identities import CFO, CONTROLLER, PLANNER

pytestmark = pytest.mark.integration
try:
    create_engine(database_url(), connect_args={"connect_timeout":2}).connect().close()
except Exception:
    pytest.skip("Postgres required", allow_module_level=True)


class ProposalModel(Model):
    def invoke(self, messages, **kwargs):
        if any(m.role == "tool" for m in messages):
            return ModelResponse(role="assistant", content='{"dsl":"","explanation":"Draft reviewed","proposed_driver":true}')
        return ModelResponse(role="assistant", tool_calls=[{"id":"proposal-call","type":"function", "function":{
            "name":"propose_driver", "arguments":'{"name":"reviewed_utilisation","expr_dsl":"0.74"}'}}])
    async def ainvoke(self,*args,**kwargs): return self.invoke(*args,**kwargs)
    def invoke_stream(self,*args,**kwargs): yield self.invoke(*args,**kwargs)
    async def ainvoke_stream(self,*args,**kwargs): yield self.invoke(*args,**kwargs)
    def _parse_provider_response(self,response,**kwargs): return response
    def _parse_provider_response_delta(self,response): return response


def test_real_agno_confirmation_survives_reconstruction_and_requires_second_human():
    scope=UserScope(user_id=PLANNER,allowed_companies=frozenset({"RTPL1"}))
    tools=FPATools(scope)
    team=build_agno_team(ProposalModel(id="scripted",name="scripted",provider="test"),tools)
    output=team.run("Propose a utilisation assumption", user_id=scope.user_id,dependencies={"scope":scope.model_dump()})
    assert output.is_paused
    assert tools.draft_drivers == []  # Agno has not executed the tool yet.
    proposal_id=proposals.save_pause(output,scope,"test",tools.schema)
    assert proposals.load(proposal_id)["drafts"][0]["status"] == "DRAFT"
    with pytest.raises(Refused,match="self-approval"):
        proposals.decide(proposal_id,PLANNER,True)
    proposals.decide(proposal_id,CONTROLLER,True)
    replacement=build_agno_team(ProposalModel(id="scripted",name="scripted",provider="test"),FPATools(scope))
    resumed=proposals.resume_snapshot(proposals.load(proposal_id),replacement)
    assert resumed
    assert proposals.load(proposal_id)["resumed_run"]
    assert proposals.resume_snapshot(proposals.load(proposal_id),replacement) == proposals.load(proposal_id)["resumed_run"]
    with pytest.raises(Refused,match="already decided"):
        proposals.decide(proposal_id,CFO,False)
