"""用户主动保存的跨会话偏好；不保存原话、位置坐标或模型上下文。"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from pydantic import BaseModel, Field

from .schemas import SearchPlan
from .storage import StateDatabase


class MemoryPreferences(BaseModel):
    category: Optional[str] = Field(default=None, max_length=30)
    area: Optional[str] = Field(default=None, max_length=30)
    max_price: Optional[int] = Field(default=None, ge=1, le=100000)
    min_score: Optional[float] = Field(default=None, ge=0, le=5)
    wants_coupon: bool = False
    wants_notes: bool = False


class UserMemoryStore:
    def __init__(self, database: StateDatabase | None = None):
        self.database = database
        self._profiles: dict[int, MemoryPreferences] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> MemoryPreferences | None:
        if self.database:
            def read():
                with self.database.connect() as conn:
                    row = conn.execute("SELECT preferences_json FROM user_memory WHERE owner_user_id=?",
                                       (user_id,)).fetchone()
                    return MemoryPreferences.model_validate_json(row[0]) if row else None
            return await asyncio.to_thread(read)
        async with self._lock:
            profile = self._profiles.get(user_id)
            return profile.model_copy(deep=True) if profile else None

    async def set(self, user_id: int, preferences: MemoryPreferences) -> None:
        if self.database:
            payload = preferences.model_dump_json()
            def write():
                with self.database.connect() as conn:
                    conn.execute("INSERT INTO user_memory VALUES (?, ?, ?) "
                                 "ON CONFLICT(owner_user_id) DO UPDATE SET "
                                 "preferences_json=excluded.preferences_json, updated_at=excluded.updated_at",
                                 (user_id, payload, time.time()))
            await asyncio.to_thread(write)
            return
        async with self._lock:
            self._profiles[user_id] = preferences.model_copy(deep=True)

    async def delete(self, user_id: int) -> None:
        if self.database:
            def delete():
                with self.database.connect() as conn:
                    conn.execute("DELETE FROM user_memory WHERE owner_user_id=?", (user_id,))
            await asyncio.to_thread(delete)
            return
        async with self._lock:
            self._profiles.pop(user_id, None)


# 只补充本轮缺省且没有明确反向表达的偏好，返回实际应用字段供界面和轨迹说明。
def apply_memory(plan: SearchPlan, memory: MemoryPreferences, message: str) -> list[str]:
    applied = []
    if not plan.stops and not plan.category and memory.category:
        plan.category = memory.category
        applied.append("常选类别")
    if not plan.area and not plan.area_unrestricted and not plan.nearby and memory.area and not re.search(r"商圈|区域|地点|周边", message):
        plan.area = memory.area
        applied.append("常用商圈")
    if (not plan.stops and plan.max_price is None and memory.max_price
            and not re.search(r"人均|预算|每人|每位|价格|\d+\s*元", message)):
        plan.max_price = memory.max_price
        applied.append("人均上限")
    if plan.min_score is None and memory.min_score is not None and "评分" not in message:
        plan.min_score = memory.min_score
        applied.append("最低评分")
    if memory.wants_coupon and not plan.wants_coupon and not re.search(r"不要.{0,3}(?:券|优惠)|不用券", message):
        plan.wants_coupon = True
        applied.append("关注优惠券")
    if memory.wants_notes and not plan.wants_notes and not re.search(r"不要.{0,3}(?:笔记|探店)|不看笔记", message):
        plan.wants_notes = True
        applied.append("查看探店笔记")
    return applied
