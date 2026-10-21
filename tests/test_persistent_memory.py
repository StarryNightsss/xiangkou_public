import asyncio

import httpx
import pytest

from xiangkou.dianping import DianpingClient
from xiangkou.api import app
from xiangkou.memory import MemoryPreferences, apply_memory
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest, SearchPlan, StopPlan
from xiangkou.session import SessionAccessError, merge_followup
from xiangkou.workflow import DiscoveryWorkflow


def test_sessions_runs_and_opt_in_memory_survive_workflow_restart(tmp_path):
    def backend(request):
        if request.url.path == "/shop-type/list":
            return httpx.Response(200, json={"success": True, "data": [{"id": 1, "name": "美食"}]})
        if request.url.path == "/shop/search":
            return httpx.Response(200, json={"success": True, "data": [
                {"id": 1, "name": "测试餐厅", "typeId": 1, "avgPrice": 70, "score": 48}], "total": 1})
        return httpx.Response(404)

    async def run():
        path = str(tmp_path / "state.sqlite3")
        first_client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        first = DiscoveryWorkflow(first_client, Planner(), "http://test", path)
        try:
            assert await first.memory.get(7) is None
            original = await first.run(ChatRequest(message="找美食"), owner_user_id=7)
            assert original.status == "ok"
            await first.memory.set(7, MemoryPreferences(category="美食", max_price=100))
        finally:
            await first_client.close()

        second_client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        second = DiscoveryWorkflow(second_client, Planner(), "http://test", path)
        try:
            assert (await second.runs.get(original.run_id, 7))["run_id"] == original.run_id
            assert await second.runs.get(original.run_id, 8) is None
            assert any(item["session_id"] == original.session_id
                       for item in await second.sessions.list_recent(7))
            assert await second.sessions.list_recent(8) == []
            with pytest.raises(SessionAccessError):
                await second.sessions.load(original.session_id, 8)
            changed = await second.run(ChatRequest(message="人均改成80元", session_id=original.session_id), 7)
            assert changed.status == "ok" and changed.plan.max_price == 80
            older = await first.sessions.load(original.session_id, 7)
            newer = await second.sessions.load(original.session_id, 7)
            assert not await first.sessions.save(older, changed.plan, [1], [70])
            assert await second.sessions.save(newer, changed.plan, [1], [70])
            remembered = await second.run(ChatRequest(message="推荐"), 7)
            assert remembered.status == "ok"
            assert remembered.plan.category == "美食" and remembered.plan.max_price == 100
            assert remembered.memory_applied == ["常选类别", "人均上限"]
            assert (await second.runs.get(remembered.run_id, 7))["memory_applied"] == remembered.memory_applied
            assert await second.memory.get(8) is None
            await second.memory.delete(7)
            assert await second.memory.get(7) is None
            without_memory = await second.run(ChatRequest(message="推荐"), 7)
            assert without_memory.status == "no_results"
        finally:
            await second_client.close()

    asyncio.run(run())


def test_followup_can_change_budget_score_category_and_opt_out_of_coupon():
    current = SearchPlan(category="美食", keyword="火锅", area="运河上街", max_price=120,
                         min_score=4.5, wants_coupon=True,
                         stops=[StopPlan(category="美食", keyword="火锅"), StopPlan(category="KTV")],
                         party_size=2, total_budget=400)
    changed = merge_followup("总预算改成500，评分降低到4分，不要优惠券", current,
                             SearchPlan(), [1, 10], [100, 90])
    assert changed.total_budget == 500 and changed.min_score == 4
    assert changed.max_price == 120 and changed.wants_coupon is False
    switched = merge_followup("第二站换成烤肉", changed,
                              SearchPlan(category="美食", keyword="烤肉"), [1, 10], [100, 90])
    assert switched.stops[0].keyword == "火锅"
    assert switched.stops[1].keyword == "烤肉"
    assert switched.category == "美食"
    assert current.stops[1].category == "KTV"
    single = SearchPlan(category="美食", keyword="火锅", min_score=4.5)
    alternate = merge_followup("换一家，不要评分限制", single, SearchPlan(), [9], [100])
    assert alternate.excluded_shop_ids == [9]
    assert alternate.min_score is None
    explicit = SearchPlan(category="美食")
    applied = apply_memory(explicit, MemoryPreferences(max_price=100, min_score=4.5),
                           "预算改成80，不限评分")
    assert applied == [] and explicit.max_price is None and explicit.min_score is None


def test_memory_api_requires_explicit_consent_and_is_user_scoped(tmp_path):
    async def run():
        def backend(request):
            if request.url.path == "/user/me":
                token = request.headers.get("authorization")
                if token in ("token-a", "token-b"):
                    return httpx.Response(200, json={"success": True, "data": {
                        "id": 7 if token == "token-a" else 8}})
                return httpx.Response(401)
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        previous = getattr(app.state, "workflow", None)
        app.state.workflow = DiscoveryWorkflow(client, Planner(), "http://test", str(tmp_path / "api.sqlite3"))
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                         headers={"authorization": "token-a"}) as owner:
                assert (await owner.get("/api/memory")).json()["enabled"] is False
                assert (await owner.put("/api/memory", json={"preferences": {"area": "运河上街"}})).status_code == 422
                saved = await owner.put("/api/memory", json={
                    "consent": True, "preferences": {"area": "运河上街", "max_price": 100}})
                assert saved.status_code == 200
                assert saved.json()["preferences"]["area"] == "运河上街"
                async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                             headers={"authorization": "token-b"}) as other:
                    assert (await other.get("/api/memory")).json()["enabled"] is False
                    assert (await other.get("/api/sessions")).json()["sessions"] == []
                assert (await owner.delete("/api/memory")).status_code == 200
                assert (await owner.get("/api/memory")).json()["enabled"] is False
        finally:
            app.state.workflow = previous
            await client.close()

    asyncio.run(run())
