import asyncio
from types import SimpleNamespace
import httpx
from xiangkou.api import app,require_user


def test_payment_requires_confirmation_valid_amount_and_same_origin():
    async def run():
        calls=[]
        class Client:
            async def pay_mock_order(self,order,amount,token):calls.append((order,amount,token))
            async def get_voucher_order(self,order,token):return {'order_id':order,'status':2,'pay_cents':8900}
        old=getattr(app.state,'workflow',None)
        app.state.workflow=SimpleNamespace(client=Client())
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',headers={'authorization':'owner'}) as c:
                endpoint='/api/my/orders/88/pay/mock'
                assert (await c.post(endpoint,json={'expected_pay_cents':8900,'confirmed':False})).status_code==422
                assert (await c.post(endpoint,json={'expected_pay_cents':0,'confirmed':True})).status_code==422
                assert (await c.post(endpoint,json={'expected_pay_cents':8900,'confirmed':True},headers={'Origin':'https://evil.example'})).status_code==403
                assert not calls
                r=await c.post(endpoint,json={'expected_pay_cents':8900,'confirmed':True})
                assert r.status_code==200 and r.json()['status']==2 and r.json()['payment_mode']=='mock'
                assert calls==[('88',8900,'owner')]
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old
    asyncio.run(run())


def test_payment_adapter_posts_go_api_form_with_owner_token():
    async def run():
        from xiangkou.dianping import DianpingClient
        def backend(request):
            assert request.method=='POST' and request.url.path=='/voucher-order/88/pay/mock'
            assert request.content==b'expectedPayCents=8900' and request.headers['authorization']=='owner'
            return httpx.Response(200,json={'success':True,'data':'88'})
        c=DianpingClient('http://test',transport=httpx.MockTransport(backend))
        try:await c.pay_mock_order('88',8900,'owner')
        finally:await c.close()
    asyncio.run(run())
