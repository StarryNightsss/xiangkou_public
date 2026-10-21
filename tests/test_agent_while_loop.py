import asyncio
from types import SimpleNamespace

import pytest

from xiangkou.agent_loop import AgentLoop, LoopPolicy
from xiangkou.harness import AgentHarness
from xiangkou.llm import Completion
from xiangkou.protocol import AgentMessage, ToolCall, ToolSpec
from xiangkou.run_context import RunContext


class Provider:
    config = SimpleNamespace(timeout=1, provider='test', model='test')


def test_last_allowed_tool_result_returns_to_model_for_final_answer():
    async def run():
        calls=[]
        async def read():
            calls.append('read')
            return {'value':42}
        spec=ToolSpec('read',read)
        class Model(Provider):
            turns=0
            async def complete(self, system, messages, **kwargs):
                self.turns+=1
                if self.turns==1:
                    return Completion(tool_calls=[ToolCall('1','read',{})])
                assert kwargs['tools']==[]
                assert messages[-1]['role']=='tool' and '42' in messages[-1]['content']
                return Completion(text='结果为42')
        model=Model()
        result=await AgentLoop(model,AgentHarness({'read':spec})).run(
            'test',[AgentMessage('user','read')],[spec],LoopPolicy(max_actions=1))
        assert calls==['read'] and model.turns==2
        assert result.stop_reason=='model_finished' and result.messages[-1].content=='结果为42'
    asyncio.run(run())


def test_endless_duplicate_requests_stop_at_shared_model_budget():
    async def run():
        calls=[]
        async def read():
            calls.append('read')
        spec=ToolSpec('read',read)
        class Model(Provider):
            turns=0
            async def complete(self,*args,**kwargs):
                self.turns+=1
                return Completion(tool_calls=[ToolCall(str(self.turns),'read',{})])
        model=Model();context=RunContext(max_model_calls=5)
        result=await AgentLoop(model,AgentHarness({'read':spec},context=context)).run(
            'test',[AgentMessage('user','read')],[spec],LoopPolicy())
        assert model.turns==5 and calls==['read']
        assert result.failed and result.stop_reason=='model_budget'
    asyncio.run(run())


def test_cancel_after_tool_prevents_next_model_turn():
    async def run():
        context=RunContext()
        async def read():
            context.cancelled.set()
            return {'value':42}
        spec=ToolSpec('read',read)
        class Model(Provider):
            turns=0
            async def complete(self,*args,**kwargs):
                self.turns+=1
                return Completion(tool_calls=[ToolCall('1','read',{})])
        model=Model()
        with pytest.raises(asyncio.CancelledError):
            await AgentLoop(model,AgentHarness({'read':spec},context=context)).run(
                'test',[AgentMessage('user','read')],[spec],LoopPolicy())
        assert model.turns==1
    asyncio.run(run())


@pytest.mark.parametrize('context, reason',[
    (RunContext(max_duration=0),'deadline_exceeded'),
    (RunContext(max_tokens=1,input_tokens=1),'token_budget'),
])
def test_shared_resource_budget_prevents_new_model_call(context,reason):
    async def run():
        class Model(Provider):
            async def complete(self,*args,**kwargs):
                pytest.fail('Exhausted budget must prevent model call')
        result=await AgentLoop(Model(),AgentHarness({},context=context)).run(
            'test',[AgentMessage('user','hello')],[],LoopPolicy())
        assert result.failed and result.stop_reason==reason
    asyncio.run(run())
