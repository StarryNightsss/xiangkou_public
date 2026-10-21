"""长对话压缩：超过保留窗口的问答要被摘要并持久化，指标事件要落进轨迹。"""
import asyncio
from types import SimpleNamespace

from xiangkou.conversations import ConversationStore
from xiangkou.dialogue import route_message
from xiangkou.llm import Completion
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest
from xiangkou.session import SessionStore
from xiangkou.storage import StateDatabase
from xiangkou.workflow import DiscoveryWorkflow


class FakeProvider:
    config = SimpleNamespace(timeout=1, provider="test", model="test")
    enabled = True

    def __init__(self):
        self.systems = []

    async def complete(self, system, messages, tools=None, max_tokens=500):
        self.systems.append(system)
        if "会话摘要器" in system:
            return Completion(text="- 用户偏好火锅，预算 120 元")
        return Completion(text="好的，已记住。")


async def seed(history, session_id, pairs):
    for index in range(pairs):
        turn = await history.begin(session_id, 7, f"问题{index}")
        history.finish(turn, f"回答{index}", run_id=f"run{index}")


def test_older_turns_are_summarized_and_metrics_recorded(tmp_path):
    async def run():
        database = StateDatabase(str(tmp_path / "summary.sqlite3"))
        sessions = SessionStore(database=database)
        provider = FakeProvider()
        workflow = DiscoveryWorkflow(None, Planner(provider=provider), "http://test", str(tmp_path / "summary.sqlite3"))
        workflow.sessions = sessions
        workflow.conversations = ConversationStore(sessions)
        state = await sessions.load(None, 7)
        await seed(workflow.conversations, state.id, 8)

        result, _ = await route_message(ChatRequest(message="继续", session_id=state.id), workflow, 7)

        stored = await workflow.conversations.summary(state.id, 7)
        # 8 组问答保留最近 5 组，前 3 组被压缩成摘要
        assert stored and stored["covered_turns"] == 3
        assert "用户偏好火锅" in stored["summary"]
        # 摘要必须注入到本轮系统提示里，而不是只落库
        assert any("早期对话摘要" in system and "用户偏好火锅" in system for system in provider.systems)
        metrics = [event for event in result.events if event.get("event") == "context_metrics"]
        assert metrics and metrics[-1]["summarized_pairs"] == 3
        assert metrics[-1]["summary_chars"] > 0
        assert result.answer

    asyncio.run(run())

def test_short_conversation_skips_summary(tmp_path):
    async def run():
        database = StateDatabase(str(tmp_path / "short.sqlite3"))
        sessions = SessionStore(database=database)
        provider = FakeProvider()
        workflow = DiscoveryWorkflow(None, Planner(provider=provider), "http://test", str(tmp_path / "short.sqlite3"))
        workflow.sessions = sessions
        workflow.conversations = ConversationStore(sessions)
        state = await sessions.load(None, 7)
        await seed(workflow.conversations, state.id, 3)

        result, _ = await route_message(ChatRequest(message="继续", session_id=state.id), workflow, 7)

        assert await workflow.conversations.summary(state.id, 7) is None
        metrics = [event for event in result.events if event.get("event") == "context_metrics"]
        assert metrics and metrics[-1]["summarized_pairs"] == 0
        assert not any("早期对话摘要" in system for system in provider.systems)

    asyncio.run(run())
