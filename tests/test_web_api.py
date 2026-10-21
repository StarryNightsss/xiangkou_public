import asyncio
from types import SimpleNamespace
import httpx
from xiangkou.api import app, require_user
from xiangkou.dianping import DianpingClient


def test_frontend_bff_forwards_cookie_without_exposing_token_and_rejects_csrf():
    async def run():
        seen=[]
        def backend(request):
            seen.append((request.method,request.url.path))
            assert request.headers['authorization']=='owner-token'
            return httpx.Response(200,json={'success':True,'data':[],'total':0})
        client=DianpingClient('http://go-api',transport=httpx.MockTransport(backend))
        old=getattr(app.state,'workflow',None)
        app.state.workflow=SimpleNamespace(client=client)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',cookies={'xiangkou_dp_token':'owner-token'}) as c:
                result=await c.get('/api/catalog/shops?keyword=火锅')
                assert result.status_code==200 and 'owner-token' not in result.text
                assert seen==[('GET','/shop/catalog')]
                assert (await c.put('/api/community/blogs/1/like',headers={'Origin':'http://evil.example'})).status_code==403
                assert len(seen)==1
                assert (await c.get('/api/community/blogs?feed=admin')).status_code==422
                assert (await c.post('/api/community/blogs',json={'shopId':5,'title':'标题','content':'正文','images':'../../x'})).status_code==422
                assert (await c.post('/api/community/images',json={'data':'PHN2Zz4='})).status_code==422
                assert len(seen)==1
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old;await client.close()
    asyncio.run(run())


def test_unified_business_pages_require_login():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou') as c:
            for path in ['/api/catalog/types','/api/catalog/shops','/api/community/blogs']:
                assert (await c.get(path)).status_code==401
    asyncio.run(run())


def test_publish_uses_only_validated_fields_and_current_login():
    async def run():
        seen=[]
        def backend(request):
            assert request.headers['authorization']=='owner'
            if request.method=='GET':
                return httpx.Response(200,json={'success':True,'data':{'id':5}})
            import json
            fields=json.loads(request.content)
            assert set(fields)=={'shopId','title','content','images'}
            assert fields['shopId']==5
            seen.append(fields)
            return httpx.Response(200,json={'success':True,'data':42})
        client=DianpingClient('http://go-api',transport=httpx.MockTransport(backend))
        old=getattr(app.state,'workflow',None);app.state.workflow=SimpleNamespace(client=client)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',headers={'authorization':'owner'}) as c:
                r=await c.post('/api/community/blogs',json={'shopId':5,'title':'测试笔记','content':'测试正文','images':'/blogs/1/2/1234-abcd.png','userId':999})
                assert r.status_code==200 and r.json()['data']==42 and len(seen)==1
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old;await client.close()
    asyncio.run(run())


def test_user_profile_aggregates_basic_info_follow_state_and_blogs():
    async def run():
        seen=[]
        def backend(request):
            path=request.url.path
            seen.append((request.method,path,dict(request.url.params)))
            payload={
                '/user/12':{'success':True,'data':{'id':12,'nickName':'小林','icon':'/icons/12.png'}},
                '/user/info/12':{'success':True,'data':{'userId':12,'city':'深圳','introduce':'爱探店'}},
                '/follow/or/not/12':{'success':True,'data':True},
                '/blog/of/user':{'success':True,'data':[{'id':101,'title':'第一篇笔记'}]},
            }[path]
            return httpx.Response(200,json=payload)
        client=DianpingClient('http://go-api',transport=httpx.MockTransport(backend))
        old=getattr(app.state,'workflow',None);app.state.workflow=SimpleNamespace(client=client)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',headers={'authorization':'owner'}) as c:
                r=await c.get('/api/community/users/12')
                assert r.status_code==200, r.text
                body=r.json()
                assert body['user']['id']==12 and body['user']['nickName']=='小林'
                assert body['profile']['city']=='深圳'
                assert body['followed'] is True and body['is_self'] is False
                assert [item['id'] for item in body['blogs']]==[101]
                assert ('GET','/blog/of/user',{'id':'12','current':'1'}) in seen
                # 分页接口应把页码透传成 current
                seen.clear()
                page=await c.get('/api/community/users/12/blogs?page=2')
                assert page.status_code==200 and page.json()['data'][0]['id']==101
                assert ('GET','/blog/of/user',{'id':'12','current':'2'}) in seen
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old;await client.close()
    asyncio.run(run())


def test_user_profile_skips_follow_lookup_for_self_and_rejects_bad_ids():
    async def run():
        seen=[]
        def backend(request):
            path=request.url.path
            seen.append(path)
            if path=='/user/7':
                return httpx.Response(200,json={'success':True,'data':{'id':7,'nickName':'我'}})
            if path=='/user/info/7':
                return httpx.Response(200,json={'success':True,'data':None})
            if path=='/blog/of/user':
                return httpx.Response(200,json={'success':True,'data':[]})
            if path=='/user/99':
                return httpx.Response(200,json={'success':True,'data':None})
            return httpx.Response(404)
        client=DianpingClient('http://go-api',transport=httpx.MockTransport(backend))
        old=getattr(app.state,'workflow',None);app.state.workflow=SimpleNamespace(client=client)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://xiangkou',headers={'authorization':'owner'}) as c:
                mine=await c.get('/api/community/users/7')
                assert mine.status_code==200 and mine.json()['is_self'] is True
                assert mine.json()['followed'] is False and mine.json()['profile'] is None
                assert not any(path.startswith('/follow/') for path in seen)
                assert (await c.get('/api/community/users/0')).status_code==422
                missing=await c.get('/api/community/users/99')
                assert missing.status_code==404 and '用户不存在' in missing.json()['detail']
        finally:
            app.dependency_overrides.pop(require_user,None);app.state.workflow=old;await client.close()
    asyncio.run(run())


def test_sign_endpoints_forward_to_go_api_and_require_origin_for_writes():
    async def run():
        seen = []
        def backend(request):
            seen.append((request.method, request.url.path))
            if request.url.path == '/user/sign/count':
                return httpx.Response(200, json={'success': True, 'data': 3})
            return httpx.Response(200, json={'success': True})
        client = DianpingClient('http://go-api', transport=httpx.MockTransport(backend))
        old = getattr(app.state, 'workflow', None)
        app.state.workflow = SimpleNamespace(client=client)
        app.dependency_overrides[require_user] = lambda: {'id': 7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://xiangkou',
                                         headers={'authorization': 'owner'}) as c:
                count = await c.get('/api/community/sign/count')
                assert count.status_code == 200 and count.json()['data'] == 3
                # 写操作必须校验来源
                assert (await c.post('/api/community/sign', headers={'Origin': 'http://evil.example'})).status_code == 403
                ok = await c.post('/api/community/sign')
                assert ok.status_code == 200
                assert seen == [('GET', '/user/sign/count'), ('POST', '/user/sign')]
        finally:
            app.dependency_overrides.pop(require_user, None)
            app.state.workflow = old
            await client.close()
    asyncio.run(run())
