"""短期运行记录，用于按 run_id 回看模型决策、工具调用和校验。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any

from .schemas import ChatResponse
from .storage import StateDatabase


class RunStore:
    def __init__(self, ttl_seconds: int = 86400, max_runs: int = 1000,
                 database: StateDatabase | None = None):
        self.ttl_seconds = ttl_seconds
        self.max_runs = max_runs
        self.database = database
        self._runs: OrderedDict[str, tuple[float, int | None, dict[str, Any]]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def save(self, response: ChatResponse, owner_user_id: int | None = None) -> None:
        if not response.run_id:
            return
        record = {
            "run_id": response.run_id, "session_id": response.session_id,
            "status": response.status, "mode": response.mode,
            "plan": response.plan.model_dump(exclude={"excluded_shop_ids"}) if response.plan else None,
            "shop_ids": list(dict.fromkeys(shop.id for shop in response.shops +
                                          [stop for itinerary in response.itineraries for stop in itinerary.stops])),
            "response": response.model_dump(mode="json"),
            "memory_applied": response.memory_applied,
            "candidate_decisions": [item.model_dump() for item in response.candidate_decisions],
            "candidate_summary": response.candidate_summary,
            "relaxations": [item.model_dump() for item in response.relaxations],
            "events": response.events,
        }
        if self.database:
            def write():
                with self.database.connect() as conn:
                    conn.execute("DELETE FROM runs WHERE created_at < ?", (time.time() - self.ttl_seconds,))
                    conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?)",
                                 (response.run_id, owner_user_id, json.dumps(record, ensure_ascii=False), time.time()))
                    count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    if count > self.max_runs:
                        conn.execute("DELETE FROM runs WHERE id IN (SELECT id FROM runs ORDER BY created_at LIMIT ?)",
                                     (count - self.max_runs,))
            await asyncio.to_thread(write)
            return
        async with self._lock:
            self._prune()
            self._runs[response.run_id] = (time.monotonic(), owner_user_id, record)
            while len(self._runs) > self.max_runs:
                self._runs.popitem(last=False)

    async def get(self, run_id: str, owner_user_id: int | None = None) -> dict[str, Any] | None:
        if self.database:
            def read():
                with self.database.connect() as conn:
                    row = conn.execute("SELECT record_json FROM runs WHERE id=? AND owner_user_id IS ? "
                                       "AND created_at >= ?", (run_id, owner_user_id,
                                                              time.time() - self.ttl_seconds)).fetchone()
                    return json.loads(row["record_json"]) if row else None
            return await asyncio.to_thread(read)
        async with self._lock:
            self._prune()
            saved = self._runs.get(run_id)
            return dict(saved[2]) if saved and saved[1] == owner_user_id else None

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (created, _owner, _record) in list(self._runs.items()):
            if now - created > self.ttl_seconds:
                self._runs.pop(key)

    async def latest_for_session(self, session_id: str, owner_user_id: int, display: bool = False) -> dict | None:
        if self.database:
            def read():
                with self.database.connect() as conn:
                    rows = conn.execute("SELECT record_json FROM runs WHERE owner_user_id=? AND created_at>=? "
                                        "ORDER BY created_at DESC LIMIT 1000", (owner_user_id, time.time() - self.ttl_seconds)).fetchall()
                    return [json.loads(row[0]) for row in rows]
            records = await asyncio.to_thread(read)
        else:
            records = [value[2] for value in reversed(list(self._runs.values())) if value[1] == owner_user_id]
        return next((record for record in records if record.get("session_id") == session_id
                     and ((display and record.get("response", {}).get("response_kind", "task") != "conversation")
                          or (not display and record.get("shop_ids") and record.get("status") == "ok"
                              and not record.get("response", {}).get("recommendation_run_id")))), None)
