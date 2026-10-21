import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner, rule_plan
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


SHOPS = {
    1: [
        {"id": 1, "name": "近处火锅", "typeId": 1, "area": "运河上街", "avgPrice": 80, "score": 45, "x": 120.1000, "y": 30.3000},
        {"id": 2, "name": "平价火锅", "typeId": 1, "area": "运河上街", "avgPrice": 60, "score": 43, "x": 120.1100, "y": 30.3100},
    ],
    2: [
        {"id": 11, "name": "近处KTV", "typeId": 2, "area": "运河上街", "avgPrice": 70, "score": 48, "x": 120.1005, "y": 30.3005},
        {"id": 12, "name": "另一家KTV", "typeId": 2, "area": "运河上街", "avgPrice": 110, "score": 42, "x": 120.1500, "y": 30.3200},
        {"id": 13, "name": "平价KTV", "typeId": 2, "area": "运河上街", "avgPrice": 60, "score": 40, "x": 120.1400, "y": 30.3200},
    ],
    3: [
        {"id": 21, "name": "近处酒吧", "typeId": 3, "area": "运河上街", "avgPrice": 90, "score": 46, "x": 120.1010, "y": 30.3010},
        {"id": 22, "name": "平价酒吧", "typeId": 3, "area": "运河上街", "avgPrice": 50, "score": 40, "x": 120.1300, "y": 30.3100},
    ],
}


def fake_dianping(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/shop-type/list":
        data = [{"id": 1, "name": "美食"}, {"id": 2, "name": "KTV"}, {"id": 3, "name": "酒吧"}]
    elif path == "/shop/areas":
        data = ["运河上街", "大关"]
    elif path == "/shop/search":
        params = request.url.params
        data = [shop for shop in SHOPS[int(params["typeId"])]
                if (not params.get("area") or params["area"] == shop["area"])
                and (not params.get("keyword") or params["keyword"] in shop["name"])]
        return httpx.Response(200, json={"success": True, "data": data, "total": len(data)})
    elif path == "/shop/batch":
        ids = {int(value) for value in request.url.params["ids"].split(",")}
        data = [shop for group in SHOPS.values() for shop in group if shop["id"] in ids]
    else:
        return httpx.Response(404)
    return httpx.Response(200, json={"success": True, "data": data})


def test_two_stop_plan_extracts_people_order_and_total_budget():
    plan = rule_plan("两个人在运河上街先吃火锅再唱歌，总预算400元，尽量近")
    assert [(stop.category, stop.keyword) for stop in plan.stops] == [("美食", "火锅"), ("KTV", None)]
    assert plan.party_size == 2
    assert plan.total_budget == 400
    assert plan.max_price is None


def test_three_stop_plan_and_targeted_replacement():
    async def run():
        plan = rule_plan("两人先吃火锅再唱歌再喝酒，总预算600元")
        assert [stop.category for stop in plan.stops] == ["美食", "KTV", "酒吧"]
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            response = await workflow.run(
                ChatRequest(message="两人先吃火锅再唱歌再喝酒，总预算600元"))
            assert response.status == "ok"
            assert len(response.shops) == 3
            assert response.itineraries[0].estimated_total <= 600
            assert len(response.itineraries[0].leg_distances_m) == 2
            changed = await workflow.run(ChatRequest(message="第三家太贵，换便宜点的", session_id=response.session_id))
            assert changed.status == "ok"
            assert [shop.id for shop in changed.shops[:2]] == [shop.id for shop in response.shops[:2]]
            assert changed.shops[2].avg_price < response.shops[2].avg_price
        finally:
            await client.close()
    asyncio.run(run())


def test_four_stop_request_is_rejected_before_tool_calls():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            response = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="两人先吃火锅再唱歌再喝酒最后吃饭，总预算1000元"))
            assert response.status == "unsupported_request"
            assert len(response.plan.stops) == 4
            assert not response.trace
        finally:
            await client.close()
    asyncio.run(run())


def test_batch_detail_rechecks_every_candidate_before_itinerary_ranking():
    first_stop = [
        {"id": i, "name": f"火锅{i}", "typeId": 1, "avgPrice": 80, "score": 48,
         "x": 120.1 + i * 0.01, "y": 30.3} for i in range(1, 5)
    ]
    second_stop = {"id": 11, "name": "KTV", "typeId": 2, "avgPrice": 70,
                   "score": 48, "x": 120.14, "y": 30.3}

    def dianping(request):
        if request.url.path == "/shop-type/list":
            data = [{"id": 1, "name": "美食"}, {"id": 2, "name": "KTV"}]
        elif request.url.path == "/shop/areas":
            data = []
        elif request.url.path == "/shop/search":
            data = first_stop if request.url.params["typeId"] == "1" else [second_stop]
            return httpx.Response(200, json={"success": True, "data": data, "total": len(data)})
        elif request.url.path == "/shop/batch":
            ids = {int(value) for value in request.url.params["ids"].split(",")}
            data = [{**shop, "avgPrice": (None if shop["id"] == 3 else
                                           500 if shop["id"] == 4 else shop["avgPrice"])}
                    for shop in first_stop + [second_stop] if shop["id"] in ids]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            response = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="两人先吃火锅再唱歌，总预算400元，尽量近"))
            assert response.status == "ok"
            assert all(shop.id not in (3, 4) for itinerary in response.itineraries for shop in itinerary.stops)
            assert any(event["event"] == "candidate_detail_batch" and event["requested"] == 5
                       for event in response.events)
            assert all(itinerary.estimated_total <= 400 for itinerary in response.itineraries)
            assert all(evidence["source"] == "/shop/batch" for evidence in response.shops[0].evidence
                       if evidence["field"] in {"avgPrice", "score", "x", "y"})
        finally:
            await client.close()
    asyncio.run(run())


def test_two_stop_workflow_and_followup_replace_second_shop():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            first = await workflow.run(ChatRequest(message="两个人在运河上街先吃火锅再唱歌，总预算400元，尽量近"))
            assert first.status == "ok"
            assert first.plan.area == "运河上街"
            assert [shop.id for shop in first.shops] == [1, 11]
            assert first.itineraries[0].estimated_total == 300
            assert first.itineraries[0].straight_distance_m < 100
            assert all(item.estimated_total <= 400 for item in first.itineraries)
            assert all(shop.evidence for shop in first.shops)
            assert first.session_id

            changed = await workflow.run(ChatRequest(message="第二家太贵，换便宜点的", session_id=first.session_id))
            assert changed.status == "ok"
            assert [shop.id for shop in changed.shops] == [1, 13]
            assert changed.itineraries[0].estimated_total == 280
            assert changed.plan.excluded_shop_ids == [11]
            assert changed.plan.fixed_first_shop_id == 1
            assert changed.plan.second_price_ceiling == 70
            assert "总预算：¥400" in changed.confirmed_constraints
        finally:
            await client.close()
    asyncio.run(run())


def test_replacement_does_not_claim_a_more_expensive_shop_is_cheaper():
    async def run():
        def no_cheaper(request: httpx.Request) -> httpx.Response:
            response = fake_dianping(request)
            if request.url.path == "/shop/search" and request.url.params.get("typeId") == "2":
                body = response.json()
                body["data"] = [shop for shop in body["data"] if shop["id"] != 13]
                return httpx.Response(200, json=body)
            return response

        client = DianpingClient("http://test", transport=httpx.MockTransport(no_cheaper))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            first = await workflow.run(ChatRequest(message="两个人在运河上街先吃火锅再唱歌，总预算400元，尽量近"))
            changed = await workflow.run(ChatRequest(message="第二家太贵，换便宜点的", session_id=first.session_id))
            assert changed.status == "no_results"
            assert "更便宜的替代店铺" in changed.answer
            assert changed.itineraries == []
        finally:
            await client.close()
    asyncio.run(run())


def test_total_budget_asks_for_people_then_continues():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            first = await workflow.run(ChatRequest(message="先吃火锅再唱歌，总预算400元"))
            assert first.status == "needs_clarification"
            assert first.trace == []
            second = await workflow.run(ChatRequest(message="2人", session_id=first.session_id))
            assert second.status == "ok"
            assert second.plan.party_size == 2
            assert second.itineraries
        finally:
            await client.close()
    asyncio.run(run())


def test_budget_conflict_has_reason_and_no_invalid_plan():
    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(fake_dianping))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            result = await workflow.run(
                ChatRequest(message="两人先吃火锅再唱歌，总预算100元")
            )
            assert result.status == "no_results"
            assert result.itineraries == []
            assert "总预算" in result.answer
            assert result.relaxations and result.relaxations[0].label == "将总预算提高到 ¥240"
            changed = await workflow.run(
                ChatRequest(message=result.relaxations[0].message, session_id=result.session_id))
            assert changed.status == "ok"
            assert changed.plan.total_budget == 240
            assert changed.itineraries[0].estimated_total == 240
        finally:
            await client.close()
    asyncio.run(run())
