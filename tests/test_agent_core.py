"""Regression gates for shared budgets, interruption and phase authorization."""
import asyncio
from types import SimpleNamespace

import pytest

from xiangkou.agent_loop import AgentLoop, LoopPolicy
from xiangkou.business_state import BusinessStateConflict, BusinessStateStore
from xiangkou.harness import AgentHarness, HarnessError
from xiangkou.llm import Completion, ToolCall
from xiangkou.protocol import AgentMessage, ToolSpec
from xiangkou.run_context import RunContext
from xiangkou.storage import StateDatabase
from xiangkou.tool_args import ShopIdArgs


def test_parent_child_share_budget_and_ordered_event_ids():
    async def run():
        async def read():
            return 1
        context = RunContext(max_calls=2)
        parent = AgentHarness({'read': read}, context=context)
        child = AgentHarness({'read': read}, context=context, parent_span_id=parent.run_id)
        await parent.call('read')
        await child.call('read', tool_call_id='model-call')
        with pytest.raises(HarnessError, match='调用预算'):
            await parent.call('read')
        assert context.calls == 2
        events = parent.events + child.events
        assert {event['root_run_id'] for event in events} == {parent.run_id}
        assert sorted(event['sequence'] for event in events) == [1, 2, 3, 4]
        assert child.events[1]['tool_call_id'] == 'model-call'
        assert child.events[1]['parent_span_id'] == parent.run_id
    asyncio.run(run())


def test_shared_cancellation_stops_running_tool_and_future_child():
    async def run():
        started, ended = asyncio.Event(), asyncio.Event()
        async def read():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        context = RunContext()
        harness = AgentHarness({'read': read}, context=context)
        task = asyncio.create_task(harness.call('read'))
        await started.wait()
        context.cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set()
        with pytest.raises(asyncio.CancelledError):
            await AgentHarness({'read': read}, context=context).call('read')
    asyncio.run(run())


def test_child_cannot_reset_parent_deadline():
    async def run():
        context = RunContext(max_duration=0.001)
        context.started -= 1
        async def read():
            pytest.fail('expired task must not execute')
        with pytest.raises(HarnessError, match='时长预算'):
            await AgentHarness({'read': read}, context=context).call('read')
    asyncio.run(run())


def test_model_usage_and_call_quota_are_shared():
    async def run():
        context = RunContext(max_tokens=10)
        parent = AgentHarness({}, context=context)
        async def model():
            return Completion(usage={'input_tokens': 8, 'output_tokens': 2})
        await parent.model_call(model)
        with pytest.raises(HarnessError, match='Token'):
            await AgentHarness({}, context=context).model_call(model)
        assert context.model_calls == 1
    asyncio.run(run())


def test_async_hooks_can_block_and_cannot_expand_result_past_limit():
    async def run():
        executions = []
        async def read():
            executions.append(1)
            return 'ok'
        async def block(*args):
            return False
        denied = AgentHarness({'read': read}, before_tool_call=block)
        with pytest.raises(HarnessError, match='阻止'):
            await denied.call('read')
        assert not executions and denied.context.calls == 0
        async def expand(*args):
            return 'x' * 100
        bounded = AgentHarness({'read': read}, after_tool_call=expand, max_result_bytes=20)
        with pytest.raises(HarnessError, match='大小限制'):
            await bounded.call('read')
        assert bounded.trace[-1]['ok'] is False
    asyncio.run(run())


def test_generic_loop_continues_beyond_old_turn_limit():
    async def run():
        observed = []
        async def read(shop_id):
            observed.append(shop_id)
            return {'id': shop_id}
        spec = ToolSpec('read', read, 'test read', ShopIdArgs)
        harness = AgentHarness({'read': spec})
        class Provider:
            config = SimpleNamespace(timeout=1, provider='test', model='test')
            turns = 0
            async def complete(self, system, messages, tools, max_tokens):
                self.turns += 1
                assert tools[0]['parameters']['additionalProperties'] is False
                if self.turns <= 3:
                    return Completion(tool_calls=[ToolCall(str(self.turns), 'read', {'shop_id': self.turns})])
                assert len([item for item in messages if item['role'] == 'tool']) == 3
                return Completion(text='done')
        provider = Provider()
        result = await AgentLoop(provider, harness).run('test', [AgentMessage('user', 'read')],
                                                       [spec], LoopPolicy())
        assert observed == [1, 2, 3] and provider.turns == 4
        assert result.stop_reason == 'model_finished' and not result.failed
        assert [item.tool_call_id for item in result.messages if item.role == 'tool'] == ['1', '2', '3']
    asyncio.run(run())


def test_generic_loop_rejects_write_without_phase_permission():
    async def run():
        async def write():
            pytest.fail('write tool must not execute')
        spec = ToolSpec('write', write, read_only=False)
        class Provider:
            config = SimpleNamespace(timeout=1, provider='test', model='test')
            async def complete(self, *args, **kwargs):
                return Completion(tool_calls=[ToolCall('1', 'write', {})])
        result = await AgentLoop(Provider(), AgentHarness({'write': spec})).run(
            'test', [AgentMessage('user', 'write')], [spec], LoopPolicy())
        assert result.actions == 0
        assert result.failed and result.stop_reason == "model_budget"
        assert all(item.is_error for item in result.messages if item.role == "tool")
    asyncio.run(run())


@pytest.mark.parametrize('persistent', [False, True])
def test_stale_business_state_cannot_overwrite_or_resurrect_after_clear(tmp_path, persistent):
    database = StateDatabase(str(tmp_path / 'state.sqlite3')) if persistent else None
    first = BusinessStateStore(database)
    first.save(7, 'session', {'phase': 'awaiting_coupon'}, expected_revision=0)
    second = BusinessStateStore(database) if persistent else first
    _, revision = second.snapshot(7, 'session')
    first.clear(7, 'session', expected_revision=revision)
    with pytest.raises(BusinessStateConflict):
        second.save(7, 'session', {'phase': 'awaiting_confirmation'}, expected_revision=revision)
    assert first.get(7, 'session') is None
    _, current = second.snapshot(7, 'session')
    second.save(7, 'session', {'phase': 'awaiting_order'}, expected_revision=current)
    assert first.get(7, 'session')['phase'] == 'awaiting_order'
    assert first.get(8, 'session') is None


def test_awaiting_hooks_cannot_bypass_local_call_budget():
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def hook(*args):
            nonlocal count
            count += 1
            if count == 2:
                ready.set()
            await release.wait()
        async def read():
            return 1
        harness = AgentHarness({'read': read}, max_calls=1, context=RunContext(max_calls=10), before_tool_call=hook)
        tasks = [asyncio.create_task(harness.call('read')) for _ in range(2)]
        await ready.wait()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert results.count(1) == 1
        assert sum(isinstance(result, HarnessError) for result in results) == 1
        assert harness.calls == 1 and harness.context.calls == 1
    asyncio.run(run())


def test_business_session_lock_is_shared_between_workflow_instances():
    async def run():
        store = BusinessStateStore(None)
        lock = store.lock(7, 'session')
        assert store.lock(7, 'session') is lock
        assert store.lock(8, 'session') is not lock
        assert store.lock(7, 'another') is not lock
    asyncio.run(run())
