import asyncio

import httpx

from xiangkou.api import app
from xiangkou.dianping import DianpingClient
from xiangkou.schemas import ChatResponse


def test_stream_emits_progress_and_result_with_authenticated_owner():
    async def run():
        def backend(request):
            if request.url.path == "/user/me" and request.headers.get("authorization") == "token-a":
                return httpx.Response(200, json={"success": True, "data": {"id": 7}})
            return httpx.Response(401)
        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))

        class Workflow:
            def __init__(self):
                self.client = client

            async def run(self, body, owner_user_id=None, on_event=None):
                assert owner_user_id == 7
                on_event({"event": "tool_start", "run_id": "run-1", "elapsed_ms": 1})
                return ChatResponse(answer="完成", status="ok", mode="rules", run_id="run-1")

        old_workflow = getattr(app.state, "workflow", None)
        old_active = getattr(app.state, "active_requests", None)
        app.state.workflow = Workflow()
        app.state.active_requests = {}
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                         headers={"authorization": "token-a"}) as owner:
                response = await owner.post("/api/chat/stream", json={
                    "message": "找美食", "request_id": "00000000-0000-0000-0000-000000000001"})
                assert response.status_code == 200
                assert '"type": "event"' in response.text
                assert '"type": "result"' in response.text
                assert '"answer": "完成"' in response.text
            assert not app.state.active_requests
        finally:
            app.state.workflow = old_workflow
            app.state.active_requests = old_active
            await client.close()
    asyncio.run(run())


def test_only_owner_can_cancel_running_stream():
    async def run():
        started = asyncio.Event()
        def backend(request):
            token = request.headers.get("authorization")
            if request.url.path == "/user/me" and token in ("token-a", "token-b"):
                return httpx.Response(200, json={"success": True, "data": {
                    "id": 7 if token == "token-a" else 8}})
            return httpx.Response(401)
        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))

        class Workflow:
            def __init__(self):
                self.client = client

            async def run(self, body, owner_user_id=None, on_event=None):
                started.set()
                await asyncio.sleep(60)

        old_workflow = getattr(app.state, "workflow", None)
        old_active = getattr(app.state, "active_requests", None)
        app.state.workflow = Workflow()
        app.state.active_requests = {}
        request_id = "00000000-0000-0000-0000-000000000002"
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                         headers={"authorization": "token-a"}) as owner:
                async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                             headers={"authorization": "token-b"}) as other:
                    pending = asyncio.create_task(owner.post("/api/chat/stream", json={
                        "message": "找美食", "request_id": request_id}))
                    await asyncio.wait_for(started.wait(), 2)
                    assert (await other.delete(f"/api/requests/{request_id}")).status_code == 404
                    assert (await owner.delete(f"/api/requests/{request_id}")).status_code == 200
                    result = await asyncio.wait_for(pending, 2)
                    assert '"type": "cancelled"' in result.text
            assert not app.state.active_requests
        finally:
            app.state.workflow = old_workflow
            app.state.active_requests = old_active
            await client.close()
    asyncio.run(run())


def test_full_progress_queue_does_not_block_disconnect_cleanup(monkeypatch):
    from xiangkou import api
    from xiangkou.schemas import ChatRequest

    async def run():
        ready, refill, filled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stopped = asyncio.Event()
        async def fake_chat(body, user_id, on_event=None, token=''):
            try:
                for number in range(101):
                    on_event({'event': 'progress', 'number': number})
                ready.set()
                await refill.wait()
                on_event({'event': 'refill'})
                filled.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        monkeypatch.setattr(api, '_run_chat', fake_chat)
        monkeypatch.setattr(app.state, 'active_requests', {}, raising=False)
        class Request:
            cookies, headers = {}, {}
        response = await api.chat_stream(ChatRequest(message='test', request_id='11111111-1111-1111-1111-111111111111'), Request(), {'id': 7})
        await asyncio.wait_for(ready.wait(), 1)
        await response.body_iterator.__anext__()
        refill.set()
        await asyncio.wait_for(filled.wait(), 1)
        await asyncio.wait_for(response.body_iterator.aclose(), 1)
        assert stopped.is_set() and not app.state.active_requests
    asyncio.run(run())


def test_terminal_result_survives_full_queue_and_reports_dropped_events(monkeypatch):
    import json
    from xiangkou import api
    from xiangkou.schemas import ChatRequest

    async def run():
        async def fake_chat(body, user_id, on_event=None, token=''):
            for number in range(105):
                on_event({'event': 'progress', 'number': number})
            return ChatResponse(answer='done', status='ok', mode='rules', run_id='test')
        monkeypatch.setattr(api, '_run_chat', fake_chat)
        monkeypatch.setattr(app.state, 'active_requests', {}, raising=False)
        class Request:
            cookies, headers = {}, {}
        response = await api.chat_stream(ChatRequest(message='test', request_id='22222222-2222-2222-2222-222222222222'), Request(), {'id': 7})
        async def collect():
            return [json.loads(item.removeprefix('data: ')) async for item in response.body_iterator]
        items = await asyncio.wait_for(collect(), 1)
        assert len([item for item in items if item['type']=='event']) == 100
        assert items[-1]['type']=='result' and items[-1]['dropped_events']==5
        assert not app.state.active_requests
    asyncio.run(run())


def test_cancellation_before_worker_start_still_terminates_stream(monkeypatch):
    from xiangkou import api
    from xiangkou.schemas import ChatRequest

    async def run():
        async def fake_chat(*args, **kwargs):
            raise AssertionError('cancelled task must not start')
        monkeypatch.setattr(api, '_run_chat', fake_chat)
        monkeypatch.setattr(app.state, 'active_requests', {}, raising=False)
        class Request:
            cookies, headers = {}, {}
        request_id = '33333333-3333-3333-3333-333333333333'
        response = await api.chat_stream(ChatRequest(message='test', request_id=request_id), Request(), {'id': 7})
        app.state.active_requests[request_id][1].cancel()
        async def collect():
            return [item async for item in response.body_iterator]
        items = await asyncio.wait_for(collect(), 1)
        assert '"type": "cancelled"' in items[-1]
        assert not app.state.active_requests
    asyncio.run(run())
