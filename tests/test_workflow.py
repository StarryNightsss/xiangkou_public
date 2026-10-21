import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.harness import AgentHarness, HarnessError
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


def fake_dianping(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/shop-type/list":
        data = [{"id": 1, "name": "美食"}, {"id": 4, "name": "健身运动"}]
    elif path == "/shop/search":
        shops = [
            {"id": 1, "name": "好味火锅", "area": "运河上街", "address": "运河上街 1 号", "avgPrice": 80, "score": 47, "distance": 500, "images": ""},
            {"id": 2, "name": "贵价火锅", "area": "运河上街", "address": "运河上街 2 号", "avgPrice": 200, "score": 49, "images": ""},
            {"id": 3, "name": "平价面馆", "area": "运河上街", "address": "运河上街 3 号", "avgPrice": 30, "score": 46, "images": ""},
        ]
        params = request.url.params
        data = [shop for shop in shops
                if (not params.get("keyword") or params["keyword"] in shop["name"])
                and (not params.get("area") or params["area"] in shop["area"] + shop["address"])
                and (not params.get("maxPrice") or shop["avgPrice"] <= int(params["maxPrice"]))
                and (not params.get("minScore") or shop["score"] >= int(params["minScore"]))
                and (params.get("hasVoucher") != "true" or shop["id"] == 1)]
        assert params["typeId"] == "1"
        assert params["page"] == "1"
        assert int(params["size"]) <= 20
        return httpx.Response(200, json={"success": True, "data": data, "total": len(data)})
    elif path == "/voucher/list/1":
        data = [{"id": 99, "title": "代金券", "payValue": 8000, "actualValue": 10000}]
    else:
        return httpx.Response(404)
    return httpx.Response(200, json={"success": True, "data": data})


def test_rule_plan_extracts_constraints():
    plan = rule_plan("想找人均100以内、4.5分以上的火锅店，有优惠券吗")
    assert plan.category == "美食"
    assert plan.keyword == "火锅"
    assert plan.max_price == 100
    assert plan.min_score == 4.5
    assert plan.wants_coupon


def test_unverifiable_preference_is_disclosed():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            result = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="想找安静、适合约会的火锅店")
            )
            assert result.status == "ok"
            assert [shop.id for shop in result.shops] == [1, 2]
            assert "无法验证安静、适合约会" in result.answer
            assert result.plan.unverified_preferences == ["安静", "适合约会"]
        finally:
            await client.close()
    asyncio.run(run())


def test_workflow_uses_real_fields_and_hard_filters():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://127.0.0.1:8080")
            result = await workflow.run(ChatRequest(message="想找人均100以内、4.5分以上的火锅店，有优惠券吗"))
            assert result.status == "ok"
            assert [shop.id for shop in result.shops] == [1]
            assert result.shops[0].url.endswith("shop-detail.html?id=1")
            assert result.shops[0].coupons[0]["id"] == 99
            assert [item["tool"] for item in result.trace] == ["list_types", "search_shops", "list_vouchers"]
        finally:
            await client.close()
    asyncio.run(run())


def test_nearby_requires_location():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            result = await DiscoveryWorkflow(client, Planner(), "http://test").run(ChatRequest(message="找附近的火锅"))
            assert result.status == "needs_location"
            assert result.trace == []
        finally:
            await client.close()
    asyncio.run(run())


def test_nearby_with_area_does_not_ask_for_location():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            result = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="找运河上街附近最近的美食")
            )
            assert result.status == "ok"
        finally:
            await client.close()
    asyncio.run(run())


def test_coupon_request_prioritizes_available_offer():
    async def run():
        requests = []
        def record(request):
            requests.append(request)
            return fake_dianping(request)
        client = DianpingClient("http://test", transport=httpx.MockTransport(record))
        try:
            result = await DiscoveryWorkflow(client, Planner(), "http://test").run(ChatRequest(message="推荐美食，有优惠券吗"))
            assert result.status == "ok"
            assert [shop.id for shop in result.shops] == [1]
            assert result.shops[0].coupons[0]["title"] == "代金券"
            assert [request.url.params.get("hasVoucher") for request in requests if request.url.path == "/shop/search"] == ["true"]
            assert len([request for request in requests if request.url.path.startswith("/voucher/list/")]) == 1
        finally:
            await client.close()
    asyncio.run(run())


def test_coupon_search_falls_back_to_shops_when_no_offer_matches():
    async def run():
        requests = []
        def no_matching_voucher(request):
            requests.append(request)
            if request.url.path == "/shop/search" and request.url.params.get("hasVoucher") == "true":
                return httpx.Response(200, json={"success": True, "data": [], "total": 0})
            return fake_dianping(request)
        client = DianpingClient("http://test", transport=httpx.MockTransport(no_matching_voucher))
        try:
            result = await DiscoveryWorkflow(client, Planner(), "http://test").run(ChatRequest(message="推荐美食，有优惠券吗"))
            assert result.status == "ok"
            assert result.shops and all(not shop.coupons for shop in result.shops)
            assert "暂未查到有可用优惠券" in result.answer
            assert [request.url.params.get("hasVoucher") for request in requests if request.url.path == "/shop/search"] == ["true", None]
            assert not any(request.url.path.startswith("/voucher/list/") for request in requests)
        finally:
            await client.close()
    asyncio.run(run())


def test_harness_denies_unknown_tool_and_budget():
    async def noop():
        return 1
    async def run():
        harness = AgentHarness({"read": noop}, max_calls=1)
        assert await harness.call("read") == 1
        for name in ("write", "read"):
            try:
                await harness.call(name)
            except HarnessError:
                pass
            else:
                assert False, "expected HarnessError"
    asyncio.run(run())
