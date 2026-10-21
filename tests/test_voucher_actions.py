import asyncio

import httpx
import pytest

from xiangkou.actions import ActionError, VoucherActionWorkflow
from xiangkou.api import app
from xiangkou.dianping import DianpingClient
from xiangkou.planner import Planner
from xiangkou.runs import RunStore
from xiangkou.storage import StateDatabase
from xiangkou.schemas import ChatResponse, ShopCard
from xiangkou.workflow import DiscoveryWorkflow


def test_paid_ordinary_order_can_be_cancelled_only_by_owner(tmp_path):
    async def run():
        status = 1
        def backend(request):
            nonlocal status
            path = request.url.path
            if path == "/shop/1":
                data = {"id": 1, "name": "测试店"}
            elif path == "/voucher/list/1":
                data = [{"id": 9, "shopId": 1, "title": "付费券", "rules": "堂食",
                         "payValue": 1000, "actualValue": 1500, "type": 0}]
            elif path == "/voucher-order/ordinary/9":
                data = 88
            elif path == "/voucher-order/88":
                data = {"id": 88, "voucherId": 9, "status": status, "voucherType": 0, "payValue": 1000}
            elif path == "/voucher-order/88/cancel" and request.method == "POST":
                status = 4
                data = 88
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={"success": True, "data": data})
        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        runs = RunStore(database=StateDatabase(str(tmp_path / "state.sqlite3")))
        await runs.save(ChatResponse(answer="ok", status="ok", mode="rules", run_id="run-cancel", shops=[
            ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), 7)
        actions = VoucherActionWorkflow(client, runs)
        try:
            preview = await actions.prepare(7, "run-cancel", 1, 9)
            await actions.confirm(preview["action_id"], 7, "token")
            with pytest.raises(ActionError):
                await actions.cancel_order(preview["action_id"], 8, "other")
            assert (await actions.cancel_order(preview["action_id"], 7, "token"))["order_status"] == 4
            assert (await actions.status(preview["action_id"], 7, "token"))["order_status"] == 4
            assert (await actions.cancel_order(preview["action_id"], 7, "token"))["order_status"] == 4
        finally:
            await client.close()
    asyncio.run(run())


def test_action_preview_and_audit_survive_restart_without_duplicate_submit(tmp_path):
    async def run():
        writes = []
        def backend(request):
            if request.url.path == "/shop/1":
                return httpx.Response(200, json={"success": True, "data": {"id": 1, "name": "测试店"}})
            if request.url.path == "/voucher/list/1":
                return httpx.Response(200, json={"success": True, "data": [{
                    "id": 9, "shopId": 1, "title": "免费券", "rules": "堂食",
                    "payValue": 0, "actualValue": 500, "type": 0}]})
            if request.url.path == "/voucher-order/ordinary/9":
                writes.append(1)
                return httpx.Response(200, json={"success": True, "data": 88})
            if request.url.path == "/voucher-order/88":
                return httpx.Response(200, json={"success": True, "data": {
                    "id": 88, "voucherId": 9, "status": 2}})
            return httpx.Response(404)
        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        db = StateDatabase(str(tmp_path / "actions.sqlite3"))
        runs = RunStore(database=db)
        await runs.save(ChatResponse(answer="ok", status="ok", mode="rules", run_id="run-5", shops=[
            ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), 7)
        first = VoucherActionWorkflow(client, runs)
        try:
            preview = await first.prepare(7, "run-5", 1, 9)
            second = VoucherActionWorkflow(client, RunStore(database=StateDatabase(str(tmp_path / "actions.sqlite3"))))
            with pytest.raises(ActionError):
                await second.status(preview["action_id"], 8, "other")
            result = await second.confirm(preview["action_id"], 7, "token")
            third = VoucherActionWorkflow(client, runs)
            assert (await third.confirm(preview["action_id"], 7, "token")) == result
            assert (await third.status(preview["action_id"], 7, "token"))["order_status"] == 2
            assert writes == [1]
            with db.connect() as conn:
                states = [row[0] for row in conn.execute(
                    "SELECT state FROM voucher_action_events WHERE action_id=? ORDER BY id", (preview["action_id"],))]
            assert states == ["prepared", "submitting", "submitted"]
        finally:
            await client.close()
    asyncio.run(run())


def test_write_requires_preview_confirmation_and_is_idempotent():
    async def run():
        posts = []
        coupon = {"id": 1001, "shopId": 1, "title": "免费券", "rules": "堂食",
                  "payValue": 0, "actualValue": 500, "type": 0}

        def backend(request):
            if request.url.path == "/shop/1":
                return httpx.Response(200, json={"success": True, "data": {"id": 1, "name": "测试店"}})
            if request.url.path == "/voucher/list/1":
                return httpx.Response(200, json={"success": True, "data": [coupon]})
            if request.url.path == "/voucher-order/ordinary/1001":
                posts.append(request.headers.get("authorization"))
                return httpx.Response(200, json={"success": True, "data": 1234567890123456789})
            if request.url.path == "/voucher-order/1234567890123456789":
                return httpx.Response(200, json={"success": True, "data": {
                    "id": 1234567890123456789, "voucherId": 1001, "status": 2}})
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        runs = RunStore()
        await runs.save(ChatResponse(answer="ok", status="ok", mode="rules", run_id="run-1", shops=[
            ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), owner_user_id=7)
        actions = VoucherActionWorkflow(client, runs)
        try:
            with pytest.raises(ActionError):
                await actions.prepare(8, "run-1", 1, 1001)
            preview = await actions.prepare(7, "run-1", 1, 1001)
            assert preview["operation"] == "领取免费券"
            assert not posts
            action_id = preview["action_id"]
            with pytest.raises(ActionError):
                await actions.confirm(action_id, 8, "token-b")
            first = await actions.confirm(action_id, 7, "token-a")
            second = await actions.confirm(action_id, 7, "token-a")
            assert first == second
            assert first["order_id"] == "1234567890123456789"
            assert posts == ["token-a"]
            assert (await actions.status(action_id, 7, "token-a"))["order_status"] == 2
        finally:
            await client.close()

    asyncio.run(run())


def test_action_api_requires_login_and_same_origin():
    async def run():
        calls = []

        def backend(request):
            if request.url.path == "/user/me":
                token = request.headers.get("authorization")
                if token in ("token-a", "token-b"):
                    return httpx.Response(200, json={"success": True, "data": {
                        "id": 7 if token == "token-a" else 8, "nickName": "用户"}})
                return httpx.Response(401)
            if request.url.path == "/shop/1":
                return httpx.Response(200, json={"success": True, "data": {"id": 1, "name": "测试店"}})
            if request.url.path == "/voucher/list/1":
                return httpx.Response(200, json={"success": True, "data": [
                    {"id": 9, "shopId": 1, "title": "免费券", "rules": "堂食",
                     "payValue": 0, "actualValue": 500, "type": 0}]})
            if request.url.path == "/voucher-order/ordinary/9":
                calls.append(request.headers.get("authorization"))
                return httpx.Response(200, json={"success": True, "data": 77})
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        old_workflow = getattr(app.state, "workflow", None)
        old_actions = getattr(app.state, "actions", None)
        app.state.workflow = DiscoveryWorkflow(client, Planner(), "http://test")
        app.state.actions = VoucherActionWorkflow(client, app.state.workflow.runs)
        run_id = "00000000-0000-0000-0000-000000000003"
        await app.state.workflow.runs.save(ChatResponse(
            answer="ok", status="ok", mode="rules", run_id=run_id, shops=[
                ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), 7)
        try:
            transport = httpx.ASGITransport(app=app)
            payload = {"run_id": run_id, "shop_id": 1, "voucher_id": 9}
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou") as anonymous:
                assert (await anonymous.post("/api/actions/vouchers/prepare", json=payload)).status_code == 401
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                         headers={"authorization": "token-b"}) as other:
                assert (await other.post("/api/actions/vouchers/prepare", json=payload)).status_code == 404
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                         headers={"authorization": "token-a"}) as owner:
                assert (await owner.post("/api/actions/vouchers/prepare", json=payload,
                                         headers={"origin": "http://evil"})).status_code == 403
                preview = await owner.post("/api/actions/vouchers/prepare", json=payload)
                assert preview.status_code == 200
                action_id = preview.json()["action_id"]
                async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou",
                                             headers={"authorization": "token-b"}) as other:
                    assert (await other.post(f"/api/actions/{action_id}/confirm")).status_code == 404
                assert not calls
                confirmed = await owner.post(f"/api/actions/{action_id}/confirm")
                assert confirmed.status_code == 200
                assert confirmed.json()["order_id"] == "77"
                assert calls == ["token-a"]
        finally:
            app.state.workflow = old_workflow
            app.state.actions = old_actions
            await client.close()

    asyncio.run(run())


def test_price_change_blocks_write_and_requires_new_preview():
    async def run():
        posts = []
        coupon = {"id": 9, "shopId": 1, "title": "付费券", "rules": "堂食",
                  "payValue": 990, "actualValue": 2000, "type": 0}

        def backend(request):
            if request.url.path == "/shop/1":
                return httpx.Response(200, json={"success": True, "data": {"id": 1, "name": "测试店"}})
            if request.url.path == "/voucher/list/1":
                return httpx.Response(200, json={"success": True, "data": [coupon]})
            if request.url.path.startswith("/voucher-order/"):
                posts.append(request.url.path)
                return httpx.Response(200, json={"success": True, "data": 88})
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        runs = RunStore()
        await runs.save(ChatResponse(answer="ok", status="ok", mode="rules", run_id="run-2", shops=[
            ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), owner_user_id=7)
        actions = VoucherActionWorkflow(client, runs)
        try:
            preview = await actions.prepare(7, "run-2", 1, 9)
            assert preview["operation"] == "创建待支付订单"
            coupon["payValue"] = 1090
            with pytest.raises(ActionError, match="变化"):
                await actions.confirm(preview["action_id"], 7, "token-a")
            assert not posts
            with pytest.raises(ActionError):
                await actions.confirm(preview["action_id"], 7, "token-a")
        finally:
            await client.close()

    asyncio.run(run())


def test_unknown_submit_result_is_reconciled_without_retrying_write():
    async def run():
        writes = []

        def backend(request):
            if request.url.path == "/shop/1":
                return httpx.Response(200, json={"success": True, "data": {"id": 1, "name": "测试店"}})
            if request.url.path == "/voucher/list/1":
                return httpx.Response(200, json={"success": True, "data": [
                    {"id": 9, "shopId": 1, "title": "免费券", "rules": "堂食",
                     "payValue": 0, "actualValue": 500, "type": 0}]})
            if request.url.path == "/voucher-order/ordinary/9":
                writes.append(1)
                raise httpx.ReadTimeout("response lost")
            if request.url.path == "/voucher-order/of/voucher/9":
                return httpx.Response(200, json={"success": True, "data": {
                    "id": 88, "voucherId": 9, "status": 2}})
            return httpx.Response(404)

        client = DianpingClient("http://test", transport=httpx.MockTransport(backend))
        runs = RunStore()
        await runs.save(ChatResponse(answer="ok", status="ok", mode="rules", run_id="run-4", shops=[
            ShopCard(id=1, name="测试店", url="http://test/shop/1", reason="有券")]), 7)
        actions = VoucherActionWorkflow(client, runs)
        try:
            action_id = (await actions.prepare(7, "run-4", 1, 9))["action_id"]
            with pytest.raises(ActionError, match="结果未确认"):
                await actions.confirm(action_id, 7, "token-a")
            with pytest.raises(ActionError, match="不能再次提交"):
                await actions.confirm(action_id, 7, "token-a")
            reconciled = await actions.status(action_id, 7, "token-a")
            assert reconciled["order_id"] == "88" and reconciled["order_status"] == 2
            assert writes == [1]
        finally:
            await client.close()

    asyncio.run(run())
