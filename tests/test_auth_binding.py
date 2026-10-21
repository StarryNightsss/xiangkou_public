import asyncio

import httpx

from xiangkou.api import app
from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner
from xiangkou.workflow import DiscoveryWorkflow


def test_login_binds_sessions_and_runs_to_dianping_user():
    async def run():
        def dianping(request):
            path = request.url.path
            if path == "/user/code":
                return httpx.Response(200, json={"success": True})
            if path == "/user/login":
                return httpx.Response(200, json={"success": True, "data": "token-a"})
            if path == "/user/me":
                token = request.headers.get("authorization")
                if token in ("token-a", "token-b"):
                    user_id = 1 if token == "token-a" else 2
                    return httpx.Response(200, json={"success": True, "data": {"id": user_id, "nickName": f"用户{user_id}"}})
                return httpx.Response(401)
            if path == "/shop-type/list":
                return httpx.Response(200, json={"success": True, "data": [{"id": 1, "name": "美食"}]})
            if path == "/shop/search":
                return httpx.Response(200, json={"success": True, "data": [
                    {"id": 1, "name": "测试餐厅", "typeId": 1, "avgPrice": 80, "score": 48}], "total": 1})
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(dianping))
        previous = getattr(app.state, "workflow", None)
        app.state.workflow = DiscoveryWorkflow(client, Planner(), "http://test")
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou") as browser:
                assert (await browser.post("/api/chat", json={"message": "找美食"})).status_code == 401
                assert (await browser.post("/api/chat/stream", json={"message": "你好"})).status_code == 401
                for endpoint in ("/api/sessions", "/api/memory", "/api/my/orders", "/api/my/vouchers"):
                    assert (await browser.get(endpoint)).status_code == 401
                assert (await browser.post("/api/auth/code", json={"email": "user@example.com"})).status_code == 200
                login = await browser.post("/api/auth/login", json={"email": "user@example.com", "code": "123456"})
                assert login.status_code == 200
                assert login.json()["user"]["id"] == 1
                assert "httponly" in login.headers["set-cookie"].lower()
                assert "token-a" not in login.text
                chat = await browser.post("/api/chat", json={"message": "找美食"})
                assert chat.status_code == 200, chat.text
                data = chat.json()
                assert data["status"] == "ok"
                run_id, session_id = data["run_id"], data["session_id"]
                assert (await browser.get(f"/api/runs/{run_id}")).status_code == 200

                async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                             headers={"authorization": "token-b"}) as other:
                    assert (await other.get("/api/auth/me")).json()["user"]["id"] == 2
                    assert (await other.get(f"/api/runs/{run_id}")).status_code == 404
                    assert (await other.delete(f"/api/sessions/{session_id}")).status_code == 404
                    foreign = await other.post("/api/chat", json={"message": "找美食", "session_id": session_id})
                    assert foreign.status_code == 404

                assert (await browser.get(f"/api/runs/{run_id}")).status_code == 200
                assert (await browser.delete(f"/api/sessions/{session_id}")).status_code == 200
                assert (await browser.post("/api/auth/logout")).status_code == 200
                assert (await browser.get("/api/auth/me")).status_code == 401
        finally:
            app.state.workflow = previous
            await client.close()

    asyncio.run(run())
