"""Owned conversation display history; workflow context remains structured state."""
from .harness import mask_personal_info
import asyncio
import re
import time
from uuid import uuid4


def safe_text(value):
    text = mask_personal_info(str(value or "")[:6000])
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[密钥已隐藏]", text)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", "[凭证已隐藏]", text, flags=re.I)
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)


class ConversationStore:
    def __init__(self, sessions):
        self.sessions = sessions
        self.database = sessions.database
        self._turns = {}
        self._summaries = {}

    async def begin(self, session_id, owner, message):
        await self.sessions.require_owned(session_id, owner)
        turn = dict(id=str(uuid4()), session_id=session_id, owner_user_id=owner,
                    user_text=safe_text(message), assistant_text="", status="pending",
                    run_id=None, created_at=time.time())
        if self.database:
            def write():
                with self.database.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if not conn.execute("SELECT 1 FROM sessions WHERE id=? AND owner_user_id=?",
                                        (session_id, owner)).fetchone():
                        from .session import SessionAccessError
                        raise SessionAccessError("会话不存在或已过期")
                    conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
                    conn.execute("INSERT INTO conversation_turns VALUES (:id,:session_id,:owner_user_id,"
                                 ":user_text,:assistant_text,:status,:run_id,:created_at)", turn)
                    conn.execute("DELETE FROM conversation_turns WHERE session_id=? AND id NOT IN "
                                 "(SELECT id FROM conversation_turns WHERE session_id=? ORDER BY created_at DESC LIMIT 100)",
                                 (session_id, session_id))
            await asyncio.to_thread(write)
        else:
            self.sessions._states[session_id].updated_at = time.monotonic()
            self._turns[turn["id"]] = turn
            old = [t for t in self._turns.values() if t["session_id"] == session_id]
            for t in old[:-100]:
                self._turns.pop(t["id"], None)
        return turn["id"]

    def finish(self, turn_id, text, status="done", run_id=None):
        if self.database:
            with self.database.connect() as conn:
                conn.execute("UPDATE conversation_turns SET assistant_text=?,status=?,run_id=? WHERE id=?",
                             (safe_text(text), status, run_id, turn_id))
        elif turn_id in self._turns:
            self._turns[turn_id].update(assistant_text=safe_text(text), status=status, run_id=run_id)

    async def complete(self, turn_id, text, status="done", run_id=None):
        if self.database:
            await asyncio.to_thread(self.finish, turn_id, text, status, run_id)
        else:
            self.finish(turn_id, text, status, run_id)

    async def read(self, session_id, owner):
        await self.sessions.require_owned(session_id, owner)
        if self.database:
            def read():
                with self.database.connect() as conn:
                    return [dict(r) for r in conn.execute("SELECT * FROM conversation_turns WHERE session_id=? "
                             "AND owner_user_id=? ORDER BY created_at,id", (session_id, owner)).fetchall()]
            turns = await asyncio.to_thread(read)
        else:
            turns = [t.copy() for t in self._turns.values()
                     if t["session_id"] == session_id and t["owner_user_id"] == owner]
        messages = []
        for t in turns:
            messages.append(dict(id=t["id"]+"-user", role="user", text=t["user_text"], status="done"))
            messages.append(dict(id=t["id"]+"-assistant", role="assistant",
                                 text=t["assistant_text"] or "上一轮尚未完成或连接已中断，可重新发送。",
                                 status=t["status"], run_id=t["run_id"]))
        return messages, next((t["run_id"] for t in reversed(turns) if t["run_id"]), None)

    async def summary(self, session_id, owner):
        """读取该会话的早期对话摘要；没有则返回 None。"""
        await self.sessions.require_owned(session_id, owner)
        if self.database:
            def read():
                with self.database.connect() as conn:
                    row = conn.execute("SELECT summary, covered_turns, updated_at FROM session_summaries "
                                       "WHERE session_id=? AND owner_user_id=?", (session_id, owner)).fetchone()
                    return dict(row) if row else None
            return await asyncio.to_thread(read)
        return self._summaries.get(session_id)

    async def save_summary(self, session_id, owner, summary, covered_turns):
        """保存（覆盖）会话摘要；covered_turns 表示已压缩进摘要的完整对话轮数。"""
        await self.sessions.require_owned(session_id, owner)
        payload = {"summary": safe_text(summary)[:2000], "covered_turns": int(covered_turns),
                   "updated_at": time.time()}
        if self.database:
            def write():
                with self.database.connect() as conn:
                    conn.execute("INSERT INTO session_summaries VALUES (?,?,?,?,?) "
                                 "ON CONFLICT(session_id) DO UPDATE SET "
                                 "summary=excluded.summary, covered_turns=excluded.covered_turns, "
                                 "updated_at=excluded.updated_at",
                                 (session_id, owner, payload["summary"], payload["covered_turns"], payload["updated_at"]))
            await asyncio.to_thread(write)
        else:
            self._summaries[session_id] = payload

    def clear(self, session_id):
        # SQLite deletion cascades through the sessions trigger.
        self._turns = {k: t for k, t in self._turns.items() if t["session_id"] != session_id}
        self._summaries.pop(session_id, None)

def response_summary(result):
    parts = [result.answer]
    if result.confirmed_constraints:
        parts.append("当前条件：" + " · ".join(result.confirmed_constraints))
    if result.shops:
        parts.append("推荐店铺：" + "、".join(shop.name for shop in result.shops))
    if result.business_choices:
        parts.append("\n".join(f"{c.get('index', '')}. {c.get('shop_name', '')} · {c.get('title', '')} · ¥{c.get('pay_cents', 0) / 100:.2f}" for c in result.business_choices))
    if result.action_draft:
        parts.append("已生成操作草稿，请在确认窗口核对。")
    return "\n\n".join(part for part in parts if part)
