import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from xiangkou.dianping import DianpingClient
from xiangkou.llm import Completion
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow
from test_itinerary import fake_dianping


@pytest.mark.parametrize('model_enabled',[False,True])
def test_unexecuted_ktv_request_overrides_saved_hotpot_and_keeps_new_budget(tmp_path,model_enabled):
    async def run():
        class Provider:
            enabled=model_enabled
            config=SimpleNamespace(timeout=1,provider='test',model='test')
            async def complete(self,system,messages,**kwargs):
                if kwargs.get('tools'): return Completion(text='done')
                # Even an old-category model output must be validated against latest explicit intent.
                return Completion(text=json.dumps({'category':'美食','keyword':'火锅','followup':True}))
        client=DianpingClient('http://test',transport=httpx.MockTransport(fake_dianping))
        try:
            workflow=DiscoveryWorkflow(client,Planner(provider=Provider()),'http://test',str(tmp_path/'pending.sqlite3'))
            sid=(await workflow.sessions.load(None,7)).id
            first=await workflow.conversations.begin(sid,7,'查询火锅人均150以内')
            result=await workflow.run(ChatRequest(message='查询火锅人均150以内',session_id=sid),7)
            workflow.conversations.finish(first,result.answer,run_id=result.run_id)
            for text in ['你好，帮我查询下有哪些ktv推荐','一个，100以内']:
                turn=await workflow.conversations.begin(sid,7,text)
                workflow.conversations.finish(turn,'好的，还有什么要求？')
            await workflow.conversations.begin(sid,7,'直接帮我查')
            result=await workflow.run(ChatRequest(message='直接帮我查',session_id=sid),7)
            assert result.plan.category=='KTV' and result.plan.keyword is None
            assert result.plan.max_price==100
            assert result.shops and all('KTV' in shop.name and shop.avg_price<=100 for shop in result.shops)
        finally: await client.close()
    asyncio.run(run())
