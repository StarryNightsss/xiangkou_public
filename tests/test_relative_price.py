import asyncio
import json
import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.llm import Completion
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatRequest, SearchPlan
from xiangkou.session import SessionState, is_followup, merge_followup
from xiangkou.workflow import DiscoveryWorkflow, _hard_reasons
from types import SimpleNamespace


def test_relative_price_inherits_type_keyword_and_keeps_user_budget():
    prior = rule_plan('我想找个火锅店，预算1000')
    update = SearchPlan(category='美食', followup=True, relative_price='higher')
    assert is_followup('有更贵一些的吗', SessionState(id='session', plan=prior), update)
    merged = merge_followup('有更贵一些的吗', prior, update, [1,2], [104,101])
    assert merged.keyword == '火锅' and merged.category == '美食'
    assert merged.max_price == 1000 and merged.min_price == 105 and merged.price_reference == 104
    assert _hard_reasons({'typeId':1,'avgPrice':104}, merged, 1)
    assert not _hard_reasons({'typeId':1,'avgPrice':150}, merged, 1)
    assert _hard_reasons({'typeId':1,'avgPrice':1001}, merged, 1)
    cheaper = merge_followup('更便宜一些', prior, rule_plan('更便宜一些'), [1,2], [104,101])
    assert cheaper.max_price == 1000 and cheaper.price_reference == 101
    assert _hard_reasons({'typeId':1,'avgPrice':101}, cheaper, 1)
    assert not _hard_reasons({'typeId':1,'avgPrice':100}, cheaper, 1)
    reset = merge_followup('预算改成80', merged, rule_plan('预算改成80'), [1,2], [104,101])
    assert reset.max_price == 80 and reset.min_price is None and reset.relative_price is None


def test_model_receives_prior_task_and_understands_non_literal_comparison():
    async def run():
        class Provider:
            enabled=True
            config=SimpleNamespace(timeout=1, provider='test', model='test')
            async def complete(self, system, messages, **kwargs):
                data=json.loads(messages[0]['content'])
                assert data['previous_task']['plan']['keyword'] == '火锅'
                assert data['previous_task']['last_shop_prices'] == [104,101]
                return Completion(text=json.dumps({'category':'美食','followup':True,'relative_price':'higher'}))
        context={'plan':rule_plan('火锅，预算1000').model_dump(), 'last_shop_prices':[104,101]}
        update, mode = await Planner(provider=Provider()).plan('想把档次再往上提一提', context=context)
        assert mode == 'model' and update.relative_price == 'higher' and update.followup
        merged=merge_followup('想把档次再往上提一提', rule_plan('火锅，预算1000'), update, [1,2], [104,101])
        assert merged.keyword == '火锅' and merged.min_price == 105
    asyncio.run(run())


def test_workflow_finds_higher_price_beyond_first_three_and_checks_budget():
    async def run():
        shops=[{'id':1,'typeId':1,'name':'A火锅','avgPrice':104,'score':49},
               {'id':2,'typeId':1,'name':'B火锅','avgPrice':101,'score':44},
               {'id':3,'typeId':1,'name':'缺价格火锅','score':46},
               {'id':4,'typeId':1,'name':'C火锅','avgPrice':250,'score':42},
               {'id':5,'typeId':1,'name':'超预算火锅','avgPrice':1200,'score':48}]
        def server(request):
            path=request.url.path
            if path=='/shop-type/list': data=[{'id':1,'name':'美食'}]
            elif path=='/shop/search':
                assert request.url.params.get('keyword')=='火锅'
                eligible=[s for s in shops if s.get('avgPrice', 0)<=int(request.url.params.get('maxPrice') or 100000)]
                data=eligible[:int(request.url.params['size'])]
                return httpx.Response(200,json={'success':True,'data':data,'total':len(eligible)})
            elif path=='/shop/batch':
                ids=[int(i) for i in request.url.params['ids'].split(',')]
                data=[s for s in shops if s['id'] in ids]
            elif path.startswith('/shop/'):
                data=next(s for s in shops if str(s['id'])==path.rsplit('/',1)[-1])
            else: data=[]
            return httpx.Response(200,json={'success':True,'data':data})
        client=DianpingClient('http://example', transport=httpx.MockTransport(server))
        try:
            workflow=DiscoveryWorkflow(client, Planner(), 'http://web')
            first=await workflow.run(ChatRequest(message='我想找个火锅店，预算1000'),owner_user_id=7)
            assert [s.avg_price for s in first.shops]==[104,101]
            second=await workflow.run(ChatRequest(message='有更贵一些的吗', session_id=first.session_id),owner_user_id=7)
            assert second.status=='ok' and [s.id for s in second.shops]==[4]
            assert second.plan.min_price==105 and second.plan.max_price==1000
            third=await workflow.run(ChatRequest(message='有更贵一些的吗',session_id=first.session_id),owner_user_id=7)
            assert third.status=='no_results' and third.plan.keyword=='火锅'
            assert '不能编造' in third.answer and '¥1000' in third.answer and '¥250' in third.answer
        finally: await client.close()
    asyncio.run(run())
