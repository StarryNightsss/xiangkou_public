import asyncio

import httpx

from xiangkou.dianping import DianpingClient
from xiangkou.harness import AgentHarness, HarnessError
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow


def test_single_stop_relaxation_comes_from_current_candidates():
    async def run():
        def backend(request):
            if request.url.path == "/shop-type/list":
                return httpx.Response(200, json={"success": True, "data": [{"id": 1, "name": "美食"}]})
            if request.url.path == "/shop/search":
                shops = [
                    {"id": 1, "name": "贵店", "typeId": 1, "avgPrice": 120, "score": 46, "area": "甲商圈"},
                    {"id": 2, "name": "低分店", "typeId": 1, "avgPrice": 90, "score": 42, "area": "甲商圈"},
                ]
                if request.url.params.get("maxPrice") or request.url.params.get("minScore"):
                    shops = []
                return httpx.Response(200, json={"success": True, "data": shops, "total": len(shops)})
            return httpx.Response(404)
        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        try:
            workflow = DiscoveryWorkflow(client, Planner(), "http://test")
            result = await workflow.run(ChatRequest(message="找甲商圈人均100以内评分4.5以上的美食"))
            assert result.status == "no_results"
            assert any("120" in option.label for option in result.relaxations)
            assert any("4.2" in option.label for option in result.relaxations)
            assert {item.shop_id for item in result.candidate_decisions} == {1, 2}
        finally:
            await client.close()
    asyncio.run(run())


def test_harness_limits_full_result_and_redacts_event_tokens():
    async def run():
        async def huge():
            return {"text": "x" * 100}
        events = []
        harness = AgentHarness({"huge": huge}, max_result_bytes=30, on_event=events.append)
        harness.record("probe", authorization="secret")
        assert events[-1]["authorization"] == "[redacted]"
        harness.record("nested", payload={"api_key": "secret", "message": "Bearer abcdefghijklmnop"})
        assert events[-1]["payload"] == {"api_key": "[redacted]", "message": "Bearer [redacted]"}
        try:
            await harness.call("huge")
        except HarnessError as exc:
            assert "大小限制" in str(exc)
        else:
            assert False, "oversized result must fail"
    asyncio.run(run())
