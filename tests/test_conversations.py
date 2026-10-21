import asyncio
import sqlite3

import httpx
import pytest

from xiangkou.api import app, require_user, _run_chat
from xiangkou.conversations import ConversationStore
from xiangkou.schemas import ChatRequest, ChatResponse
from xiangkou.session import SessionStore, SessionAccessError
from xiangkou.storage import StateDatabase
from xiangkou.workflow import DiscoveryWorkflow
from xiangkou.planner import Planner


def test_history_restart_isolation_revision_and_deletion(tmp_path):
    async def run():
        path = str(tmp_path / "history.sqlite3")
        sessions = SessionStore(database=StateDatabase(path))
        history = ConversationStore(sessions)
        state = await sessions.load(None, 7)
        turn = await history.begin(state.id, 7, "火锅 sk-abcdefghijklmnop 13900009999")
        history.finish(turn, "预算120", run_id="run1")
        next_turn = await history.begin(state.id, 7, "便宜一点")
        history.finish(next_turn, "本轮已取消", "cancelled")
        restored = ConversationStore(SessionStore(database=StateDatabase(path)))
        messages, run_id = await restored.read(state.id, 7)
        assert len(messages) == 4 and messages[-1]["status"] == "cancelled"
        assert "abcdefghijklmnop" not in messages[0]["text"] and "13900009999" not in messages[0]["text"]
        assert run_id == "run1"
        with sessions.database.connect() as conn:
            assert conn.execute("SELECT revision FROM sessions WHERE id=?", (state.id,)).fetchone()[0] == 0
        with pytest.raises(SessionAccessError):
            await restored.read(state.id, 8)
        assert await sessions.clear(state.id, 7)
        with sessions.database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0] == 0
        with pytest.raises(SessionAccessError):
            await restored.read(state.id, 7)
    asyncio.run(run())


def test_v3_upgrade_keeps_existing_sessions(tmp_path):
    path = str(tmp_path / "v3.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=3")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, owner_user_id INTEGER, plan_json TEXT, shop_ids_json TEXT, shop_prices_json TEXT, revision INTEGER, updated_at REAL)")
        conn.execute("INSERT INTO sessions VALUES ('keep',7,NULL,'[]','[]',2,1)")
    database = StateDatabase(path)
    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT revision FROM sessions WHERE id='keep'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0] == 0


def test_chat_history_api_two_turns_and_failure(tmp_path):
    async def run():
        workflow = DiscoveryWorkflow(None, Planner(), "http://example", str(tmp_path / "api.sqlite3"))
        async def fake_run(body, **kwargs):
            if body.message == "fail":
                raise RuntimeError("private error")
            response = ChatResponse(session_id=body.session_id, run_id="run-" + body.message,
                                    status="ok", mode="rules", answer="回答：" + body.message)
            await workflow.runs.save(response, kwargs["owner_user_id"])
            return response
        workflow.run = fake_run
        previous = app.state.workflow if hasattr(app.state, "workflow") else None
        old_actions = getattr(app.state, "actions", None)
        if hasattr(app.state, "actions"): del app.state.actions
        app.state.workflow = workflow
        app.dependency_overrides[require_user] = lambda: {"id": 7}
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://xiangkou") as client:
                first = await client.post("/api/chat", json={"message": "火锅"})
                assert first.status_code == 200, first.text
                sid = first.json()["session_id"]
                second = await client.post("/api/chat", json={"message": "便宜点", "session_id": sid})
                assert second.status_code == 200, second.text
                data = (await client.get(f"/api/sessions/{sid}/messages")).json()
                assert [m["text"] for m in data["messages"]] == ["火锅", "回答：火锅", "便宜点", "回答：便宜点"]
                assert data["last_response"]["answer"] == "回答：便宜点"
                listed = (await client.get("/api/sessions")).json()["sessions"][0]
                assert listed["title"] == "火锅" and listed["message_count"] == 4
                with pytest.raises(RuntimeError):
                    await _run_chat(ChatRequest(message="fail", session_id=sid), 7)
                messages, _ = await workflow.conversations.read(sid, 7)
                assert messages[-1]["status"] == "error" and "private" not in messages[-1]["text"]
                app.dependency_overrides[require_user] = lambda: {"id": 8}
                assert (await client.get(f"/api/sessions/{sid}/messages")).status_code == 404
                app.dependency_overrides[require_user] = lambda: {"id": 7}
                assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
                assert (await client.get(f"/api/sessions/{sid}/messages")).status_code == 404
        finally:
            app.state.workflow = previous
            if old_actions is not None: app.state.actions = old_actions
            app.dependency_overrides.pop(require_user, None)
    asyncio.run(run())


def test_cancelled_chat_keeps_owned_session_and_user_message():
    async def run():
        workflow = DiscoveryWorkflow(None, Planner(), "http://example")
        async def fake_run(body, **kwargs):
            raise asyncio.CancelledError()
        workflow.run = fake_run
        previous = getattr(app.state, "workflow", None)
        old_actions = getattr(app.state, "actions", None)
        if hasattr(app.state, "actions"): del app.state.actions
        app.state.workflow = workflow
        events = []
        try:
            with pytest.raises(asyncio.CancelledError):
                await _run_chat(ChatRequest(message="火锅"), 7, on_event=events.append)
            sid = events[0]["session_id"]
            messages, _ = await workflow.conversations.read(sid, 7)
            assert messages[0]["text"] == "火锅" and messages[1]["status"] == "cancelled"
        finally:
            app.state.workflow = previous
            if old_actions is not None: app.state.actions = old_actions
    asyncio.run(run())

def test_business_choice_summary_is_restored_as_displayed():
    from xiangkou.conversations import response_summary
    response = ChatResponse(status="ok", mode="rules", answer="请选择券", business_choices=[
        {"index": 1, "shop_name": "火锅店", "title": "双人套餐", "pay_cents": 8000}])
    assert "1. 火锅店 · 双人套餐 · ¥80.00" in response_summary(response)


def test_history_capacity_retains_last_hundred_turns():
    async def run():
        sessions = SessionStore()
        history = ConversationStore(sessions)
        sid = (await sessions.load(None, 7)).id
        for n in range(101):
            turn = await history.begin(sid, 7, f"用户{n}")
            history.finish(turn, f"回复{n}")
        messages, _ = await history.read(sid, 7)
        assert len(messages) == 200 and messages[0]["text"] == "用户1"
        assert messages[-1]["text"] == "回复100"
    asyncio.run(run())


def test_per_turn_cards_restore_without_draft_credentials_or_cross_account_access(tmp_path):
    async def run():
        from xiangkou.schemas import ShopCard
        path=str(tmp_path/'cards.sqlite3')
        workflow=DiscoveryWorkflow(None,Planner(),'http://web',path)
        sid=(await workflow.sessions.load(None,7)).id
        for index in (1,2):
            turn=await workflow.conversations.begin(sid,7,f'查询{index}')
            response=ChatResponse(answer=f'方案{index}',session_id=sid,run_id=f'cards-{index}',status='ok',mode='rules',
                shops=[ShopCard(id=index,name=f'店铺{index}',url=f'http://web/shop/{index}',reason='真实结果')],
                action_draft={'confirmation_token':'must-not-expose'})
            await workflow.runs.save(response,7)
            workflow.conversations.finish(turn,f'方案{index}\n推荐店铺：重复摘要',run_id=response.run_id)
        previous=getattr(app.state,'workflow',None)
        app.state.workflow=DiscoveryWorkflow(None,Planner(),'http://web',path)
        app.dependency_overrides[require_user]=lambda:{'id':7}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                result=await client.get(f'/api/sessions/{sid}/messages')
                assert result.status_code==200
                messages=result.json()['messages'];answers=[m for m in messages if m['role']=='assistant']
                assert [m['result']['shops'][0]['id'] for m in answers]==[1,2]
                assert [m['text'] for m in answers]==['方案1','方案2']
                assert all('action_draft' not in m['result'] for m in answers)
                assert all('must-not-expose' not in str(m) for m in answers)
                app.dependency_overrides[require_user]=lambda:{'id':8}
                assert (await client.get(f'/api/sessions/{sid}/messages')).status_code==404
        finally:
            app.state.workflow=previous
            app.dependency_overrides.pop(require_user,None)
    asyncio.run(run())
