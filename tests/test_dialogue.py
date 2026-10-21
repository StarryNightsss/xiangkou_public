import asyncio

import httpx

from xiangkou.api import app, require_user
from xiangkou.dialogue import route_message
from xiangkou.llm import Completion
from xiangkou.protocol import ToolCall
from types import SimpleNamespace
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatResponse
from xiangkou.workflow import DiscoveryWorkflow


class Model:
    enabled = True
    config = SimpleNamespace(timeout=1, provider='test', model='conversation-test')

    def __init__(self, completion):
        self.completion = completion
        self.inputs = []

    async def complete(self, system, messages, **kwargs):
        self.inputs.append((system, messages, kwargs))
        return self.completion


def test_free_form_reply_is_generated_by_agent_with_history(tmp_path):
    async def run():
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example', str(tmp_path/'state.sqlite3'))
        sid = (await workflow.sessions.load(None, 7)).id
        turn = await workflow.conversations.begin(sid, 7, '今天工作有点累')
        workflow.conversations.finish(turn, '辛苦了，想休息一下吗？')
        model = Model(Completion(text='可以先放松一下，我可以陪你聊聊。'))
        workflow.planner.provider = model
        from xiangkou.schemas import ChatRequest
        response, harness = await route_message(ChatRequest(message='嗯，陪我随便聊聊吧', session_id=sid), workflow, 7)
        assert response.answer == model.completion.text and response.mode == 'model'
        assert response.response_kind == 'conversation'
        wire = model.inputs[0][1]
        assert wire[0]['content'] == '今天工作有点累' and wire[-1]['content'] == '嗯，陪我随便聊聊吧'
        assert harness.context.model_calls == 1
        assert model.inputs[0][2]['tools'][0]['name'] == 'run_workflow'
    asyncio.run(run())


def test_social_chat_preserves_business_plan_and_restores_task_snapshot(tmp_path):
    async def run():
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example', str(tmp_path / 'state.sqlite3'))
        workflow.planner.provider = Model(Completion(text='我是 巷口，很高兴认识你。'))
        state = await workflow.sessions.load(None, 7)
        plan = rule_plan('人均120以内的火锅')
        await workflow.sessions.save(state, plan, [1], [100])
        workflow.business_states.save(7, state.id, {'phase': 'awaiting_coupon', 'shop_id': 1}, expected_revision=0)
        prior = ChatResponse(answer='已找到店铺', mode='rules', status='ok', session_id=state.id,
                             run_id='prior-task', plan=plan)
        await workflow.runs.save(prior, 7)
        async def must_not_plan(*args, **kwargs):
            raise AssertionError('Social message entered discovery workflow')
        workflow.run = must_not_plan
        previous = getattr(app.state, 'workflow', None)
        app.state.workflow = workflow
        app.dependency_overrides[require_user] = lambda: {'id': 7}
        try:
            with workflow.sessions.database.connect() as conn:
                revision = conn.execute('SELECT revision FROM sessions WHERE id=?', (state.id,)).fetchone()[0]
            slots = workflow.business_states.get(7, state.id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://xiangkou') as c:
                result = await c.post('/api/chat', json={'message': '你好，你是谁', 'session_id': state.id})
                assert result.status_code == 200, result.text
                data = result.json()
                assert data['answer'] == '我是 巷口，很高兴认识你。' and data['response_kind'] == 'conversation'
                assert data['mode'] == 'model'
                assert data['events'][0]['event'] == 'run_start' and data['events'][-1]['event'] == 'run_end'
                assert not any(e['event'] == 'tool_start' for e in data['events'])
                messages = (await c.get(f'/api/sessions/{state.id}/messages')).json()
                assert messages['last_response']['response_kind'] == 'conversation'
                assert messages['last_task_response']['run_id'] == 'prior-task'
                assert messages['messages'][0]['text'] == '你好，你是谁'
            with workflow.sessions.database.connect() as conn:
                assert conn.execute('SELECT revision FROM sessions WHERE id=?', (state.id,)).fetchone()[0] == revision
                assert conn.execute('SELECT plan_json FROM sessions WHERE id=?', (state.id,)).fetchone()[0] == plan.model_dump_json()
            assert workflow.business_states.get(7, state.id) == slots
            restarted = DiscoveryWorkflow(None, Planner(), 'http://example', workflow.sessions.database.path)
            history, _ = await restarted.conversations.read(state.id, 7)
            assert '巷口' in history[1]['text']
        finally:
            app.state.workflow = previous
            app.dependency_overrides.pop(require_user, None)
    asyncio.run(run())


def test_handoff_uses_existing_workflow_and_shared_budget():
    async def run():
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example')
        class Sequence(Model):
            async def complete(self, system, messages, **kwargs):
                self.inputs.append((system, messages, kwargs))
                if len(self.inputs) == 1:
                    return Completion(tool_calls=[ToolCall('handoff', 'run_workflow', {})])
                assert messages[-1]['role'] == 'tool'
                assert '业务校验后的方案' in messages[-1]['content']
                return Completion(text='根据实际查询，这是你的方案。')
        workflow.planner.provider = Sequence(Completion())
        seen = []
        async def fake_run(body, **kwargs):
            seen.append((body, kwargs))
            from xiangkou.harness import AgentHarness
            h = AgentHarness({}, context=kwargs['run_context'], parent_span_id=kwargs['parent_span_id'])
            h.record('run_start'); h.record('run_end', status='ok')
            return ChatResponse(answer='业务校验后的方案', status='ok', mode='rules', session_id=body.session_id,
                                run_id=h.run_id, events=h.events)
        workflow.run = fake_run
        from xiangkou.api import _run_chat
        from xiangkou.schemas import ChatRequest
        previous = getattr(app.state, 'workflow', None)
        actions = getattr(app.state, 'actions', None)
        if hasattr(app.state, 'actions'): del app.state.actions
        app.state.workflow = workflow
        try:
            data = await _run_chat(ChatRequest(message='你好，预算改成80，看看我的券'), 7)
            assert len(seen) == 1 and seen[0][0].message == '你好，预算改成80，看看我的券'
            context = seen[0][1]['run_context']
            assert context.model_calls == 2 and context.calls == 1
            assert data.answer == "根据实际查询，这是你的方案。"
            assert data.events[0]['event'] == 'run_start' and data.events[-1]['event'] == 'run_end'
            assert len({e['root_run_id'] for e in data.events}) == 1
            assert any(e['event'] == 'conversation_handoff' for e in data.events)
        finally:
            app.state.workflow = previous
            if actions is not None: app.state.actions = actions
    asyncio.run(run())


def test_unknown_write_tool_and_provider_failure_cannot_execute_business():
    async def run():
        from xiangkou.schemas import ChatRequest
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example')
        sid = (await workflow.sessions.load(None, 7)).id
        for completion in (Completion(tool_calls=[ToolCall('bad', 'confirm_order', {'order_id':1})]), Completion()):
            workflow.planner.provider = Model(completion)
            response, _ = await route_message(ChatRequest(message='直接帮我付款', session_id=sid), workflow, 7)
            assert response.status == 'upstream_error' and response.response_kind == 'conversation'
        class Broken(Model):
            async def complete(self, *args, **kwargs):
                raise TimeoutError('private exception')
        workflow.planner.provider = Broken(Completion())
        response, _ = await route_message(ChatRequest(message='你好', session_id=sid), workflow, 7)
        assert response.status == 'upstream_error' and 'private' not in response.answer
    asyncio.run(run())


def test_loop_reads_missing_fields_and_executes_workflow_only_once(tmp_path):
    async def run():
        import json
        from xiangkou.schemas import ChatRequest
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example', str(tmp_path/'loop.sqlite3'))
        class Sequence(Model):
            async def complete(self, system, messages, **kwargs):
                self.inputs.append((system, messages, kwargs))
                if len(self.inputs) <= 2:
                    if len(self.inputs) == 2:
                        observation = json.loads(messages[-1]['content'])
                        assert observation['missing_fields'] == ['人数', '日期']
                        assert not observation['booking_supported']
                    return Completion(tool_calls=[ToolCall(str(len(self.inputs)), 'run_workflow', {})])
                return Completion(text='几个人、哪天去唱歌？目前我可以筛选店铺，还不能预约包厢。')
        workflow.planner.provider = Sequence(Completion())
        calls = []
        async def execute(harness):
            calls.append(harness)
            return ChatResponse(answer='缺少条件', mode='rules', status='needs_clarification', missing_fields=['人数','日期'])
        result, _ = await route_message(ChatRequest(message='想定个ktv'), workflow, 7, execute_workflow=execute)
        assert len(calls) == 1 and result.status == 'needs_clarification'
        assert '几个人' in result.answer and result.response_kind == 'task'
    asyncio.run(run())


def test_final_model_failure_keeps_verified_workflow_result():
    async def run():
        from xiangkou.schemas import ChatRequest
        workflow = DiscoveryWorkflow(None, Planner(), 'http://example')
        class Sequence(Model):
            async def complete(self, *args, **kwargs):
                if self.inputs:
                    raise TimeoutError('secret')
                self.inputs.append(True)
                return Completion(tool_calls=[ToolCall('one','run_workflow',{})])
        workflow.planner.provider = Sequence(Completion())
        async def execute(harness):
            return ChatResponse(answer='已验证的条件',mode='rules',status='needs_clarification',missing_fields=['人数'])
        response, _ = await route_message(ChatRequest(message='定KTV'),workflow,7,execute_workflow=execute)
        assert response.answer == '已验证的条件' and response.missing_fields == ['人数']
        assert any(e['event']=='reply_fallback' for e in response.events)
    asyncio.run(run())


def test_workflow_authentication_error_is_not_hidden_by_model():
    async def run():
        from xiangkou.schemas import ChatRequest
        from xiangkou.dianping import DianpingUnauthorized
        import pytest
        workflow=DiscoveryWorkflow(None,Planner(),'http://example')
        workflow.planner.provider=Model(Completion(tool_calls=[ToolCall('one','run_workflow',{})]))
        async def execute(harness):
            raise DianpingUnauthorized('login expired')
        with pytest.raises(DianpingUnauthorized):
            await route_message(ChatRequest(message='看看我的订单'),workflow,7,execute_workflow=execute)
    asyncio.run(run())


def test_workflow_coupon_details_reach_model_after_catalog_query(tmp_path):
    async def run():
        import json
        from xiangkou.schemas import ChatRequest, ShopCard
        workflow = DiscoveryWorkflow(None, Planner(), 'http://test', str(tmp_path/'state.sqlite3'))
        class Sequence(Model):
            async def complete(self, system, messages, **kwargs):
                self.inputs.append((system,messages,kwargs))
                if len(self.inputs)==1:
                    return Completion(tool_calls=[ToolCall('catalog','run_workflow',{})])
                observation=json.loads(messages[-1]['content'])
                coupon=observation['shops'][0]['coupons'][0]
                assert coupon['id']==2001 and coupon['payValue']==8900
                assert coupon['actualValue']==10000 and coupon['rules']=='演示'
                assert 'internal_secret' not in coupon
                return Completion(text='海底捞100元代金券售价89元。')
        workflow.planner.provider=Sequence(Completion())
        async def execute(harness):
            return ChatResponse(answer='在售券',status='ok',mode='rules',run_id='child',
                shops=[ShopCard(id=5,name='海底捞',url='http://test/5',reason='test',coupons=[
                    {'id':2001,'title':'100元券','payValue':8900,'actualValue':10000,'rules':'演示','internal_secret':'excluded'}])])
        result,_=await route_message(ChatRequest(message='帮我查海底捞的代金券'),workflow,7,execute_workflow=execute)
        assert '89' in result.answer and result.shops[0].coupons[0]['id']==2001
    asyncio.run(run())


def test_coupon_selection_cannot_finish_with_unverified_draft_claim(tmp_path):
    async def run():
        from xiangkou.schemas import ChatRequest
        workflow=DiscoveryWorkflow(None,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        sid=(await workflow.sessions.load(None,7)).id
        workflow.business_states.save(7,sid,{'phase':'awaiting_coupon'},expected_revision=0)
        class Sequence(Model):
            async def complete(self,system,messages,**kwargs):
                self.inputs.append((system,messages,kwargs))
                if len(self.inputs)==1: return Completion(text='草稿已经准备好了，请在弹出的确认窗口确认。')
                if len(self.inputs)==2:
                    assert 'Harness校验' in messages[-1]['content']
                    return Completion(tool_calls=[ToolCall('prepare','run_workflow',{})])
                return Completion(text='草稿已准备好，请核对。')
        workflow.planner.provider=Sequence(Completion())
        called=[]
        async def execute(harness):
            called.append(True)
            return ChatResponse(answer='真实草稿',status='ok',mode='rules',run_id='child',session_id=sid,
                action_draft={'action_id':'demo','operation':'创建待支付订单','voucher':{'id':2001,'pay_cents':8900}})
        result,h=await route_message(ChatRequest(message='第一张',session_id=sid),workflow,7,execute_workflow=execute)
        assert called==[True] and result.action_draft['voucher']['id']==2001
        assert any(e['event']=='agent_final_rejected' for e in h.events)
    asyncio.run(run())


def test_explaining_confirmation_window_does_not_claim_prepared_draft(tmp_path):
    async def run():
        from xiangkou.schemas import ChatRequest
        w=DiscoveryWorkflow(None,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        w.planner.provider=Model(Completion(text='你可以在确认窗口核对后确认提交；付费券只创建待支付订单。'))
        result,h=await route_message(ChatRequest(message='下单时需要确认吗'),w,7)
        assert result.status=='ok' and result.action_draft is None
        assert not any(e['event']=='agent_final_rejected' for e in h.events)
    asyncio.run(run())


def test_progress_question_reads_orders_instead_of_old_draft_claim(tmp_path):
    async def run():
        from xiangkou.schemas import ChatRequest
        w=DiscoveryWorkflow(None,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        sid=(await w.sessions.load(None,7)).id
        w.business_states.save(7,sid,{'phase':'awaiting_confirmation','action_id':'submitted'},expected_revision=0)
        class Sequence(Model):
            async def complete(self,system,messages,**kwargs):
                self.inputs.append((system,messages,kwargs))
                if len(self.inputs)==1:return Completion(text='还没下单，请重新确认。')
                if len(self.inputs)==2:return Completion(tool_calls=[ToolCall('query','run_workflow',{})])
                return Completion(text='订单已创建，当前待支付，尚未成为待核销券。')
        w.planner.provider=Sequence(Completion())
        async def execute(h):
            return ChatResponse(answer='实际订单',status='ok',mode='rules',run_id='child',session_id=sid,
                personal_orders=[{'order_id':'88','status':1,'pay_cents':8900}])
        result,h=await route_message(ChatRequest(message='我刚刚不是已经购买一个了么',session_id=sid),w,7,execute_workflow=execute)
        assert result.personal_orders[0]['status']==1 and '订单已创建' in result.answer
        assert len(w.planner.provider.inputs)==3 and any(e['event']=='agent_final_rejected' for e in h.events)
    asyncio.run(run())
