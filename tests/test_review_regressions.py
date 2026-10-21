"""业务边界回归：使用模拟点评接口，不连接真实订单或运行数据库。"""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from xiangkou.actions import ActionError, VoucherActionWorkflow
from xiangkou.dianping import DianpingClient, DianpingError
from xiangkou.llm import Completion
from xiangkou.memory import MemoryPreferences, UserMemoryStore, apply_memory
from xiangkou.planner import Planner, rule_plan
from xiangkou.runs import RunStore
from xiangkou.schemas import ChatRequest, ChatResponse, SearchPlan, ShopCard
from xiangkou.session import SessionAccessError, merge_followup
from xiangkou.storage import StateDatabase
from xiangkou.workflow import DiscoveryWorkflow


def shop_client(shops, calls):
    def backend(request):
        path, params = request.url.path, request.url.params
        calls.append((path, dict(params)))
        if path == '/shop-type/list':
            data = [{'id': 1, 'name': '美食'}, {'id': 2, 'name': 'KTV'}]
        elif path == '/shop/areas':
            data = ['运河上街']
        elif path == '/shop/search':
            data = [s for s in shops if s['typeId'] == int(params['typeId'])
                    and (not params.get('keyword') or params['keyword'] in s['name'])
                    and (not params.get('area') or params['area'] == s['area'])
                    and (not params.get('maxPrice') or s['avgPrice'] <= int(params['maxPrice']))]
            total = len(data)
            start = (int(params['page']) - 1) * int(params['size'])
            return httpx.Response(200, json={'success': True, 'data': data[start:start + int(params['size'])], 'total': total})
        elif path == '/shop/batch':
            ids = {int(v) for v in params['ids'].split(',')}
            data = [s for s in shops if s['id'] in ids]
        elif path.startswith('/shop/'):
            data = next((s for s in shops if str(s['id']) == path.split('/')[-1]), {})
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={'success': True, 'data': data})
    return DianpingClient('http://test', transport=httpx.MockTransport(backend))


def shops(count=6):
    return [dict(id=i, name=f'火锅{i}', typeId=1, area='运河上街', avgPrice=80,
                 score=48, x=120.1 + i * .0001, y=30.3) for i in range(1, count + 1)]


def test_single_total_budget_is_distinct_from_per_person_and_can_be_increased():
    async def run():
        values = shops(2)
        values[0]['avgPrice'] = 150
        client = shop_client(values, [])
        try:
            workflow = DiscoveryWorkflow(client, Planner(), 'http://test')
            first = await workflow.run(ChatRequest(message='两人吃火锅，总预算200元'))
            assert first.status == 'ok'
            assert first.plan.max_price is None
            assert [s.id for s in first.shops] == [2]
            assert all(s.avg_price * 2 <= 200 for s in first.shops)
            changed = await workflow.run(ChatRequest(message='总预算改成400元', session_id=first.session_id))
            assert changed.status == 'ok' and {s.id for s in changed.shops} == {1, 2}
            missing = await workflow.run(ChatRequest(message='找火锅，总预算200元'))
            assert missing.status == 'needs_clarification' and missing.missing_fields == ['人数']
            explicit = rule_plan('两人吃火锅，总预算200元，人均80以内')
            assert explicit.max_price == 80 and explicit.total_budget == 200
        finally:
            await client.close()
    asyncio.run(run())


def test_model_cannot_reinterpret_explicit_total_budget_as_per_person():
    class Provider:
        enabled = True
        config = SimpleNamespace(timeout=1, provider='test', model='test')
        async def complete(self, *args, **kwargs):
            return Completion(text=json.dumps({'category': '美食', 'keyword': '火锅', 'max_price': 200}))
    async def run():
        plan, mode = await Planner(provider=Provider()).plan('两人吃火锅，总预算200元')
        assert mode == 'model' and plan.total_budget == 200 and plan.max_price is None
        unrestricted, _ = await Planner(provider=Provider()).plan('不限商圈，其他条件不变')
        assert unrestricted.area_unrestricted
        apply_memory(unrestricted, MemoryPreferences(area='运河上街'), '哪里都行')
        assert unrestricted.area is None
    asyncio.run(run())


def test_replacements_reach_shops_beyond_the_initial_page():
    async def run():
        calls = []
        client = shop_client(shops(30), calls)
        try:
            workflow = DiscoveryWorkflow(client, Planner(), 'http://test')
            result = await workflow.run(ChatRequest(message='找火锅'))
            for _ in range(4):
                result = await workflow.run(ChatRequest(message='换一家', session_id=result.session_id))
                assert result.status == 'ok'
            assert result.shops[0].id == 5
            later = await workflow.run(ChatRequest(message='找火锅'), extra_excluded=list(range(1, 25)))
            assert later.status == 'ok' and [s.id for s in later.shops] == [25, 26, 27]
            assert any(path == '/shop/search' and params['page'] == '2' for path, params in calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_area_nearby_itinerary_and_generated_area_relaxation():
    async def run():
        values = shops(1) + [dict(id=11, name='KTV', typeId=2, area='运河上街', avgPrice=70,
                                  score=48, x=120.1002, y=30.3)]
        client = shop_client(values, [])
        try:
            workflow = DiscoveryWorkflow(client, Planner(), 'http://test')
            result = await workflow.run(ChatRequest(message='两人在运河上街附近先吃火锅再唱歌，总预算400元'))
            assert result.status == 'ok' and result.itineraries
            distance = await workflow.run(ChatRequest(message='两人在运河上街先吃火锅再唱歌，总预算400元，选最近的'))
            assert distance.status == 'needs_location'
            merged = merge_followup('不限商圈，其他条件不变', SearchPlan(category='美食', area='运河上街'),
                                    rule_plan('不限商圈，其他条件不变'), [], [])
            assert merged.area is None and merged.area_unrestricted
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('persistent', [False, True])
def test_rejected_recovery_does_not_invalidate_live_request_or_create_session(tmp_path, persistent):
    async def run():
        client = shop_client(shops(), [])
        workflow = DiscoveryWorkflow(client, Planner(), 'http://test',
                                     str(tmp_path / 'state.sqlite3') if persistent else None)
        try:
            initial = await workflow.sessions.load(None, 7)
            plan = rule_plan('找火锅')
            workflow.checkpoints.save('old-task', 7, initial.id, plan, initial.revision)
            live = await workflow.sessions.load(initial.id, 7)
            with pytest.raises(SessionAccessError):
                await workflow.resume('old-task', 7)
            assert await workflow.sessions.save(live, plan, [], [])
            assert await workflow.sessions.clear(initial.id, 7)
            with pytest.raises(SessionAccessError):
                await workflow.resume('old-task', 7)
            assert await workflow.sessions.list_recent(7) == []
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('persistent', [False, True])
def test_preflight_failure_can_be_retried_without_duplicate_order(tmp_path, persistent):
    async def run():
        fail = False
        writes = []
        def backend(request):
            if request.url.path == '/shop/1':
                data = {'id': 1, 'name': '测试店'}
            elif request.url.path == '/voucher/list/1':
                if fail:
                    raise httpx.ReadTimeout('preflight unavailable')
                data = [dict(id=9, shopId=1, title='券', rules='堂食', payValue=100, actualValue=200, type=0)]
            elif request.url.path == '/voucher-order/ordinary/9':
                writes.append(request.url.path)
                data = 88
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={'success': True, 'data': data})
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        runs = RunStore(database=StateDatabase(str(tmp_path / 'state.sqlite3')) if persistent else None)
        actions = VoucherActionWorkflow(client, runs)
        try:
            await runs.save(ChatResponse(answer='ok', status='ok', mode='rules', run_id='run',
                                        shops=[ShopCard(id=1, name='店', url='/', reason='')]), 7)
            draft = await actions.prepare(7, 'run', 1, 9)
            fail = True
            with pytest.raises(DianpingError):
                await actions.confirm(draft['action_id'], 7, 'fake-token')
            assert not writes and (await actions.status(draft['action_id'], 7, 'fake-token'))['state'] == 'prepared'
            fail = False
            result = await actions.confirm(draft['action_id'], 7, 'fake-token')
            assert result['state'] == 'submitted'
            await actions.confirm(draft['action_id'], 7, 'fake-token')
            assert len(writes) == 1
        finally:
            await client.close()
    asyncio.run(run())


def test_slow_confirmation_does_not_block_other_users_and_same_action_is_idempotent():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        writes = []
        class Client:
            async def list_vouchers(self, shop_id):
                if shop_id == 1:
                    started.set()
                    await release.wait()
                return [dict(id=shop_id, shopId=shop_id, title='券', rules='堂食', type=0, payValue=100, actualValue=200)]
            async def create_voucher_order(self, voucher_id, kind, token):
                writes.append(voucher_id)
                return str(voucher_id)
        actions = VoucherActionWorkflow(Client(), RunStore())
        for i in (1, 2):
            coupon = dict(id=i, shopId=i, title='券', rules='堂食', type=0, payValue=100, actualValue=200)
            actions._save(dict(id=str(i), user_id=i, shop_id=i, voucher_id=i,
                               fingerprint=actions._fingerprint(coupon), kind='ordinary', created=time.time(),
                               state='prepared', order_id=None, pay_cents=100))
        first = asyncio.create_task(actions.confirm('1', 1, 'fake-token'))
        duplicate = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            duplicate = asyncio.create_task(actions.confirm('1', 1, 'fake-token'))
            other = await asyncio.wait_for(actions.confirm('2', 2, 'fake-token'), 1)
            assert other['state'] == 'submitted' and writes == [2]
            release.set()
            await first
            await duplicate
            assert writes == [2, 1]
        finally:
            release.set()
            await asyncio.gather(first, *([duplicate] if duplicate else []), return_exceptions=True)
    asyncio.run(run())


def test_seckill_status_falls_back_to_owned_voucher_order():
    async def run():
        paths = []
        def backend(request):
            paths.append(request.url.path)
            if request.url.path == '/voucher-order/88':
                return httpx.Response(200, json={'success': False, 'errorMsg': '订单不存在'})
            if request.url.path == '/voucher-order/of/voucher/9':
                return httpx.Response(200, json={'success': True, 'data': {
                    'id': 88, 'voucherId': 9, 'status': 2, 'createTime': '2026-10-19T20:00:00'}})
            return httpx.Response(404)
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        actions = VoucherActionWorkflow(client, RunStore())
        actions._save(dict(id='action', user_id=7, shop_id=1, voucher_id=9, fingerprint=(), kind='seckill',
                           created=time.time(), state='submitted', order_id='88', pay_cents=990))
        try:
            result = await actions.status('action', 7, 'fake-token')
            assert result['state'] == 'submitted' and result['order_status'] == 2
            assert paths == ['/voucher-order/88', '/voucher-order/of/voucher/9']
        finally:
            await client.close()
    asyncio.run(run())


def test_seckill_status_ignores_unrelated_historical_order():
    async def run():
        paths = []
        def backend(request):
            paths.append(request.url.path)
            if request.url.path == '/voucher-order/88':
                return httpx.Response(200, json={'success': False, 'errorMsg': '订单不存在'})
            if request.url.path == '/voucher-order/of/voucher/9':
                # 同券历史订单：不是本次的订单号，不能被当成本次结果。
                return httpx.Response(200, json={'success': True, 'data': {
                    'id': 77, 'voucherId': 9, 'status': 2, 'createTime': '2026-10-01T10:00:00'}})
            return httpx.Response(404)
        client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
        actions = VoucherActionWorkflow(client, RunStore())
        actions._save(dict(id='action', user_id=7, shop_id=1, voucher_id=9, fingerprint=(), kind='seckill',
                           created=time.time(), state='submitted', order_id='88', pay_cents=990))
        try:
            result = await actions.status('action', 7, 'fake-token')
            assert result['state'] == 'submitted' and 'order_status' not in result
            assert paths == ['/voucher-order/88', '/voucher-order/of/voucher/9']
        finally:
            await client.close()
    asyncio.run(run())


def test_sqlite_write_lock_does_not_block_the_event_loop(tmp_path):
    async def run():
        database = StateDatabase(str(tmp_path / 'state.sqlite3'))
        memory = UserMemoryStore(database)
        pending = None
        with database.connect() as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                pending = asyncio.create_task(memory.set(7, MemoryPreferences(max_price=100)))
                # The loop must remain free to release the lock while this write waits.
                await asyncio.sleep(.02)
                assert not pending.done()
            finally:
                blocker.commit()
                if pending:
                    await pending
        assert (await memory.get(7)).max_price == 100
    asyncio.run(run())


def test_replaced_draft_cannot_submit_after_a_slow_preflight():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        writes = []
        coupon = dict(id=9, shopId=1, title='券', rules='堂食', type=0, payValue=100, actualValue=200)
        class Client:
            async def list_vouchers(self, shop_id):
                started.set()
                await release.wait()
                return [coupon]
            async def create_voucher_order(self, *args):
                writes.append(1)
                return '88'
        actions = VoucherActionWorkflow(Client(), RunStore())
        action = dict(id='old', user_id=7, shop_id=1, voucher_id=9, fingerprint=actions._fingerprint(coupon),
                      kind='ordinary', created=time.time(), state='prepared', order_id=None, pay_cents=100)
        actions._save(action)
        pending = asyncio.create_task(actions.confirm('old', 7, 'fake-token'))
        try:
            await asyncio.wait_for(started.wait(), 1)
            actions._save({**action, 'state': 'abandoned'})
            release.set()
            with pytest.raises(ActionError):
                await pending
            assert not writes
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(run())
