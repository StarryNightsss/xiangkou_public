import asyncio

import httpx

from xiangkou.harness import AgentHarness
from xiangkou.planner import Planner
from xiangkou.runtime import AgentRuntime, _model_observation
from xiangkou.tool_args import BlogSearchArgs, ShopIdArgs


def test_model_directs_allowed_read_tool_and_observes_result():
    async def run():
        calls = []
        async def list_vouchers(shop_id: int):
            calls.append(shop_id)
            return [{"id": 7, "title": "代金券"}]

        turns = 0
        def model(request: httpx.Request) -> httpx.Response:
            nonlocal turns
            turns += 1
            content = ([{"type": "tool_use", "id": "tool-1", "name": "list_vouchers", "input": {"shop_id": 1}}]
                       if turns == 1 else [{"type": "text", "text": "完成"}])
            return httpx.Response(200, json={"content": content})

        harness = AgentHarness({"list_vouchers": list_vouchers}, validators={"list_vouchers": ShopIdArgs})
        runtime = AgentRuntime(Planner(api_key="test", base_url="https://model.test"), harness,
                               model_transport=httpx.MockTransport(model))
        coupons, blogs, failed = await runtime.enrich([{"id": 1, "name": "店铺"}], [1], [])
        assert coupons[1][0]["id"] == 7
        assert blogs == {} and not failed
        assert calls == [1]
        assert turns == 2
        assert any(event["event"] == "agent_decision" and event["mode"] == "model" for event in harness.events)
    asyncio.run(run())


def test_model_cannot_read_shop_outside_candidate_set():
    async def run():
        calls = []
        async def list_vouchers(shop_id: int):
            calls.append(shop_id)
            return []

        def model(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "tool-x", "name": "list_vouchers", "input": {"shop_id": 999}},
            ]})

        harness = AgentHarness({"list_vouchers": list_vouchers}, validators={"list_vouchers": ShopIdArgs})
        runtime = AgentRuntime(Planner(api_key="test", base_url="https://model.test"), harness,
                               model_transport=httpx.MockTransport(model))
        await runtime.enrich([{"id": 1, "name": "店铺"}], [1], [])
        assert calls == [1]  # 无效模型动作被拒绝；规则回退只读取允许的店铺。
        assert any(event["event"] == "agent_rejected" for event in harness.events)
    asyncio.run(run())


def test_model_blog_search_uses_workflow_keyword_only():
    async def run():
        calls = []

        async def search_blogs(shop_id: int, keyword: str):
            calls.append((shop_id, keyword))
            return [{"id": 7, "shopId": shop_id, "title": "约会参考"}]

        turns = 0

        def model(_request):
            nonlocal turns
            turns += 1
            content = ([
                {"type": "tool_use", "id": "bad", "name": "search_blogs",
                 "input": {"shop_id": 1, "keyword": "任意扩大范围"}},
                {"type": "tool_use", "id": "good", "name": "search_blogs",
                 "input": {"shop_id": 1, "keyword": "约会"}},
            ] if turns == 1 else [{"type": "text", "text": "完成"}])
            return httpx.Response(200, json={"content": content})

        harness = AgentHarness({"search_blogs": search_blogs}, validators={"search_blogs": BlogSearchArgs})
        runtime = AgentRuntime(Planner(api_key="test", base_url="https://model.test"), harness,
                               model_transport=httpx.MockTransport(model))
        coupons, blogs, failed = await runtime.enrich(
            [{"id": 1, "name": "候选店"}], [], [1], evidence_query="约会")
        assert not failed and coupons == {}
        assert blogs[1][0]["id"] == 7
        assert calls == [(1, "约会")]
        assert any(item["event"] == "agent_rejected" for item in harness.events)
    asyncio.run(run())


def test_model_observation_is_bounded_and_excludes_unrelated_fields():
    import json

    records = [{"id": index, "shopId": 1, "title": "探店", "content": "安静" * 500,
                "privateToken": "must-not-enter-model"} for index in range(10)]
    observation, limited = _model_observation("search_blogs", records)
    payload = json.loads(observation)
    assert limited
    assert len(payload) == 3
    assert all(len(item["content"]) <= 200 for item in payload)
    assert "must-not-enter-model" not in observation
    assert len(observation) < 2000
