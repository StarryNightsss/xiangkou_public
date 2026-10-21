import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


def test_older_same_session_result_is_discarded_after_newer_request():
    async def run():
        blocked = False
        old_started = asyncio.Event()
        release_old = asyncio.Event()

        async def dianping(request):
            if request.url.path == "/shop-type/list":
                data = [{"id": 1, "name": "美食"}]
            elif request.url.path == "/shop/search":
                if blocked and "maxPrice" not in request.url.params:
                    old_started.set()
                    await release_old.wait()
                data = [{"id": 1, "name": "测试餐厅", "typeId": 1,
                         "avgPrice": 80, "score": 48}]
                return httpx.Response(200, json={"success": True, "data": data, "total": 1})
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={"success": True, "data": data})

        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            initial = await workflow.run(ChatRequest(message="找美食"))
            assert initial.status == "ok"
            blocked = True
            old = asyncio.create_task(workflow.run(ChatRequest(
                message="找美食", session_id=initial.session_id)))
            await asyncio.wait_for(old_started.wait(), 1)
            newer = await workflow.run(ChatRequest(
                message="找人均100以内的美食", session_id=initial.session_id))
            assert newer.status == "ok"
            release_old.set()
            stale = await old
            assert stale.status == "session_conflict"
            assert not stale.shops and not stale.itineraries and stale.plan is None
            assert stale.events[-1]["event"] == "run_end"
            assert stale.events[-1]["status"] == "session_conflict"
            assert (await workflow.runs.get(stale.run_id))["status"] == "session_conflict"
            current = await workflow.sessions.load(initial.session_id)
            assert current.plan.max_price == 100
        finally:
            await client.close()

    asyncio.run(run())
