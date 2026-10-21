import asyncio
from types import SimpleNamespace

import httpx
import pytest

from xiangkou.agent_loop import AgentLoop, LoopPolicy
from xiangkou.dianping import DianpingError
from xiangkou.errors import classify_error
from xiangkou.harness import AgentHarness
from xiangkou.llm import Completion, LLMConfig, LLMProvider, ToolCall
from xiangkou.protocol import AgentMessage, ToolSpec
from xiangkou.run_context import RunContext


class Provider:
    config = SimpleNamespace(timeout=1, provider='test', model='test')
    def __init__(self, calls):
        self.calls, self.turns = calls, 0
    async def complete(self, *args, **kwargs):
        self.turns += 1
        return Completion(tool_calls=self.calls) if self.turns == 1 else Completion(text='done')


def test_transient_read_retry_recovers_and_counts_every_attempt():
    async def run():
        attempts = 0
        async def read():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise DianpingError('private upstream message')
            return {'ok': True}
        spec = ToolSpec('read', read)
        harness = AgentHarness({'read': spec})
        result = await AgentLoop(Provider([ToolCall('1','read',{})]),harness).run(
            'test',[AgentMessage('user','test')],[spec],LoopPolicy())
        assert attempts==2 and harness.context.calls==2 and not result.failed
        assert result.stop_reason=='model_finished'
        assert result.errors[0].code=='upstream_unavailable'
        assert any(item['event']=='tool_retry' for item in harness.events)
        assert 'private upstream message' not in str(harness.events)
    asyncio.run(run())


def test_retry_cannot_bypass_shared_tool_budget():
    async def run():
        attempts = 0
        async def read():
            nonlocal attempts
            attempts += 1
            raise DianpingError('unavailable')
        spec = ToolSpec('read',read)
        harness = AgentHarness({'read':spec},context=RunContext(max_calls=1))
        result = await AgentLoop(Provider([ToolCall('1','read',{})]),harness).run(
            'test',[AgentMessage('user','test')],[spec],LoopPolicy())
        assert attempts==1 and result.stop_reason=='tool_budget' and result.failed
    asyncio.run(run())


def test_write_is_never_automatically_retried():
    async def run():
        attempts = 0
        async def write():
            nonlocal attempts
            attempts += 1
            raise DianpingError('uncertain')
        spec = ToolSpec('write',write,read_only=False)
        harness = AgentHarness({'write':spec})
        result = await AgentLoop(Provider([ToolCall('1','write',{})]),harness).run(
            'test',[AgentMessage('user','test')],[spec],LoopPolicy(allow_writes=True))
        assert attempts==1 and result.failed
        assert not any(item['event']=='tool_retry' for item in harness.events)
    asyncio.run(run())


def test_observation_bug_is_internal_and_stops_remaining_tools():
    async def run():
        calls = []
        async def read():
            calls.append('read')
            return {'ok':True}
        async def other():
            calls.append('other')
        def broken_project(result):
            raise TypeError('sensitive implementation detail')
        first, second = ToolSpec('read',read,project=broken_project), ToolSpec('other',other)
        harness = AgentHarness({'read':first,'other':second})
        result = await AgentLoop(Provider([ToolCall('1','read',{}),ToolCall('2','other',{})]),harness).run(
            'test',[AgentMessage('user','test')],[first,second],LoopPolicy())
        assert calls==['read'] and result.stop_reason=='internal_error'
        assert result.errors[0].stage=='observation'
        assert [item.tool_call_id for item in result.messages if item.role=='tool']==['1','2']
        assert 'sensitive implementation detail' not in str(harness.events)
    asyncio.run(run())


def test_duplicate_call_ids_rejected_before_any_tool_executes():
    async def run():
        async def read():
            pytest.fail('invalid batch must not execute')
        spec=ToolSpec('read',read)
        result=await AgentLoop(Provider([ToolCall('1','read',{}),ToolCall('1','read',{})]),
                               AgentHarness({'read':spec})).run('test',[AgentMessage('user','test')],[spec],LoopPolicy())
        assert result.stop_reason=='model_protocol_error' and result.actions==0
    asyncio.run(run())


def test_wrapped_rate_limit_is_not_immediately_retried():
    request=httpx.Request('GET','https://test')
    cause=httpx.HTTPStatusError('rate limit',request=request,response=httpx.Response(429,request=request))
    error=DianpingError('wrapped')
    error.__cause__=cause
    assert classify_error(error).code=='rate_limited'
    assert not classify_error(error).retryable


def test_provider_reuses_client_and_closes_it_explicitly():
    async def run():
        transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]}))
        provider=LLMProvider(LLMConfig(provider='deepseek',model='test',api_key='test',base_url='https://test'),transport)
        async with provider:
            await provider.complete('test',[{'role':'user','content':'first'}])
            client=provider._client
            await provider.complete('test',[{'role':'user','content':'second'}])
            assert provider._client is client and not client.is_closed
        assert client.is_closed
        with pytest.raises(RuntimeError,match='已关闭'):
            await provider.complete('test',[{'role':'user','content':'third'}])
    asyncio.run(run())


def test_phase_stop_reason_is_returned_and_survives_restart(tmp_path):
    import json
    from xiangkou.dianping import DianpingClient
    from xiangkou.planner import Planner
    from xiangkou.schemas import ChatRequest
    from xiangkou.workflow import DiscoveryWorkflow
    from test_itinerary import fake_dianping

    async def run():
        def model(request):
            body=json.loads(request.content)
            content='done' if body.get('tools') else '{"category":"美食","keyword":"火锅","sort":"score"}'
            return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
        provider=LLMProvider(LLMConfig(provider='deepseek',model='test',api_key='test',base_url='https://test'),
                             httpx.MockTransport(model))
        client=DianpingClient('http://test',transport=httpx.MockTransport(fake_dianping))
        path=str(tmp_path/'state.sqlite3')
        try:
            workflow=DiscoveryWorkflow(client,Planner(provider=provider),'http://test',path)
            response=await workflow.run(ChatRequest(message='找火锅店'),7)
            assert response.status=='ok'
            assert response.agent_outcomes[0]['phase']=='inspect'
            assert response.agent_outcomes[0]['stop_reason']=='model_finished'
            restarted=DiscoveryWorkflow(client,Planner(),'http://test',path)
            record=await restarted.runs.get(response.run_id,7)
            assert record['response']['agent_outcomes']==response.agent_outcomes
        finally:
            await provider.close()
            await client.close()
    asyncio.run(run())
