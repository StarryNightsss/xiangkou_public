import asyncio,json,os
from unittest.mock import patch
from types import SimpleNamespace
import httpx
from xiangkou.api import app,require_user
from xiangkou.dianping import DianpingClient


def test_management_is_opt_in_and_validates_amount_time_and_origin():
    async def run():
        seen=[]
        def backend(request):
            assert request.headers['authorization']=='owner'
            if request.method=='GET':return httpx.Response(200,json={'success':True,'data':{'id':5}})
            seen.append(json.loads(request.content))
            assert request.url.path=='/voucher'
            return httpx.Response(200,json={'success':True,'data':3001})
        client=DianpingClient('http://go-api',transport=httpx.MockTransport(backend))
        old=getattr(app.state,'workflow',None);app.state.workflow=SimpleNamespace(client=client)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',headers={'authorization':'owner'}) as c:
                body={'title':'100元券','rules':'仅供演示','payValue':8900,'actualValue':10000,'type':0}
                endpoint='/api/catalog/shops/5/vouchers'
                with patch.dict(os.environ,{'XIANGKOU_CATALOG_ADMIN_IDS':''}):
                    assert (await c.post(endpoint,json=body)).status_code==403
                with patch.dict(os.environ,{'XIANGKOU_CATALOG_ADMIN_IDS':'7'}):
                    assert (await c.post(endpoint,json=body,headers={'Origin':'https://evil.example'})).status_code==403
                    assert (await c.post(endpoint,json={**body,'payValue':10001})).status_code==422
                    assert (await c.post(endpoint,json={**body,'type':1})).status_code==422
                    assert not seen
                    result=await c.post(endpoint,json={**body,'userId':999,'id':1,'status':4})
                    assert result.status_code==200
                    assert seen==[{**body,'subTitle':'','title':'【演示】100元券','shopId':5,'status':1}]
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old;await client.close()
    asyncio.run(run())
