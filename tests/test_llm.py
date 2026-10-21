import asyncio
import json

import httpx
import pytest

from xiangkou.harness import AgentHarness
from xiangkou.llm import DEFAULT_BASE_URLS, LLMConfig, LLMProvider
from xiangkou.planner import Planner
from xiangkou.runtime import AgentRuntime
from xiangkou.tool_args import ShopIdArgs


@pytest.mark.parametrize("name", list(DEFAULT_BASE_URLS) + ["openai-compatible"])
def test_provider_configuration(name, monkeypatch):
    monkeypatch.setenv("XIANGKOU_LLM_PROVIDER", name)
    monkeypatch.setenv("XIANGKOU_LLM_MODEL", "demo-model")
    monkeypatch.setenv("XIANGKOU_LLM_API_KEY", "secret")
    if name == "openai-compatible":
        monkeypatch.setenv("XIANGKOU_LLM_BASE_URL", "https://custom.example/v1")
    config = LLMConfig.from_env()
    assert config.provider == name
    assert config.api_key == "secret"
    assert config.base_url


def test_model_requires_name_when_key_present(monkeypatch):
    monkeypatch.setenv("XIANGKOU_LLM_PROVIDER", "openai")
    monkeypatch.setenv("XIANGKOU_LLM_API_KEY", "secret")
    monkeypatch.delenv("XIANGKOU_LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="XIANGKOU_LLM_MODEL"):
        LLMConfig.from_env()


@pytest.mark.parametrize("name", ["openai", "gemini", "deepseek", "qwen", "kimi", "zhipu"])
def test_openai_compatible_tool_cycle(name):
    async def run():
        requests = []
        async def list_vouchers(shop_id: int):
            return [{"id": 7, "shopId": shop_id}]

        def model(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append((request, body))
            if name == "deepseek":
                assert body["thinking"] == {"type": "disabled"}
            if len(requests) == 1:
                assert body["tools"][0]["function"]["name"] == "list_vouchers"
                return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call-1", "type": "function", "function": {
                        "name": "list_vouchers", "arguments": "{\"shop_id\": 1}"}}]}}]})
            assert body["messages"][-1] == {"role": "tool", "tool_call_id": "call-1",
                                            "content": '[{"id": 7, "shopId": 1}]'}
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})

        provider = LLMProvider(LLMConfig(provider=name, model="demo-model", api_key="secret",
                                         base_url=DEFAULT_BASE_URLS[name]), httpx.MockTransport(model))
        harness = AgentHarness({"list_vouchers": list_vouchers}, validators={"list_vouchers": ShopIdArgs})
        coupons, blogs, failed = await AgentRuntime(Planner(provider=provider), harness).enrich(
            [{"id": 1, "name": "店铺"}], [1], [])
        assert coupons[1][0]["id"] == 7 and blogs == {} and not failed
        assert len(requests) == 2
        assert requests[0][0].headers["authorization"] == "Bearer secret"
        assert requests[0][0].url.path.endswith("/chat/completions")
    asyncio.run(run())


def test_invalid_model_tool_arguments_fall_back_to_allowed_shop():
    async def run():
        calls = []
        async def list_vouchers(shop_id: int):
            calls.append(shop_id)
            return []

        def model(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{
                "id": "bad", "type": "function", "function": {
                    "name": "list_vouchers", "arguments": "{bad json"}}]}}]})

        provider = LLMProvider(LLMConfig(provider="openai", model="demo", api_key="secret",
                                         base_url="https://model.test/v1"), httpx.MockTransport(model))
        harness = AgentHarness({"list_vouchers": list_vouchers}, validators={"list_vouchers": ShopIdArgs})
        result, _, _ = await AgentRuntime(Planner(provider=provider), harness).enrich(
            [{"id": 1}], [1], [])
        assert result == {1: []}
        assert calls == [1]
        assert any(item["event"] == "model_fallback" for item in harness.events)
    asyncio.run(run())


def test_openai_provider_is_used_by_planner():
    async def run():
        def model(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][1]["content"] == "想找火锅店，人均100以内"
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "category": "美食", "keyword": "火锅", "max_price": 100, "sort": "score",
            })}}]})

        provider = LLMProvider(LLMConfig(provider="openai", model="demo", api_key="secret",
                                         base_url="https://model.test/v1"), httpx.MockTransport(model))
        plan, mode = await Planner(provider=provider).plan("想找火锅店，人均100以内")
        assert mode == "model"
        assert plan.category == "美食" and plan.keyword == "火锅" and plan.max_price == 100
    asyncio.run(run())


def test_planner_normalizes_null_sort_and_total_budget_from_model():
    async def run():
        def model(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "category": "火锅", "keyword": None, "max_price": 400, "sort": None,
            })}}]})

        provider = LLMProvider(LLMConfig(provider="deepseek", model="deepseek-flash", api_key="secret",
                                         base_url="https://model.test"), httpx.MockTransport(model))
        plan, mode = await Planner(provider=provider).plan("两个人先吃火锅再唱歌，总预算400元")
        assert mode == "model"
        assert plan.category == "美食" and len(plan.stops) == 2
        assert plan.total_budget == 400 and plan.max_price is None and plan.sort == "score"
    asyncio.run(run())
