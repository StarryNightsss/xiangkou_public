import asyncio
import json
import httpx
import pytest
from xiangkou.actions import ActionError, VoucherActionWorkflow
from xiangkou.business import BusinessWorkflow
from xiangkou.checkpoints import CheckpointStore
from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest, ChatResponse, ShopCard
from xiangkou.storage import StateDatabase
from xiangkou.workflow import DiscoveryWorkflow
from test_itinerary import fake_dianping


async def seeded(tmp_path, backend):
    path = str(tmp_path / 'state.sqlite3')
    client = DianpingClient('http://test', transport=httpx.MockTransport(backend))
    workflow = DiscoveryWorkflow(client,Planner(),'http://test',path)
    session = (await workflow.sessions.load(None,7)).id
    run_id = '11111111-1111-1111-1111-111111111111'
    await workflow.runs.save(ChatResponse(answer='ok',status='ok',mode='rules',run_id=run_id,session_id=session,
                                         shops=[ShopCard(id=1,name='测试店',url='http://test/1',reason='test')]),7)
    return client, workflow, session, run_id, path


def coupon_backend(request):
    if request.url.path == '/shop/1':
        data = {'id':1,'name':'测试店'}
    elif request.url.path == '/voucher/list/1':
        data = [dict(id=i,shopId=1,title='券'+str(i),type=0,payValue=price,actualValue=1500)
                for i,price in [(9,1000),(10,0),(11,0)]]
    else:
        return httpx.Response(404)
    return httpx.Response(200,json={'success':True,'data':data})


def test_coupon_slots_survive_restart_and_abandon_blocks_confirmation(tmp_path):
    async def run():
        writes=[]
        def backend(request):
            if request.method == 'POST': writes.append(request.url.path)
            return coupon_backend(request)
        client,w,session,run_id,path = await seeded(tmp_path,backend)
        try:
            first = await BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs)).run(
                ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            assert len(first.business_choices)==3 and first.business_state=='awaiting_coupon'
            restarted=DiscoveryWorkflow(client,Planner(),'http://test',path)
            actions=VoucherActionWorkflow(client,restarted.runs)
            business=BusinessWorkflow(restarted,actions)
            second=await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            assert second.action_draft['voucher']['id']==10 and not writes
            free=await business.run(ChatRequest(message='换免费的',session_id=session),7,'owner')
            assert len(free.business_choices)==2
            with pytest.raises(ActionError):
                await actions.confirm(second.action_draft['action_id'],7,'owner')
            next_draft=await business.run(ChatRequest(message='第一张',session_id=session),7,'owner')
            result=await business.run(ChatRequest(message='先别下单',session_id=session),7,'owner')
            assert result.business_state=='abandoned'
            with pytest.raises(ActionError):
                await actions.confirm(next_draft.action_draft['action_id'],7,'owner')
            assert not writes
            assert restarted.business_states.get(8,session) is None
        finally: await client.close()
    asyncio.run(run())


def test_removed_second_coupon_never_silently_selects_third(tmp_path):
    async def run():
        removed=False
        def backend(request):
            response=coupon_backend(request)
            if removed and request.url.path=='/voucher/list/1':
                body=response.json(); body['data']=[coupon for coupon in body['data'] if coupon['id']!=10]
                return httpx.Response(200,json=body)
            return response
        client,w,session,run_id,_=await seeded(tmp_path,backend)
        try:
            business=BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs))
            await business.run(ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            removed=True
            changed=await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            assert changed.action_draft is None and changed.status=='needs_clarification'
        finally: await client.close()
    asyncio.run(run())


def test_held_plan_obeys_budget_reloads_assets_and_records_parent_reads(tmp_path):
    async def run():
        held=True
        def backend(request):
            if request.url.path=='/voucher-order/mine/vouchers':
                data=[{'order_id':'88','voucher_id':9,'shop_id':2,'status':2}] if held else []
                return httpx.Response(200,json={'success':True,'data':data,'total':len(data)})
            return fake_dianping(request)
        client=DianpingClient('http://test',transport=httpx.MockTransport(backend))
        w=DiscoveryWorkflow(client,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        try:
            business=BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs))
            result=await business.run(ChatRequest(message='用我的券，两人先吃火锅再唱歌，总预算300元'),7,'owner')
            assert result.status=='ok' and result.itineraries
            assert all(any(shop.id==2 for shop in itinerary.stops) and itinerary.estimated_total<=300 for itinerary in result.itineraries)
            assert any(item['tool']=='my_vouchers' for item in result.trace)
            stored=await w.runs.get(result.run_id,7)
            assert any(item['tool']=='my_vouchers' for item in stored['response']['trace'])
            held=False
            changed=await business.run(ChatRequest(message='预算400元',session_id=result.session_id),7,'owner')
            assert changed.status=='no_results' and not changed.itineraries
        finally: await client.close()
    asyncio.run(run())


def test_held_single_shop_hard_price_not_relaxed(tmp_path):
    async def run():
        def backend(request):
            if request.url.path=='/voucher-order/mine/vouchers':
                return httpx.Response(200,json={'success':True,'data':[{'shop_id':1,'status':2}],'total':1})
            if request.url.path=='/shop/search':
                response=fake_dianping(request).json(); response['total']=100
                return httpx.Response(200,json=response)
            return fake_dianping(request)
        client=DianpingClient('http://test',transport=httpx.MockTransport(backend))
        w=DiscoveryWorkflow(client,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        try:
            result=await BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs)).run(
                ChatRequest(message='用我的券推荐火锅，人均不超过70元'),7,'owner')
            assert result.status=='no_results' and not result.shops
        finally: await client.close()
    asyncio.run(run())


def test_legacy_cancel_timeout_recovers_after_restart_without_second_post(tmp_path):
    async def run():
        order_status=1; writes=[]
        def backend(request):
            nonlocal order_status
            if request.url.path=='/voucher-order/88':
                return httpx.Response(200,json={'success':True,'data':{'id':88,'voucherId':9,'status':order_status,'voucherType':0,'payValue':1000}})
            if request.url.path=='/voucher-order/88/cancel':
                writes.append('cancel'); order_status=4
                raise httpx.ReadTimeout('response lost after commit',request=request)
            if request.url.path=='/voucher-order/ordinary/9':
                return httpx.Response(200,json={'success':True,'data':88})
            return coupon_backend(request)
        client,w,session,run_id,path=await seeded(tmp_path,backend)
        try:
            actions=VoucherActionWorkflow(client,w.runs)
            draft=await actions.prepare(7,run_id,1,9)
            await actions.confirm(draft['action_id'],7,'owner')
            with pytest.raises(ActionError): await actions.cancel_order(draft['action_id'],7,'owner')
            restarted=VoucherActionWorkflow(client,DiscoveryWorkflow(client,Planner(),'http://test',path).runs)
            with pytest.raises(ActionError): await restarted.cancel_order(draft['action_id'],8,'other')
            result=await restarted.cancel_order(draft['action_id'],7,'owner')
            assert result['order_status']==4 and writes==['cancel']
            await restarted.cancel_order(draft['action_id'],7,'owner')
            assert writes==['cancel']
        finally: await client.close()
    asyncio.run(run())


def test_checkpoint_limit_and_v2_upgrade_keep_existing_actions(tmp_path):
    db=StateDatabase(str(tmp_path/'state.sqlite3'))
    with db.connect() as conn:
        conn.execute('DROP TABLE business_tasks'); conn.execute('PRAGMA user_version=2')
        conn.execute("INSERT INTO voucher_actions VALUES ('keep',7,'{}',1)")
    upgraded=StateDatabase(db.path)
    from xiangkou.planner import rule_plan
    checkpoints=CheckpointStore(upgraded,max_records=2)
    for i in range(3): checkpoints.save(str(i),7,'session',rule_plan('火锅'),i)
    assert len(checkpoints.recent(7))==2 and checkpoints.get('0',7) is None
    with upgraded.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==5
        assert conn.execute('SELECT id FROM voucher_actions').fetchone()[0]=='keep'


def test_model_assists_free_coupon_intent_but_cannot_submit(tmp_path):
    from xiangkou.llm import LLMConfig, LLMProvider
    async def run():
        writes=[]
        def backend(request):
            if request.method=='POST': writes.append(request.url.path)
            return coupon_backend(request)
        client,w,session,run_id,_=await seeded(tmp_path,backend)
        try:
            actions=VoucherActionWorkflow(client,w.runs)
            business=BusinessWorkflow(w,actions)
            await business.run(ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            provider=LLMProvider(LLMConfig(provider='anthropic',api_key='test',model='test-model',base_url='https://example.test'),
                httpx.MockTransport(lambda request:httpx.Response(200,json={'content':[{'type':'text','text':'{"intent":"free_coupon","execute":true}'}]})))
            w.planner.provider=provider
            result=await business.run(ChatRequest(message='那就零元的那款吧',session_id=session),7,'owner')
            assert result.mode=='model' and len(result.business_choices)==2
            assert all(choice['pay_cents']==0 for choice in result.business_choices)
            assert not writes and result.action_draft is None
        finally: await client.close()
    asyncio.run(run())


def test_cancel_order_number_followup_and_chat_confirmation_never_writes(tmp_path):
    async def run():
        writes=[]
        def backend(request):
            if request.method=='POST': writes.append(request.url.path)
            if request.url.path=='/voucher-order/mine':
                return httpx.Response(200,json={'success':True,'data':[],'total':0})
            if request.url.path=='/voucher-order/88':
                return httpx.Response(200,json={'success':True,'data':{'id':88,'voucherId':9,'status':1,'voucherType':0,'payValue':1000}})
            return coupon_backend(request)
        client,w,session,_,path=await seeded(tmp_path,backend)
        try:
            actions=VoucherActionWorkflow(client,w.runs)
            result=await BusinessWorkflow(w,actions).run(ChatRequest(message='取消订单',session_id=session),7,'owner')
            assert result.business_state=='awaiting_order'
            restarted=DiscoveryWorkflow(client,Planner(),'http://test',path)
            business=BusinessWorkflow(restarted,VoucherActionWorkflow(client,restarted.runs))
            draft=await business.run(ChatRequest(message='88',session_id=session),7,'owner')
            assert draft.action_draft['order_id']=='88'
            confirm=await business.run(ChatRequest(message='确认',session_id=session),7,'owner')
            assert confirm.action_draft['action_id']==draft.action_draft['action_id'] and not writes
        finally: await client.close()
    asyncio.run(run())


def test_interrupted_held_plan_cannot_resume_after_coupon_is_redeemed(tmp_path):
    async def run():
        client=DianpingClient('http://test',transport=httpx.MockTransport(lambda request:
            httpx.Response(200,json={'success':True,'data':[],'total':0}) if request.url.path=='/voucher-order/mine/vouchers'
            else fake_dianping(request)))
        w=DiscoveryWorkflow(client,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        try:
            from xiangkou.planner import rule_plan
            from xiangkou.session import SessionAccessError
            state=await w.sessions.load(None,7)
            plan=rule_plan('两人先吃火锅再唱歌，总预算400元'); plan.required_shop_ids=[2]
            w.checkpoints.save('task',7,state.id,plan,state.revision)
            w.checkpoints.phase('task','interrupted')
            with pytest.raises(SessionAccessError): await w.resume('task',7,token='owner')
        finally: await client.close()
    asyncio.run(run())


def test_held_nearby_shop_outside_radius_is_not_returned(tmp_path):
    async def run():
        def backend(request):
            if request.url.path=='/voucher-order/mine/vouchers':
                return httpx.Response(200,json={'success':True,'data':[{'shop_id':1,'status':2}],'total':1})
            return fake_dianping(request)
        client=DianpingClient('http://test',transport=httpx.MockTransport(backend))
        w=DiscoveryWorkflow(client,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        try:
            result=await BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs)).run(
                ChatRequest(message='用我的券，推荐附近火锅，人均100元'),7,'owner')
            assert result.status=='no_results' and not result.shops
        finally: await client.close()
    asyncio.run(run())


def test_held_resume_rechecks_assets_and_saves_complete_read_trace(tmp_path):
    async def run():
        def backend(request):
            if request.url.path=='/voucher-order/mine/vouchers':
                return httpx.Response(200,json={'success':True,'data':[{'shop_id':2,'status':2}],'total':1})
            return fake_dianping(request)
        client=DianpingClient('http://test',transport=httpx.MockTransport(backend))
        path=str(tmp_path/'state.sqlite3')
        w=DiscoveryWorkflow(client,Planner(),'http://test',path)
        try:
            from xiangkou.planner import rule_plan
            state=await w.sessions.load(None,7)
            plan=rule_plan('两人先吃火锅再唱歌，总预算300元'); plan.required_shop_ids=[2]
            w.checkpoints.save('task',7,state.id,plan,state.revision)
            w.checkpoints.phase('task','interrupted')
            restarted=DiscoveryWorkflow(client,Planner(),'http://test',path)
            result=await restarted.resume('task',7,token='owner')
            assert result.status=='ok' and any(row['tool']=='my_vouchers' for row in result.trace)
            assert all(any(shop.id==2 for shop in option.stops) for option in result.itineraries)
            record=await restarted.runs.get(result.run_id,7)
            assert any(row['tool']=='my_vouchers' for row in record['response']['trace'])
        finally: await client.close()
    asyncio.run(run())


def test_failed_coupon_replacement_keeps_original_draft_usable(tmp_path, monkeypatch):
    async def run():
        client, w, session, run_id, path = await seeded(tmp_path, coupon_backend)
        actions = VoucherActionWorkflow(client, w.runs)
        business = BusinessWorkflow(w, actions)
        try:
            await business.run(ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            first = await business.run(ChatRequest(message='第一张',session_id=session),7,'owner')
            original_id = first.action_draft['action_id']
            original_get_shop = client.get_shop
            async def unavailable(*args, **kwargs):
                raise ActionError('模拟新券复核失败', 409)
            monkeypatch.setattr(client, 'get_shop', unavailable)
            with pytest.raises(ActionError):
                await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            restarted = DiscoveryWorkflow(client, Planner(), 'http://test', path)
            assert restarted.business_states.get(7,session)['action_id'] == original_id
            assert (await VoucherActionWorkflow(client,restarted.runs).preview(original_id,7))['voucher']['id']==9
            monkeypatch.setattr(client, 'get_shop', original_get_shop)
            second = await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            assert second.action_draft['voucher']['id']==10
            with pytest.raises(ActionError):
                await actions.preview(original_id,7)
            assert w.business_states.get(7,session)['action_id']==second.action_draft['action_id']
        finally:
            await client.close()
    asyncio.run(run())


def test_cross_worker_slot_conflict_does_not_abandon_old_or_create_orphan(tmp_path, monkeypatch):
    from xiangkou.business_state import BusinessStateConflict, BusinessStateStore
    async def run():
        client, w, session, run_id, path = await seeded(tmp_path, coupon_backend)
        actions = VoucherActionWorkflow(client, w.runs)
        business = BusinessWorkflow(w, actions)
        try:
            await business.run(ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            first = await business.run(ChatRequest(message='第一张',session_id=session),7,'owner')
            original_id = first.action_draft['action_id']
            other = BusinessStateStore(StateDatabase(path))
            original_get_shop = client.get_shop
            async def concurrent_update(shop_id):
                record, revision = other.snapshot(7,session)
                other.save(7,session,{**record,'other_worker':True},expected_revision=revision)
                return await original_get_shop(shop_id)
            monkeypatch.setattr(client,'get_shop',concurrent_update)
            with pytest.raises(BusinessStateConflict):
                await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            assert (await actions.preview(original_id,7))['voucher']['id']==9
            assert other.get(7,session)['other_worker']
            with w.runs.database.connect() as conn:
                assert conn.execute('SELECT COUNT(*) FROM voucher_actions').fetchone()[0]==1
                assert conn.execute("SELECT COUNT(*) FROM voucher_action_events WHERE state='abandoned'").fetchone()[0]==0
        finally:
            await client.close()
    asyncio.run(run())


def test_draft_commit_storage_failure_rolls_back_slots_old_draft_and_audit(tmp_path, monkeypatch):
    async def run():
        client, w, session, run_id, path = await seeded(tmp_path, coupon_backend)
        actions = VoucherActionWorkflow(client, w.runs)
        business = BusinessWorkflow(w, actions)
        try:
            await business.run(ChatRequest(message='帮我领券',session_id=session,context_run_id=run_id),7,'owner')
            first = await business.run(ChatRequest(message='第一张',session_id=session),7,'owner')
            original_id = first.action_draft['action_id']
            original_slots = w.business_states.get(7,session)
            original_save = actions._save
            def fail_new_draft(action, connection=None):
                if action['voucher_id']==10 and action['state']=='prepared':
                    raise RuntimeError('模拟事务中途写入失败')
                return original_save(action,connection)
            monkeypatch.setattr(actions,'_save',fail_new_draft)
            with pytest.raises(RuntimeError):
                await business.run(ChatRequest(message='第二张',session_id=session),7,'owner')
            restarted = DiscoveryWorkflow(client,Planner(),'http://test',path)
            assert restarted.business_states.get(7,session)==original_slots
            assert (await VoucherActionWorkflow(client,restarted.runs).preview(original_id,7))['voucher']['id']==9
            with w.runs.database.connect() as conn:
                assert conn.execute('SELECT COUNT(*) FROM voucher_actions').fetchone()[0]==1
                assert conn.execute("SELECT COUNT(*) FROM voucher_action_events WHERE state='abandoned'").fetchone()[0]==0
        finally:
            await client.close()
    asyncio.run(run())


def test_business_prelude_and_discovery_have_complete_root_events(tmp_path, monkeypatch):
    from xiangkou.api import app, _run_chat
    async def run():
        client=DianpingClient('http://test',transport=httpx.MockTransport(fake_dianping))
        workflow=DiscoveryWorkflow(client,Planner(),'http://test',str(tmp_path/'state.sqlite3'))
        actions=VoucherActionWorkflow(client,workflow.runs)
        monkeypatch.setattr(app.state,'workflow',workflow,raising=False)
        monkeypatch.setattr(app.state,'actions',actions,raising=False)
        try:
            result=await _run_chat(ChatRequest(message='推荐美食，有优惠券吗'),7,token='owner')
            assert result.events[0]['event']=='run_start'
            assert result.events[-1]['event']=='run_end'
            assert result.events[-1]['parent_span_id'] is None
            assert [event['sequence'] for event in result.events]==sorted(event['sequence'] for event in result.events)
            persisted=await workflow.runs.get(result.run_id,7)
            assert persisted['events'][-1]['event']=='run_end'
        finally:
            await client.close()
    asyncio.run(run())


def test_personal_goods_vouchers_use_held_endpoint_and_logged_in_token(tmp_path):
    async def run():
        seen=[]
        def backend(request):
            seen.append((request.url.path,request.headers.get('authorization')))
            return httpx.Response(200,json={'success':True,'data':[{'order_id':'7','voucher_id':9,'status':2,'title':'我的商品券'}],'total':1})
        client,workflow,sid,_,_=await seeded(tmp_path,backend)
        try:
            business=BusinessWorkflow(workflow,VoucherActionWorkflow(client,workflow.runs))
            response=await business.run(ChatRequest(message='查询我的商品券',session_id=sid),7,'owner-token')
            assert response.personal_orders[0]['status']==2
            assert seen==[('/voucher-order/mine/vouchers','owner-token')]
            seen.clear()
            response=await business.run(ChatRequest(message='查询我的订单',session_id=sid),7,'owner-token')
            assert seen==[('/voucher-order/mine','owner-token')]
        finally:await client.close()
    asyncio.run(run())


def test_merchant_catalog_is_independent_of_owned_vouchers_and_old_price(tmp_path):
    async def run():
        from types import SimpleNamespace
        from xiangkou.llm import Completion
        calls = []
        class Provider:
            enabled = True
            config = SimpleNamespace(timeout=1, provider='test', model='test')
            async def complete(self, system, messages, **kwargs):
                return Completion(text=json.dumps({'intent':'merchant_vouchers','shop_keyword':'海底捞'}))
        def backend(request):
            calls.append(request.url.path)
            if request.url.path == '/shop-type/list':
                return httpx.Response(200,json={'success':True,'data':[{'id':1,'name':'美食'}]})
            if request.url.path == '/shop/search':
                assert request.url.params['keyword']=='海底捞'
                assert 'maxPrice' not in request.url.params and 'minScore' not in request.url.params
                return httpx.Response(200,json={'success':True,'data':[{'id':5,'name':'海底捞火锅','avgPrice':104,'score':49}], 'total':1})
            if request.url.path == '/voucher/list/5':
                return httpx.Response(200,json={'success':True,'data':[{'id':2001,'shopId':5,'title':'演示100元券','payValue':8900,'actualValue':10000,'type':0,'orderable':True}]})
            raise AssertionError('Must not query personal assets: '+request.url.path)
        client = DianpingClient('http://test',transport=httpx.MockTransport(backend))
        w = DiscoveryWorkflow(client,Planner(provider=Provider()),'http://test',str(tmp_path/'state.sqlite3'))
        try:
            session = (await w.sessions.load(None,7)).id
            w.business_states.save(7,session,{'phase':'held_plan'},expected_revision=0)
            result = await BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs)).run(
                ChatRequest(message='帮我查一下海底捞目前有哪些代金券',session_id=session),7,'owner')
            assert result.status=='ok' and result.shops[0].id==5
            assert result.shops[0].coupons[0]['id']==2001 and '89' in result.answer
            assert '/voucher/list/5' in calls and not result.personal_orders
            assert not result.action_draft
        finally: await client.close()
    asyncio.run(run())


def test_empty_held_vouchers_explain_existing_unpaid_order(tmp_path):
    async def run():
        calls=[]
        def backend(request):
            calls.append(request.url.path)
            assert request.headers.get('authorization')=='owner-token'
            if request.url.path=='/voucher-order/mine/vouchers':
                data=[]
            elif request.url.path=='/voucher-order/mine':
                data=[{'order_id':'88','voucher_id':2001,'shop_id':5,'title':'演示海底捞100元代金券','status':1,'pay_cents':8900}]
            else:raise AssertionError(request.url.path)
            return httpx.Response(200,json={'success':True,'data':data,'total':len(data)})
        client,w,sid,_,_=await seeded(tmp_path,backend)
        try:
            business=BusinessWorkflow(w,VoucherActionWorkflow(client,w.runs))
            w.business_states.save(7,sid,{'phase':'awaiting_confirmation','action_id':'submitted-action'},expected_revision=0)
            held=await business.run(ChatRequest(message='帮我看下我现在有海底捞的代金券了没有',session_id=sid),7,'owner-token')
            assert held.personal_orders[0]['status']==1 and '订单已创建' in held.answer
            assert '尚未进入待核销券包' in held.answer and held.action_draft is None
            calls.clear()
            progress=await business.run(ChatRequest(message='我刚刚不是已经购买一个了么',session_id=sid),7,'owner-token')
            assert calls==['/voucher-order/mine'] and progress.personal_orders[0]['order_id']=='88'
            assert '已创建订单' in progress.answer and progress.action_draft is None
        finally:await client.close()
    asyncio.run(run())
