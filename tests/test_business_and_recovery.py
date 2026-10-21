import asyncio
import httpx
import pytest

from xiangkou.actions import ActionError, VoucherActionWorkflow
from xiangkou.business import BusinessWorkflow
from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatRequest, ChatResponse, ItineraryCard, ShopCard
from xiangkou.session import SessionAccessError, merge_followup
from xiangkou.storage import StateDatabase
from xiangkou.workflow import DiscoveryWorkflow
from test_itinerary import fake_dianping


def test_structural_edits_clear_replacement_limits_and_support_per_stop_price():
    p = rule_plan('两人先吃火锅再唱歌，总预算400元')
    p = merge_followup('第二家太贵，换便宜点的', p, rule_plan('第二家太贵，换便宜点的'), [1, 11], [80, 70])
    p = merge_followup('改成先吃饭再喝酒', p, rule_plan('改成先吃饭再喝酒'), [1, 13], [80, 60])
    assert p.fixed_first_shop_id is None and p.second_price_ceiling is None
    p = rule_plan('两人先吃火锅再唱歌再喝酒，总预算600元')
    p = merge_followup('第三站每人最多50元', p, rule_plan('第三站每人最多50元'), [1, 11, 21], [80, 70, 90])
    assert p.stops[2].max_price == 50 and p.max_price is None
    p = merge_followup('交换前两站', p, rule_plan('交换前两站'), [1, 11, 21], [80, 70, 90])
    assert [s.category for s in p.stops] == ['KTV', '美食', '酒吧']
    p = merge_followup('删掉第二站', p, rule_plan('删掉第二站'), [11, 1, 21], [70, 80, 90])
    assert [s.category for s in p.stops] == ['KTV', '酒吧']


def test_pinned_shop_is_verified_even_when_missing_from_search_page():
    async def run():
        hide = False
        def backend(request):
            if hide and request.url.path == '/shop/search' and request.url.params.get('typeId') == '1':
                return httpx.Response(200, json={'success': True, 'data': [], 'total': 1})
            if request.url.path == '/shop/1':
                from test_itinerary import SHOPS
                return httpx.Response(200, json={'success': True, 'data': SHOPS[1][0]})
            return fake_dianping(request)
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        try:
            w = DiscoveryWorkflow(client, Planner(), 'http://test')
            first = await w.run(ChatRequest(message='两人先吃火锅再唱歌，总预算400元，尽量近'))
            hide = True
            changed = await w.run(ChatRequest(message='第二家太贵，换便宜点的', session_id=first.session_id))
            assert changed.status == 'ok' and changed.shops[0].id == first.shops[0].id
        finally:
            await client.close()
    asyncio.run(run())


def test_alternative_shop_can_prepare_and_restore_draft_without_writing(tmp_path):
    async def run():
        writes = []
        def backend(request):
            if request.method == 'POST': writes.append(request.url.path)
            if request.url.path == '/shop/2':
                data = {'id': 2, 'name': '备选店'}
            elif request.url.path == '/voucher/list/2':
                data = [{'id': 9, 'shopId': 2, 'type': 0, 'payValue': 0, 'actualValue': 500, 'title': '测试券'}]
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={'success': True, 'data': data})
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        path = str(tmp_path / 'state.sqlite3')
        w = DiscoveryWorkflow(client, Planner(), 'http://test', path)
        first = ShopCard(id=1, name='首选', url='http://test/1', reason='测试')
        alt = ShopCard(id=2, name='备选', url='http://test/2', reason='测试')
        run_id = '11111111-1111-1111-1111-111111111111'
        await w.runs.save(ChatResponse(answer='ok', status='ok', mode='rules', run_id=run_id, shops=[first],
                                     itineraries=[ItineraryCard(stops=[first], estimated_total=10, party_size=1, reason=''),
                                                  ItineraryCard(stops=[alt], estimated_total=10, party_size=1, reason='')]), 7)
        try:
            actions = VoucherActionWorkflow(client, w.runs)
            business = BusinessWorkflow(w, actions)
            response = await business.run(ChatRequest(message='帮我领第二个方案的券', context_run_id=run_id), 7, 'token')
            assert response.action_draft['voucher']['id'] == 9
            assert not writes
            restarted = VoucherActionWorkflow(client, DiscoveryWorkflow(client, Planner(), 'http://test', path).runs)
            draft = await restarted.preview(response.action_draft['action_id'], 7)
            assert draft['shop']['id'] == 2
            with pytest.raises(ActionError):
                await restarted.preview(draft['action_id'], 8)
        finally:
            await client.close()
    asyncio.run(run())


def test_interrupted_read_plan_survives_restart_and_rejects_stale_session(tmp_path):
    async def run():
        client = DianpingClient('http://test', transport=httpx.MockTransport(fake_dianping))
        path = str(tmp_path / 'state.sqlite3')
        w = DiscoveryWorkflow(client, Planner(), 'http://test', path)
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        w._execute = cancelled
        try:
            with pytest.raises(asyncio.CancelledError):
                await w.run(ChatRequest(message='两人先吃火锅再唱歌，总预算400元'), 7)
            task = w.checkpoints.recent(7)[0]
            assert task['phase'] == 'interrupted'
            restarted = DiscoveryWorkflow(client, Planner(), 'http://test', path)
            with pytest.raises(SessionAccessError):
                await restarted.resume(task['run_id'], 8)
            response = await restarted.resume(task['run_id'], 7)
            assert response.status == 'ok' and response.plan.total_budget == 400
            assert (await restarted.resume(response.run_id, 7)).run_id == response.run_id
            with pytest.raises(SessionAccessError):
                await restarted.resume(task['run_id'], 7)
        finally:
            await client.close()
    asyncio.run(run())


def test_schema_v1_upgrade_preserves_user_memory(tmp_path):
    path = str(tmp_path / 'v1.sqlite3')
    db = StateDatabase(path)
    with db.connect() as conn:
        conn.execute('DROP TABLE task_checkpoints')
        conn.execute('PRAGMA user_version=1')
        conn.execute('INSERT INTO user_memory VALUES (7, ?, 1)', ('{"area":"运河上街"}',))
    db = StateDatabase(path)
    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert conn.execute('SELECT COUNT(*) FROM user_memory').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM task_checkpoints').fetchone()[0] == 0


def test_personal_orders_use_current_token_and_cancel_requires_confirmation():
    async def run():
        writes = []
        status = 1
        def backend(request):
            nonlocal status
            if request.headers.get('authorization') != 'owner-token':
                return httpx.Response(401)
            if request.url.path == '/voucher-order/mine':
                data = [{'order_id': '88', 'voucher_id': 9, 'shop_id': 1, 'status': status, 'title': '测试券', 'voucher_type': 0}]
            elif request.url.path == '/voucher-order/88':
                data = {'id': 88, 'voucherId': 9, 'status': status, 'voucherType': 0, 'payValue': 1000}
            elif request.url.path == '/voucher-order/88/cancel' and request.method == 'POST':
                writes.append('cancel')
                status, data = 4, 88
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={'success': True, 'data': data, 'total': 1})
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        w = DiscoveryWorkflow(client, Planner(), 'http://test')
        actions = VoucherActionWorkflow(client, w.runs)
        business = BusinessWorkflow(w, actions)
        try:
            result = await business.run(ChatRequest(message='我的订单'), 7, 'owner-token')
            assert result.personal_orders[0]['order_id'] == '88'
            draft = await business.run(ChatRequest(message='取消最近的待支付订单'), 7, 'owner-token')
            assert draft.action_draft['order_id'] == '88' and not writes
            with pytest.raises(ActionError):
                await actions.confirm(draft.action_draft['action_id'], 8, 'other-token')
            result = await actions.confirm(draft.action_draft['action_id'], 7, 'owner-token')
            assert result['order_status'] == 4 and writes == ['cancel']
            await actions.confirm(draft.action_draft['action_id'], 7, 'owner-token')
            assert writes == ['cancel']
        finally:
            await client.close()
    asyncio.run(run())


def test_recovery_rejects_a_plan_after_user_changes_session(tmp_path):
    async def run():
        client = DianpingClient('http://test', transport=httpx.MockTransport(fake_dianping))
        w = DiscoveryWorkflow(client, Planner(), 'http://test', str(tmp_path / 'state.sqlite3'))
        try:
            state = await w.sessions.load(None, 7)
            plan = rule_plan('两人先吃火锅再唱歌，总预算400元')
            w.checkpoints.save('old-task', 7, state.id, plan, state.revision)
            await w.sessions.save(state, plan, [], [])
            with pytest.raises(SessionAccessError):
                await w.resume('old-task', 7)
        finally:
            await client.close()
    asyncio.run(run())


def test_usage_counts_are_visible_but_credentials_are_redacted():
    from xiangkou.harness import AgentHarness
    result = AgentHarness._redact({'usage': {'input_tokens': 12, 'output_tokens': 3},
                                   'token': 'private', 'input_tokens': 'private'})
    assert result['usage'] == {'input_tokens': 12, 'output_tokens': 3}
    assert result['token'] == result['input_tokens'] == '[redacted]'


def test_upstream_failure_can_retry_persisted_plan_after_restart(tmp_path):
    async def run():
        failed = True
        def backend(request):
            if failed:
                return httpx.Response(503)
            return fake_dianping(request)
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        path = str(tmp_path / 'state.sqlite3')
        try:
            workflow = DiscoveryWorkflow(client, Planner(), 'http://test', path)
            response = await workflow.run(ChatRequest(message='两人先吃火锅再唱歌，总预算400元'), 7)
            assert response.status == 'upstream_error'
            assert workflow.checkpoints.get(response.run_id, 7)['phase'] == 'retryable'
            failed = False
            restarted = DiscoveryWorkflow(client, Planner(), 'http://test', path)
            restored = await restarted.resume(response.run_id, 7)
            assert restored.status == 'ok' and restored.plan.total_budget == 400
        finally:
            await client.close()
    asyncio.run(run())
