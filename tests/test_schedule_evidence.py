import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner, rule_plan
from xiangkou.schedule import open_for_visit, requested_time
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow, _coupon_face_saving, _shop_blogs


def test_time_parser_and_overnight_opening():
    assert requested_time("今晚7点去吃饭") == "19:00"
    assert requested_time("19:30到店") == "19:30"
    assert requested_time("两个人吃饭") is None
    assert open_for_visit("11:30-03:00", 23 * 60, 120) is True
    assert open_for_visit("11:30-03:00", 60, 90) is True
    assert open_for_visit("10:00-13:50,17:00-20:50", 14 * 60, 90) is False
    assert open_for_visit("未填写", 19 * 60, 90) is None


def test_itinerary_rejects_closed_first_stop_and_shows_visit_times():
    def dianping(request):
        if request.url.path == "/shop-type/list":
            data = [{"id": 1, "name": "美食"}, {"id": 2, "name": "KTV"}]
        elif request.url.path == "/shop/areas":
            data = []
        elif request.url.path == "/shop/search":
            if request.url.params["typeId"] == "1":
                data = [
                    {"id": 1, "name": "早关门火锅", "typeId": 1, "avgPrice": 80, "score": 50,
                     "x": 120.1, "y": 30.3, "openHours": "11:00-20:00"},
                    {"id": 2, "name": "晚间火锅", "typeId": 1, "avgPrice": 80, "score": 45,
                     "x": 120.11, "y": 30.31, "openHours": "11:00-23:00"},
                ]
            else:
                data = [{"id": 3, "name": "晚间KTV", "typeId": 2, "avgPrice": 70, "score": 45,
                         "x": 120.12, "y": 30.32, "openHours": "18:00-02:00"}]
            return httpx.Response(200, json={"success": True, "data": data, "total": len(data)})
        elif request.url.path == "/shop/batch":
            ids = {int(value) for value in request.url.params["ids"].split(",")}
            data = [shop for shop in [
                {"id": 1, "name": "早关门火锅", "typeId": 1, "avgPrice": 80, "score": 50,
                 "x": 120.1, "y": 30.3, "openHours": "11:00-20:00"},
                {"id": 2, "name": "晚间火锅", "typeId": 1, "avgPrice": 80, "score": 45,
                 "x": 120.11, "y": 30.31, "openHours": "11:00-23:00"},
                {"id": 3, "name": "晚间KTV", "typeId": 2, "avgPrice": 70, "score": 45,
                 "x": 120.12, "y": 30.32, "openHours": "18:00-02:00"},
            ] if shop["id"] in ids]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            message = "两人今晚7点先吃火锅再唱歌，总预算400元"
            assert rule_plan(message).visit_time == "19:00"
            response = await DiscoveryWorkflow(client, Planner(), "http://test").run(ChatRequest(message=message))
            assert response.status == "ok"
            assert [shop.id for shop in response.shops] == [2, 3]
            assert response.itineraries[0].visit_times == ["19:00", "21:00"]
            assert "营业时间" in response.answer
            assert any(item["event"] == "itinerary_validation" and item["time_rejected"] == 1
                       for item in response.events)
        finally:
            await client.close()
    asyncio.run(run())


def test_mismatched_blog_is_never_used_as_shop_evidence():
    shop = {"id": 4, "name": "Mamala(杭州远洋乐堤港店)"}
    notes = [
        {"id": 4, "shopId": 4, "title": "小筑里花园餐厅", "content": "适合约会"},
        {"id": 100, "shopId": 4, "title": "【演示笔记】Mamala", "content": "演示数据，非真实用户评价。"},
        {"id": 101, "shopId": 5, "title": "Mamala", "content": "关联了错误的店铺"},
    ]
    assert [note["id"] for note in _shop_blogs(shop, notes)] == [100]


def test_go_api_blog_search_adapter_uses_shop_and_keyword():
    def dianping(request):
        assert request.url.path == "/blog/search"
        assert request.url.params["shopId"] == "4"
        assert request.url.params["keyword"] == "约会"
        return httpx.Response(200, json={"success": True, "data": [{"id": 100}], "total": 1})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            assert await client.search_blogs(4, "约会") == [{"id": 100}]
        finally:
            await client.close()
    asyncio.run(run())


def test_coupon_face_difference_is_not_treated_as_final_price():
    assert _coupon_face_saving([{"payValue": 4750, "actualValue": 5000}]) == 2.5
    assert _coupon_face_saving([{"payValue": 5000, "actualValue": 5000}]) is None


def test_preference_evidence_moves_supported_shop_ahead_of_higher_score():
    def dianping(request):
        if request.url.path == "/shop-type/list":
            data = [{"id": 1, "name": "美食"}]
        elif request.url.path == "/shop/search":
            data = [
                {"id": 1, "name": "高分餐厅", "typeId": 1, "avgPrice": 90, "score": 49},
                {"id": 2, "name": "约会餐厅", "typeId": 1, "avgPrice": 95, "score": 45},
            ]
            return httpx.Response(200, json={"success": True, "data": data, "total": 2})
        elif request.url.path == "/blog/search":
            assert request.url.params["keyword"] == "约会"
            data = ([{"id": 10, "shopId": 2, "title": "【演示笔记】约会餐厅",
                     "content": "约会场景参考"}]
                    if request.url.params["shopId"] == "2" else [])
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    async def run():
        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        try:
            response = await DiscoveryWorkflow(client, Planner(), "http://test").run(
                ChatRequest(message="推荐适合约会的美食"))
            assert response.status == "ok"
            assert [shop.id for shop in response.shops] == [2, 1]
            assert response.shops[0].evidence[-1]["field"] == "blog"
            assert "主观描述" in response.answer
            assert any(item["event"] == "evidence_rerank" for item in response.events)
        finally:
            await client.close()
    asyncio.run(run())
