import asyncio
import json
from types import SimpleNamespace

from xiangkou.llm import Completion
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


def test_missing_booking_does_not_access_dianping_and_survives_restart(tmp_path):
    async def run():
        class NoSearch:
            def __getattr__(self, name):
                async def fail(*args, **kwargs):
                    raise AssertionError('Incomplete booking must not search')
                return fail
        path=str(tmp_path/'state.sqlite3')
        workflow=DiscoveryWorkflow(NoSearch(),Planner(),'http://web',path)
        result=await workflow.run(ChatRequest(message='想定个ktv'),owner_user_id=7)
        assert result.status=='needs_clarification' and len(result.missing_fields)==5
        restarted=DiscoveryWorkflow(NoSearch(),Planner(),'http://web',path)
        await restarted.sessions.require_owned(result.session_id,7)
        state=await restarted.sessions.load(result.session_id,7)
        assert state.plan.goal=='booking' and state.plan.category=='KTV'
    asyncio.run(run())


def test_model_booking_extracts_current_task_only_and_requires_budget_basis():
    async def run():
        class Provider:
            enabled=True
            config=SimpleNamespace(timeout=1,provider='test',model='test')
            async def complete(self,*args,**kwargs):
                return Completion(text=json.dumps({'goal':'booking','category':'KTV','followup':True}))
        planner=Planner(provider=Provider())
        context={'recent_user_messages':['明天4个人，总预算500，地点不限，19:00找火锅','想定个ktv']}
        plan,mode=await planner.plan('预算500',context=context)
        assert mode=='model' and plan.max_price is None and plan.total_budget is None
        assert plan.party_size is None and plan.visit_day is None
        context={'plan':rule_plan('想定个ktv').model_dump(),'recent_user_messages':['想定个ktv']}
        plan,mode=await planner.plan('明天4个人，19:00，总预算400，地点不限',context=context)
        assert plan.goal=='booking' and plan.party_size==4 and plan.total_budget==400
        assert plan.visit_day=='明天' and plan.visit_time=='19:00' and plan.area_unrestricted
    asyncio.run(run())


def test_search_only_does_not_require_booking_fields():
    assert rule_plan('找个KTV').goal=='search'
