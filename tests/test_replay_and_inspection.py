import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.harness import AgentHarness
from xiangkou.planner import Planner
from xiangkou.runtime import AgentRuntime
from xiangkou.tool_args import ShopIdArgs
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


def test_missing_search_price_is_checked_against_shop_detail_and_replayed():
    def dianping(request):
        if request.url.path == "/shop-type/list":
            data = [{"id": 1, "name": "美食"}]
        elif request.url.path == "/shop/search":
            data = [{"id": 1, "name": "价格待核对餐厅", "typeId": 1,
                     "area": "运河上街", "avgPrice": None, "score": 48}]
            return httpx.Response(200, json={"success": True, "data": data, "total": 1})
        elif request.url.path == "/shop/1":
            data = {"id": 1, "name": "价格待核对餐厅", "typeId": 1,
                    "area": "运河上街", "avgPrice": 180, "score": 48}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            response = await workflow.run(ChatRequest(message="找人均100以内的美食"))
            assert response.status == "no_results"
            assert response.shops == []
            assert [item["tool"] for item in response.trace] == ["list_types", "search_shops", "get_shop"]
            assert [item["event"] for item in response.events][0] == "run_start"
            assert [item["event"] for item in response.events][-1] == "run_end"
            assert any(item["event"] == "candidate_validation" and item["after"] == 0
                       for item in response.events)
            saved = await workflow.runs.get(response.run_id)
            assert saved["run_id"] == response.run_id
            assert saved["status"] == "no_results"
            assert saved["events"] == response.events
            assert "找人均100以内的美食" not in str(saved)
        finally:
            await client.close()
    asyncio.run(run())


def test_model_inspects_candidate_and_rejects_outside_id():
    async def run():
        calls = []

        async def get_shop(shop_id):
            calls.append(shop_id)
            return {"id": shop_id, "avgPrice": 80, "score": 47}

        turns = 0

        def model(_request):
            nonlocal turns
            turns += 1
            content = ([
                {"type": "tool_use", "id": "bad", "name": "get_shop", "input": {"shop_id": 999}},
                {"type": "tool_use", "id": "good", "name": "get_shop", "input": {"shop_id": 1}},
            ] if turns == 1 else [{"type": "text", "text": "完成"}])
            return httpx.Response(200, json={"content": content})

        harness = AgentHarness({"get_shop": get_shop}, validators={"get_shop": ShopIdArgs})
        runtime = AgentRuntime(Planner(api_key="test", base_url="https://model.test"), harness,
                               model_transport=httpx.MockTransport(model))
        result = await runtime.inspect([{"id": 1, "name": "候选店", "avgPrice": 80, "score": 47}])
        assert result[1]["avgPrice"] == 80
        assert calls == [1]
        assert any(item["event"] == "agent_rejected" for item in harness.events)
        assert any(item["event"] == "agent_decision" and item["tool"] == "get_shop"
                   for item in harness.events)
    asyncio.run(run())


def test_second_page_is_checked_when_first_page_fails_detail_validation():
    def dianping(request):
        if request.url.path == "/shop-type/list":
            data = [{"id": 1, "name": "美食"}]
        elif request.url.path == "/shop/search":
            page = int(request.url.params["page"])
            data = ([{"id": shop_id, "name": f"待核价{shop_id}", "typeId": 1,
                     "avgPrice": None, "score": 48} for shop_id in (1, 2, 3)]
                    if page == 1 else [{"id": 4, "name": "合格店", "typeId": 1,
                                        "avgPrice": 70, "score": 48}])
            return httpx.Response(200, json={"success": True, "data": data, "total": 4})
        elif request.url.path.startswith("/shop/"):
            shop_id = int(request.url.path.rsplit("/", 1)[-1])
            data = {"id": shop_id, "name": f"待核价{shop_id}", "typeId": 1,
                    "avgPrice": 180, "score": 48}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            response = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="找人均100以内的美食"))
            assert response.status == "ok"
            assert [shop.id for shop in response.shops] == [4]
            assert [item["tool"] for item in response.trace].count("search_shops") == 2
        finally:
            await client.close()
    asyncio.run(run())
