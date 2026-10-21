"""短期会话状态：用于澄清和追问，不保存精确位置。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .schemas import SearchPlan
from .storage import StateDatabase


class SessionAccessError(RuntimeError):
    pass


@dataclass
class SessionState:
    id: str
    owner_user_id: int | None = None
    plan: SearchPlan | None = None
    last_shop_ids: list[int] = field(default_factory=list)
    last_shop_prices: list[int | None] = field(default_factory=list)
    revision: int = 0
    updated_at: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, ttl_seconds: int = 86400, max_sessions: int = 1000,
                 database: StateDatabase | None = None):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.database = database
        self._states: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: str | None, owner_user_id: int | None = None,
                   expected_revision: int | None = None) -> SessionState:
        if self.database:
            return await asyncio.to_thread(self._load_sqlite, session_id, owner_user_id, expected_revision)
        async with self._lock:
            now = time.monotonic()
            if expected_revision is not None:
                current = self._states.get(session_id)
                if (current is None or current.owner_user_id != owner_user_id
                        or now - current.updated_at >= self.ttl_seconds or current.revision != expected_revision):
                    raise SessionAccessError("会话已修改、删除或过期，不能恢复旧计划")
            self._states = {key: state for key, state in self._states.items()
                            if now - state.updated_at < self.ttl_seconds}
            try:
                key = str(UUID(session_id)) if session_id else None
            except ValueError:
                key = None
            state = self._states.get(key) if key else None
            if state is None:
                if len(self._states) >= self.max_sessions:
                    oldest = min(self._states, key=lambda item: self._states[item].updated_at)
                    self._states.pop(oldest)
                state = SessionState(id=str(uuid4()), owner_user_id=owner_user_id)
                self._states[state.id] = state
            else:
                if state.owner_user_id != owner_user_id:
                    raise SessionAccessError("会话不属于当前点评用户")
                # 新请求取得更新版本；较早开始的同会话请求不能覆盖其结果。
                state.revision += 1
            return SessionState(id=state.id, owner_user_id=state.owner_user_id,
                                plan=state.plan.model_copy(deep=True) if state.plan else None,
                                last_shop_ids=list(state.last_shop_ids), revision=state.revision,
                                last_shop_prices=list(state.last_shop_prices),
                                updated_at=state.updated_at)

    async def require_owned(self, session_id: str, owner_user_id: int) -> None:
        """Read ownership without claiming a new workflow revision."""
        if self.database:
            def check():
                with self.database.connect() as conn:
                    row = conn.execute("SELECT 1 FROM sessions WHERE id=? AND owner_user_id=? AND updated_at>=?",
                                       (session_id, owner_user_id, time.time() - self.ttl_seconds)).fetchone()
                    if row is None:
                        raise SessionAccessError("会话不存在或已过期")
            await asyncio.to_thread(check)
        else:
            async with self._lock:
                state = self._states.get(session_id)
                if (state is None or state.owner_user_id != owner_user_id
                        or time.monotonic() - state.updated_at >= self.ttl_seconds):
                    raise SessionAccessError("会话不存在或已过期")

    async def save(self, state: SessionState, plan: SearchPlan, shop_ids: list[int],
                   shop_prices: list[int | None]) -> bool:
        if self.database:
            def write():
                with self.database.connect() as conn:
                    cursor = conn.execute(
                        "UPDATE sessions SET plan_json=?, shop_ids_json=?, shop_prices_json=?, "
                        "revision=revision+1, updated_at=? WHERE id=? AND revision=? AND owner_user_id IS ?",
                        (plan.model_dump_json(), json.dumps(shop_ids), json.dumps(shop_prices),
                         time.time(), state.id, state.revision, state.owner_user_id))
                    return cursor.rowcount == 1
            return await asyncio.to_thread(write)
        async with self._lock:
            current = self._states.get(state.id)
            if (current is None or current.revision != state.revision
                    or current.owner_user_id != state.owner_user_id):
                return False
            current.plan = plan.model_copy(deep=True)
            current.last_shop_ids = list(shop_ids)
            current.last_shop_prices = list(shop_prices)
            current.revision += 1
            current.updated_at = time.monotonic()
            return True

    async def clear(self, session_id: str, owner_user_id: int | None = None) -> bool:
        if self.database:
            def delete():
                with self.database.connect() as conn:
                    result = conn.execute("DELETE FROM sessions WHERE id=? AND owner_user_id IS ?",
                                          (session_id, owner_user_id))
                    return result.rowcount == 1
            return await asyncio.to_thread(delete)
        async with self._lock:
            state = self._states.get(session_id)
            if state is None or state.owner_user_id != owner_user_id:
                return False
            self._states.pop(session_id)
            return True

    def _load_sqlite(self, session_id: str | None, owner_user_id: int | None,
                     expected_revision: int | None = None) -> SessionState:
        now = time.time()
        try:
            key = str(UUID(session_id)) if session_id else None
        except ValueError:
            key = None
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (key,)).fetchone() if key else None
            if expected_revision is not None:
                if (row is None or row["owner_user_id"] != owner_user_id
                        or row["updated_at"] < now - self.ttl_seconds or row["revision"] != expected_revision):
                    raise SessionAccessError("会话已修改、删除或过期，不能恢复旧计划")
            else:
                conn.execute("DELETE FROM sessions WHERE updated_at < ?", (now - self.ttl_seconds,))
                if row and row["updated_at"] < now - self.ttl_seconds:
                    row = None
            if row is None:
                count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                if count >= self.max_sessions:
                    conn.execute("DELETE FROM sessions WHERE id=(SELECT id FROM sessions ORDER BY updated_at LIMIT 1)")
                state = SessionState(id=str(uuid4()), owner_user_id=owner_user_id, updated_at=now)
                conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (state.id, owner_user_id, None, "[]", "[]", 0, now))
                return state
            if row["owner_user_id"] != owner_user_id:
                raise SessionAccessError("会话不属于当前点评用户")
            revision = row["revision"] + 1
            conn.execute("UPDATE sessions SET revision=? WHERE id=?", (revision, key))
            return SessionState(
                id=key, owner_user_id=owner_user_id,
                plan=SearchPlan.model_validate_json(row["plan_json"]) if row["plan_json"] else None,
                last_shop_ids=json.loads(row["shop_ids_json"]),
                last_shop_prices=json.loads(row["shop_prices_json"]),
                revision=revision, updated_at=row["updated_at"])

    async def list_recent(self, owner_user_id: int, limit: int = 20) -> list[dict]:
        if self.database:
            def read():
                with self.database.connect() as conn:
                    rows = conn.execute("SELECT id, plan_json, updated_at FROM sessions "
                                        "WHERE owner_user_id=? AND updated_at>=? "
                                        "ORDER BY updated_at DESC LIMIT ?",
                                        (owner_user_id, time.time() - self.ttl_seconds, limit)).fetchall()
                    return [{"session_id": row["id"], "updated_at": row["updated_at"],
                             "constraints": constraints(SearchPlan.model_validate_json(row["plan_json"]))
                             if row["plan_json"] else []} for row in rows]
            return await asyncio.to_thread(read)
        async with self._lock:
            states = sorted((state for state in self._states.values()
                             if state.owner_user_id == owner_user_id
                             and time.monotonic() - state.updated_at < self.ttl_seconds),
                            key=lambda state: state.updated_at, reverse=True)[:limit]
            return [{"session_id": state.id, "updated_at": state.updated_at,
                     "constraints": constraints(state.plan) if state.plan else []} for state in states]


def is_followup(message: str, state: SessionState, update: SearchPlan | None = None) -> bool:
    if state.plan is None:
        return False
    if re.search(r"(?:重新开始|全新计划|另起一个|新的计划)", message):
        return False
    if update and (update.followup or update.relative_price):
        return True
    if re.search(r"(?:第[一二三123][家站]|交换|对调|删|换|改|调整|保留|其他条件不变|再便宜|近一点|取消|去掉|不要|不用|不看)", message):
        return True
    if update and not update.category and not update.stops:
        return bool(update.party_size or update.total_budget or update.max_price or
                    update.min_score is not None or update.area or update.visit_time or
                    update.wants_coupon or update.wants_notes or update.area_unrestricted or update.budget_unrestricted)
    if update and not state.plan.stops and update.category == state.plan.category:
        return bool(update.party_size or update.max_price or update.min_score is not None or
                    update.area or update.visit_time or update.wants_coupon or update.wants_notes)
    return bool(re.fullmatch(r"\s*\d{1,2}\s*(?:人|位|个人)\s*", message))


# 先复制旧计划，再按用户明确修改更新字段；原计划不在合并过程中被原地修改。
def merge_followup(message: str, current: SearchPlan, update: SearchPlan,
                   last_shop_ids: list[int], last_shop_prices: list[int | None]) -> SearchPlan:
    merged = current.model_copy(deep=True)
    merged.followup = True
    if update.goal == "booking":
        merged.goal = "booking"
    merged.area_unrestricted = current.area_unrestricted or update.area_unrestricted
    merged.budget_unrestricted = current.budget_unrestricted or update.budget_unrestricted
    if update.relative_price:
        merged.relative_price = update.relative_price
    if update.max_price is not None and not update.relative_price:
        merged.min_price = None
        merged.price_reference = None
        merged.relative_price = None
    if current.stops and update.relative_price == "lower" and re.search(r"第[一二三123][家站]", message):
        # Positional cheaper replacements retain their existing per-stop validation.
        merged.relative_price = None
    if update.relative_price and not current.stops:
        # 单站相对价格使用上次实际展示价格：更贵高于最高价，更便宜低于最低价，不提高原预算。
        prices = [p for p in last_shop_prices if p is not None and 0 <= p <= 100000]
        merged.price_reference = (max(prices) if update.relative_price == "higher" else min(prices)) if prices else None
        merged.min_price = None
        if merged.price_reference is not None:
            if update.relative_price == "higher" and merged.price_reference < 100000:
                merged.min_price = merged.price_reference + 1
        merged.sort = "price"

    for field_name in ("party_size", "total_budget", "min_score", "area", "visit_time", "visit_day"):
        value = getattr(update, field_name)
        if value is not None:
            setattr(merged, field_name, value)
    # 指定站点价格与全局人均上限分开处理，避免把第二站预算套用到所有站。
    targeted_price = bool(re.search(r"第[一二三123]站.*(?:人均|每人|价格)", message))
    if update.max_price is not None and not targeted_price and not (merged.stops and update.total_budget is not None):
        merged.max_price = update.max_price
    price_change = re.search(r"(?:人均|每人|单站价格|价格)\s*(?:改成|改为|改到|调整为|提高到|降低到|设为)\s*(\d{1,5})", message)
    if price_change and not targeted_price:
        merged.max_price = int(price_change.group(1))
    budget_change = re.search(r"(?:总预算|预算)\s*(?:改成|改为|改到|调整为|提高到|降低到|设为)\s*(\d{1,6})", message)
    if not update.relative_price and ((price_change and not targeted_price) or budget_change):
        merged.min_price = None
        merged.price_reference = None
        merged.relative_price = None
    if budget_change:
        if merged.stops or "总预算" in message:
            merged.total_budget = int(budget_change.group(1))
        else:
            merged.max_price = int(budget_change.group(1))
    if re.search(r"(?:取消|去掉|不要|不设).{0,3}(?:预算|价格)(?:限制)?|(?:预算|价格)不限", message):
        merged.max_price = None
        merged.total_budget = None
    score_change = re.search(r"评分\s*(?:改成|改为|改到|提高到|降低到|设为)\s*([0-5](?:\.\d)?)", message)
    if score_change:
        merged.min_score = float(score_change.group(1))
    if re.search(r"(?:取消|去掉|不要|不设).{0,3}评分(?:限制)?|评分不限", message):
        merged.min_score = None
    area_change = re.search(r"(?:商圈|区域|地点)\s*(?:改成|改为|换成|改到|换到|设为)\s*([\u4e00-\u9fa5]{2,12})", message)
    if area_change:
        merged.area = area_change.group(1)
    if update.area_unrestricted or re.search(r"(?:取消|去掉|不要).{0,3}(?:商圈|区域|地点)(?:限制)?", message):
        merged.area = None
        merged.area_unrestricted = True
    elif update.area or area_change:
        merged.area_unrestricted = False
    if re.search(r"(?:取消|去掉|不要).{0,3}(?:到店)?时间(?:限制)?", message):
        merged.visit_time = None
    # 用户提供新站点结构时清理旧固定店铺、替换价格上限和排除列表，避免残留限制。
    if update.stops:
        merged.stops = update.stops
        merged.category = update.stops[0].category
        merged.keyword = update.stops[0].keyword
        merged.fixed_shop_ids = []
        merged.stop_price_ceilings = {}
        merged.fixed_first_shop_id = None
        merged.second_price_ceiling = None
        merged.excluded_shop_ids = []
    elif update.category and not ((update.followup or update.relative_price) and update.category == current.category and update.keyword is None):
        target = re.search(r"第([一二三123])[家站]", message)
        index = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2}.get(target.group(1)) if target else None
        if index is not None and index < len(merged.stops) and re.search(r"(?:换成|换为|改成|改为)", message):
            merged.stops[index] = update.stops[0] if update.stops else type(merged.stops[index])(
                category=update.category, keyword=update.keyword)
            if index == 0:
                merged.category = update.category
                merged.keyword = update.keyword
            merged.fixed_first_shop_id = None
            merged.second_price_ceiling = None
            merged.fixed_shop_ids = []
            merged.stop_price_ceilings = {}
        elif update.category != current.category or update.keyword != current.keyword:
            merged.category = update.category
            merged.keyword = update.keyword
            if update.category != current.category:
                merged.goal = update.goal
                merged.required_shop_ids = []
                merged.excluded_shop_ids = []
                merged.visit_day = update.visit_day
                merged.area_unrestricted = update.area_unrestricted
                merged.budget_unrestricted = update.budget_unrestricted
            merged.min_price = None
            merged.price_reference = None
            merged.relative_price = None
            merged.stops = []
            merged.fixed_first_shop_id = None
            merged.second_price_ceiling = None
            merged.fixed_shop_ids = []
            merged.stop_price_ceilings = {}
            merged.excluded_shop_ids = []
    if re.search(r'不用.*券|不要.*券|不按.*券|普通推荐', message):
        merged.required_shop_ids = []
    if update.wants_coupon:
        merged.wants_coupon = True
    if re.search(r"(?:不要|不用|取消|去掉).{0,3}(?:优惠券|券|优惠)", message):
        merged.wants_coupon = False
    if update.wants_notes:
        merged.wants_notes = True
    if re.search(r"(?:不要|不看|取消|去掉).{0,3}(?:笔记|探店|评论)", message):
        merged.wants_notes = False
    if any(word in message for word in ("便宜", "低价")):
        merged.sort = "price"
    elif "最近" in message:
        merged.nearby = True
    merged.unverified_preferences = list(dict.fromkeys(current.unverified_preferences + update.unverified_preferences))
    # Structural edits invalidate positional pins and price limits from previous replacements.
    remove = re.search(r"(?:删掉|删除|去掉|取消)第([一二三123])[站家]", message)
    swap = re.search(r"(?:交换|对调)(?:前两站|第([一二三123])站和第([一二三123])站)", message)
    positions = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2}
    if remove or swap:
        if remove and positions[remove.group(1)] < len(merged.stops):
            merged.stops.pop(positions[remove.group(1)])
        elif swap:
            left, right = (0, 1) if swap.group(1) is None else (positions[swap.group(1)], positions[swap.group(2)])
            if max(left, right) < len(merged.stops):
                merged.stops[left], merged.stops[right] = merged.stops[right], merged.stops[left]
        if merged.stops:
            merged.category, merged.keyword = merged.stops[0].category, merged.stops[0].keyword
        elif remove:
            merged.category, merged.keyword = None, None
        merged.fixed_first_shop_id = None
        merged.second_price_ceiling = None
        merged.fixed_shop_ids = []
        merged.stop_price_ceilings = {}
        merged.excluded_shop_ids = []
    limit = re.search(r"第([一二三123])站.{0,8}?(?:人均|每人|价格).{0,4}?(?:最多|不超过|上限|以内)?\s*(\d{1,5})", message)
    if limit and positions[limit.group(1)] < len(merged.stops):
        merged.stops[positions[limit.group(1)]].max_price = int(limit.group(2))
    match = re.search(r"第([一二三123])[家站]", message)
    if match and re.search(r"(?:换|不要|太贵|太远|更便宜)", message):
        index = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2}[match.group(1)]
        if index < len(last_shop_ids):
            merged.excluded_shop_ids = list(dict.fromkeys(merged.excluded_shop_ids + [last_shop_ids[index]]))
            if len(merged.stops) == 3:
                merged.stop_price_ceilings = {}
                merged.fixed_shop_ids = [shop_id if position != index else None
                                         for position, shop_id in enumerate(last_shop_ids[:3])]
                if re.search(r"(?:太贵|便宜|低价)", message) and index < len(last_shop_prices):
                    previous_price = last_shop_prices[index]
                    if previous_price is not None:
                        merged.stop_price_ceilings[index] = previous_price
            if index == 0:
                merged.fixed_first_shop_id = None
                merged.second_price_ceiling = None
            if index == 1 and len(last_shop_ids) >= 2 and len(merged.stops) == 2:
                merged.fixed_first_shop_id = last_shop_ids[0]
                if re.search(r"(?:太贵|便宜|低价)", message) and len(last_shop_prices) > 1:
                    merged.second_price_ceiling = last_shop_prices[1]
    elif re.search(r"(?:换一家|换个店|再来一家)", message) and last_shop_ids:
        merged.excluded_shop_ids = list(dict.fromkeys(merged.excluded_shop_ids + [last_shop_ids[0]]))
    elif re.search(r"(?:剩下|其余|其他家|别家|还有哪些|还有哪几家)", message) and last_shop_ids:
        # 「看看剩下几家」要把已展示过的全部排除，否则只会重复上一轮同样的三家
        merged.excluded_shop_ids = list(dict.fromkeys(merged.excluded_shop_ids + last_shop_ids))
    return merged


def constraints(plan: SearchPlan) -> list[str]:
    values = []
    if plan.stops:
        values.append(" → ".join(stop.keyword or stop.category for stop in plan.stops))
        values.extend(f"第 {index + 1} 站人均上限：¥{stop.max_price}" for index, stop in enumerate(plan.stops)
                      if stop.max_price is not None)
    elif plan.category:
        values.append(plan.keyword or plan.category)
    if plan.area:
        values.append(f"商圈：{plan.area}")
    if plan.party_size:
        values.append(f"人数：{plan.party_size}")
    if plan.total_budget:
        values.append(f"总预算：¥{plan.total_budget}")
    if plan.visit_time:
        values.append(f"到店时间：{plan.visit_time}")
    if plan.min_price:
        values.append(f"单站人均下限：¥{plan.min_price}（高于上轮最高人均 ¥{plan.price_reference}）")
    if plan.relative_price == "lower" and plan.price_reference is not None:
        values.append(f"低于上轮最低人均 ¥{plan.price_reference}")
    if plan.max_price:
        values.append(f"单站人均上限：¥{plan.max_price}")
    if plan.min_score is not None:
        values.append(f"最低评分：{plan.min_score:g}")
    if plan.required_shop_ids:
        values.append("至少一站为持有券关联店铺（券使用条件待核）")
    if plan.wants_coupon:
        values.append("关注优惠券")
    if plan.wants_notes:
        values.append("查看探店笔记")
    return values
