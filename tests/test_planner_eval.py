import asyncio
import json
import httpx

from xiangkou.llm import LLMConfig, LLMProvider
from xiangkou.harness import AgentHarness
from xiangkou.planner import Planner
from scripts.eval_planner import evaluate


def test_rule_planner_examples():
    total, passed, failures = evaluate()
    assert passed == total, "\n".join(failures)


def test_model_plan_keeps_only_supported_search_terms():
    model_output = {
        "category": "餐厅", "keyword": "火锅店", "area": "编造地点", "max_price": 120,
        "min_score": 4.5, "wants_coupon": False, "nearby": False, "sort": "score",
    }

    def model(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(model_output)}]})

    async def run():
        provider = LLMProvider(LLMConfig(provider="anthropic", api_key="test", model="test-model",
                                         base_url="https://example.test"), httpx.MockTransport(model))
        plan, mode = await Planner(provider=provider).plan("想找安静的火锅店，预算120，评分4.5")
        assert mode == "model"
        assert plan.category == "美食"
        assert plan.keyword == "火锅"
        assert plan.area is None
        assert plan.unverified_preferences == ["安静"]

    asyncio.run(run())


def test_model_cannot_set_internal_session_fields_and_trace_shows_sources():
    output = {"category": "美食", "keyword": "火锅", "max_price": 999,
              "excluded_shop_ids": [1, 2], "fixed_first_shop_id": 1,
              "second_price_ceiling": 1}

    def model(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(output)}]})

    async def run():
        provider = LLMProvider(LLMConfig(provider="anthropic", api_key="test", model="test-model",
                                         base_url="https://example.test"), httpx.MockTransport(model))
        harness = AgentHarness({})
        plan, mode = await Planner(provider=provider).plan("找人均120以内的火锅", harness=harness)
        assert mode == "model"
        assert plan.max_price == 120
        assert plan.excluded_shop_ids == []
        assert plan.fixed_first_shop_id is None and plan.second_price_ceiling is None
        event = next(item for item in harness.events if item["event"] == "planner_model")
        assert event["field_sources"]["max_price"] == "rules"
        assert "excluded_shop_ids" in event["ignored_fields"]
    asyncio.run(run())


def test_model_relative_itinerary_distance_does_not_require_user_location():
    def model(_request):
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': json.dumps(
            {'category': '美食', 'nearby': True, 'sort': 'score'})}]})
    async def run():
        provider = LLMProvider(LLMConfig(provider='anthropic', api_key='test', model='test-model',
                                       base_url='https://example.test'), httpx.MockTransport(model))
        plan, mode = await Planner(provider=provider).plan('两个人先吃火锅再唱歌，总预算400元，尽量近')
        assert mode == 'model' and not plan.nearby and plan.sort == 'score'
        assert len(plan.stops) == 2
    asyncio.run(run())
