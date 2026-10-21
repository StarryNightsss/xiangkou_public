"""优惠券写操作：推荐内候选、短时预览、显式确认和订单回查。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from contextlib import nullcontext
from collections.abc import Callable
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

from .dianping import DianpingClient, DianpingError, DianpingRejected, DianpingUnauthorized
from .runs import RunStore


class ActionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# 交易分为准备、用户确认和结果回查；草稿状态与 既有服务 订单状态分别保存。
class VoucherActionWorkflow:
    def __init__(self, client: DianpingClient, runs: RunStore, ttl_seconds: int = 300,
                 max_actions: int = 1000):
        self.client = client
        self.runs = runs
        self.ttl_seconds = ttl_seconds
        self.max_actions = max_actions
        self.database = runs.database
        self._actions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._action_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _action_lock(self, action_id: str) -> asyncio.Lock:
        # 活跃动作各自互斥，空闲锁自动释放，避免一个账号的慢请求阻塞其他账号。
        lock = self._action_locks.get(action_id)
        if lock is None:
            lock = asyncio.Lock()
            self._action_locks[action_id] = lock
        return lock

    def _save(self, action: dict[str, Any], connection=None) -> None:
        if self.database:
            with (nullcontext(connection) if connection is not None else self.database.connect()) as conn:
                previous = conn.execute("SELECT record_json FROM voucher_actions WHERE id=?", (action["id"],)).fetchone()
                conn.execute("INSERT INTO voucher_actions VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json, "
                             "updated_at=excluded.updated_at",
                             (action["id"], action["user_id"], json.dumps(action, ensure_ascii=False), time.time()))
                if previous is None or json.loads(previous[0])["state"] != action["state"]:
                    conn.execute("INSERT INTO voucher_action_events(action_id, owner_user_id, state, created_at) "
                                 "VALUES (?, ?, ?, ?)",
                                 (action["id"], action["user_id"], action["state"], time.time()))
        else:
            self._actions[action["id"]] = action

    def _load(self, action_id: str, user_id: int) -> dict[str, Any] | None:
        if self.database:
            with self.database.connect() as conn:
                row = conn.execute("SELECT record_json FROM voucher_actions "
                                   "WHERE id=? AND owner_user_id=?", (action_id, user_id)).fetchone()
                if not row:
                    return None
                action = json.loads(row[0])
                action["fingerprint"] = tuple(action["fingerprint"])
                return action
        action = self._actions.get(action_id)
        return action if action and action["user_id"] == user_id else None

    # 外部写入前持久化提交权；进程重启看到 submitting 时先回查，不自动重发。
    def _claim_submission(self, action: dict[str, Any]) -> bool:
        """跨进程互斥领取一次外部写入权；崩溃后 submitting 按未知结果处理。"""
        if not self.database:
            current = self._actions.get(action["id"])
            if not current or current["state"] != "prepared" or current["user_id"] != action["user_id"]:
                return False
            action["state"] = "submitting"
            self._save(action)
            return True
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT record_json FROM voucher_actions WHERE id=? AND owner_user_id=?",
                               (action["id"], action["user_id"])).fetchone()
            if not row or json.loads(row[0])["state"] != "prepared":
                return False
            action["state"] = "submitting"
            conn.execute("UPDATE voucher_actions SET record_json=?, updated_at=? WHERE id=?",
                         (json.dumps(action, ensure_ascii=False), time.time(), action["id"]))
            conn.execute("INSERT INTO voucher_action_events(action_id, owner_user_id, state, created_at) "
                         "VALUES (?, ?, ?, ?)",
                         (action["id"], action["user_id"], "submitting", time.time()))
            return True

    @staticmethod
    def _voucher(vouchers: list[dict[str, Any]], voucher_id: int) -> dict[str, Any]:
        coupon = next((item for item in vouchers if item.get("id") == voucher_id), None)
        if not coupon or coupon.get("type") not in (0, 1):
            raise ActionError("优惠券不存在或已下架", 404)
        # 秒杀准备和确认都检查时间字段、可购买标志及库存；预览不预留库存。
        if coupon["type"] == 1:
            try:
                datetime.fromisoformat(str(coupon["beginTime"]))
                datetime.fromisoformat(str(coupon["endTime"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ActionError("秒杀时间不可核实，暂不能下单", 409) from exc
            if coupon.get("orderable") is False:
                raise ActionError("秒杀券不在可购买时段", 409)
            if not isinstance(coupon.get("stock"), int) or coupon["stock"] <= 0:
                raise ActionError("秒杀券库存不足", 409)
        for field in ("payValue", "actualValue"):
            if not isinstance(coupon.get(field), int) or coupon[field] < 0:
                raise ActionError("券价格不可核实，暂不能下单", 409)
        return coupon

    @staticmethod
    # 比较用户核对过的商品字段；库存不放入指纹，确认时按当前库存独立检查。
    def _fingerprint(coupon: dict[str, Any]) -> tuple:
        return tuple(coupon.get(field) for field in
                     ("id", "shopId", "type", "title", "rules", "payValue", "actualValue", "beginTime", "endTime"))

    async def prepare(self, user_id: int, run_id: str, shop_id: int, voucher_id: int,
                      on_prepared: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        # 准备操作必须有本人推荐或目录选择来源，不能直接用任意店铺/券 ID 绕过来源检查。
        run = await self.runs.get(run_id, user_id)
        if run is None or shop_id not in run["shop_ids"]:
            raise ActionError("这家店不在当前账号的推荐结果中", 404)
        shop = await self.client.get_shop(shop_id)
        if not shop or shop.get("id") != shop_id:
            raise ActionError("店铺不存在", 404)
        coupon = self._voucher(await self.client.list_vouchers(shop_id), voucher_id)
        if coupon.get("shopId") != shop_id:
            raise ActionError("优惠券不属于所选店铺", 409)
        kind = "seckill" if coupon["type"] == 1 else "ordinary"
        action_id = str(uuid4())
        action = {"id": action_id, "user_id": user_id, "shop_id": shop_id,
                  "voucher_id": voucher_id, "fingerprint": self._fingerprint(coupon),
                  "kind": kind, "created": time.time(), "state": "prepared",
                  "order_id": None, "pay_cents": coupon["payValue"]}
        action["preview"] = {"action_id": action_id, "operation": "领取免费券" if coupon["payValue"] == 0 else
                             "创建待支付订单", "shop": {"id": shop_id, "name": shop.get("name")},
                             "voucher": {"id": voucher_id, "title": coupon.get("title"), "rules": coupon.get("rules"),
                                         "pay_cents": coupon["payValue"], "face_cents": coupon["actualValue"],
                                         "stock": coupon.get("stock"), "begin_time": coupon.get("beginTime"),
                                         "end_time": coupon.get("endTime")},
                             "notice": "确认后提交点评订单；付费券仅创建待支付订单。"}
        async with self._lock:
            if on_prepared:
                # Callback commits draft + business slots under one DB transaction.
                on_prepared(action)
            else:
                if self.database:
                    with self.database.connect() as conn:
                        conn.execute("DELETE FROM voucher_actions WHERE updated_at < ?", (time.time() - 30 * 86400,))
                        conn.execute("DELETE FROM voucher_action_events WHERE created_at < ?", (time.time() - 30 * 86400,))
                        count = conn.execute("SELECT COUNT(*) FROM voucher_actions").fetchone()[0]
                        if count >= self.max_actions:
                            conn.execute("DELETE FROM voucher_actions WHERE id IN "
                                         "(SELECT id FROM voucher_actions ORDER BY updated_at LIMIT ?)",
                                         (count - self.max_actions + 1,))
                else:
                    self._actions = {key: value for key, value in self._actions.items()
                                     if time.time() - value["created"] < 86400}
                    if len(self._actions) >= self.max_actions:
                        oldest = min(self._actions, key=lambda key: self._actions[key]["created"])
                        self._actions.pop(oldest)
                self._save(action)
        pay_cents = coupon["payValue"]
        operation = "领取免费券" if pay_cents == 0 else ("秒杀券下单" if kind == "seckill" else "创建待支付订单")
        return {"action_id": action_id, "state": "prepared", "expires_in_seconds": self.ttl_seconds,
                "operation": operation, "shop": {"id": shop_id, "name": shop.get("name")},
                "voucher": {"id": voucher_id, "title": coupon.get("title"),
                            "rules": coupon.get("rules"), "pay_cents": pay_cents,
                            "face_cents": coupon["actualValue"], "stock": coupon.get("stock"),
                            "begin_time": coupon.get("beginTime"), "end_time": coupon.get("endTime")},
                "notice": ("确认后会向点评提交订单；免费券将直接领取。"
                           if pay_cents == 0 else "确认后只创建订单，不会支付；金额与库存会在提交前复核。")}

    def commit_business_state(self, states, user_id, session, record, expected_revision,
                              old_action_id=None, new_action=None):
        """Commit slots and draft transitions together; caller holds _lock."""
        if new_action and (new_action['user_id'] != user_id or new_action['state'] != 'prepared'):
            raise ActionError('草稿归属或状态不合法', 409)
        if self.database:
            if not states.database or states.database.path != self.database.path:
                raise RuntimeError('业务状态与草稿必须使用同一数据库')
            with self.database.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                old = None
                if old_action_id:
                    row = conn.execute('SELECT record_json FROM voucher_actions WHERE id=? AND owner_user_id=?',
                                       (old_action_id, user_id)).fetchone()
                    old = json.loads(row[0]) if row else None
                    if not old or old['state'] != 'prepared':
                        raise ActionError('原草稿已提交或失效，请查询订单状态', 409)
                # 同一事务内先验证并保存版本，再废弃旧草稿、保存新草稿；任一步失败全部回滚。
                revision = states.save(user_id, session, record, expected_revision, connection=conn)
                if old:
                    self._save({**old, 'state': 'abandoned'}, connection=conn)
                if new_action:
                    self._save(new_action, connection=conn)
                    # Retain the same bounded history as ordinary prepare calls.
                    conn.execute('DELETE FROM voucher_actions WHERE updated_at<? AND id<>?',
                                 (time.time()-30*86400, new_action['id']))
                    conn.execute('DELETE FROM voucher_action_events WHERE created_at<?',
                                 (time.time()-30*86400,))
                    count = conn.execute('SELECT COUNT(*) FROM voucher_actions').fetchone()[0]
                    if count > self.max_actions:
                        conn.execute('DELETE FROM voucher_actions WHERE id IN '
                                     '(SELECT id FROM voucher_actions WHERE id<>? ORDER BY updated_at LIMIT ?)',
                                     (new_action['id'], count-self.max_actions))
                return revision
        if states.database:
            raise RuntimeError('业务状态与草稿必须使用同一存储')
        old = self._load(old_action_id, user_id) if old_action_id else None
        if old_action_id and (not old or old['state'] != 'prepared'):
            raise ActionError('原草稿已提交或失效，请查询订单状态', 409)
        # No await: validation and all in-memory updates form one critical section.
        revision = states.save(user_id, session, record, expected_revision)
        if old:
            self._save({**old, 'state': 'abandoned'})
        if new_action:
            self._save(new_action)
            for key in list(self._actions):
                if key != new_action['id'] and time.time()-self._actions[key]['created'] >= 86400:
                    self._actions.pop(key)
            while len(self._actions) > self.max_actions:
                oldest = min((key for key in self._actions if key != new_action['id']),
                             key=lambda key: self._actions[key]['created'])
                self._actions.pop(oldest)
        return revision

    async def update_business_state(self, states, user_id, session, record, expected_revision,
                                    old_action_id=None):
        async with self._lock:
            return self.commit_business_state(states, user_id, session, record, expected_revision,
                                              old_action_id=old_action_id)

    async def _owned(self, action_id: str, user_id: int) -> dict[str, Any]:
        action = self._load(action_id, user_id)
        if action is None:
            raise ActionError("操作不存在", 404)
        return action

    # 展示预览不执行交易，且仍要检查账号、草稿阶段与期限。
    async def preview(self, action_id: str, user_id: int) -> dict[str, Any]:
        action = await self._owned(action_id, user_id)
        if action["state"] != "prepared" or time.time() - action["created"] > self.ttl_seconds:
            raise ActionError("草稿已提交或过期，请重新预览", 409)
        if not action.get("preview"):
            raise ActionError("旧版草稿没有可恢复的确认详情，请重新预览", 409)
        return {**action["preview"], "expires_in_seconds": max(0, int(self.ttl_seconds - time.time() + action["created"]))}

    async def abandon(self, action_id: str, user_id: int) -> None:
        """Invalidate an unsubmitted draft; never cancel a submitted order implicitly."""
        async with self._action_lock(action_id):
            action = await self._owned(action_id, user_id)
            if self.database:
                with self.database.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT record_json FROM voucher_actions WHERE id=? AND owner_user_id=?',
                                       (action_id, user_id)).fetchone()
                    current = json.loads(row[0]) if row else None
                    if not current or current['state'] != 'prepared':
                        raise ActionError('草稿已提交或失效，请查询订单状态', 409)
                    current['state'] = 'abandoned'
                    conn.execute('UPDATE voucher_actions SET record_json=?,updated_at=? WHERE id=?',
                                 (json.dumps(current, ensure_ascii=False), time.time(), action_id))
                    conn.execute('INSERT INTO voucher_action_events(action_id,owner_user_id,state,created_at) VALUES (?,?,?,?)',
                                 (action_id, user_id, 'abandoned', time.time()))
            else:
                if action['state'] != 'prepared':
                    raise ActionError('草稿已提交或失效，请查询订单状态', 409)
                action['state'] = 'abandoned'
                self._save(action)

    async def prepare_cancel(self, user_id: int, order_id: str, token: str) -> dict[str, Any]:
        order = await self.client.get_voucher_order(order_id, token)
        if order["status"] != 1:
            raise ActionError("订单不是待支付状态", 409)
        if order.get("voucher_type") != 0 or not isinstance(order.get("pay_cents"), int) or order["pay_cents"] <= 0:
            raise ActionError("仅支持取消普通付费券订单", 409)
        action_id = str(uuid4())
        preview = {"action_id": action_id, "operation": "取消待支付订单", "order_id": order_id,
                   "notice": "确认后取消此订单；仅支持普通付费券，不涉及支付或退款。",
                   "expires_in_seconds": self.ttl_seconds}
        action = {"id": action_id, "user_id": user_id, "shop_id": None, "voucher_id": order["voucher_id"],
                  "fingerprint": (), "kind": "cancel", "created": time.time(), "state": "prepared",
                  "order_id": order_id, "pay_cents": order["pay_cents"], "preview": preview}
        self._save(action)
        return preview

    # 仅待确认草稿可以执行一次；商品变化或过期要求重新准备，未知结果要求回查。
    async def confirm(self, action_id: str, user_id: int, token: str) -> dict[str, Any]:
        async with self._action_lock(action_id):
            action = await self._owned(action_id, user_id)
            if action["state"] in ("submitted", "cancelled"):
                return self._result(action)
            if action["state"] == "submitting":
                action["state"] = "uncertain"
                self._save(action)
            if action["state"] != "prepared":
                raise ActionError("该操作不能再次提交，请先查询订单状态", 409)
            if time.time() - action["created"] > self.ttl_seconds:
                action["state"] = "expired"
                self._save(action)
                raise ActionError("预览已过期，请重新查看券详情", 409)
            if action["kind"] == "cancel":
                return await self._confirm_cancel(action, token)
            # 只读预检失败时保留 prepared；仅实际尝试写入后的异常才是结果未知。
            coupon = self._voucher(await self.client.list_vouchers(action["shop_id"]), action["voucher_id"])
            if self._fingerprint(coupon) != action["fingerprint"]:
                action["state"] = "stale"
                self._save(action)
                raise ActionError("券信息已变化，请重新预览后确认", 409)
            if not self._claim_submission(action):
                raise ActionError("操作已由其他请求提交，请查询订单状态", 409)
            try:
                order_id = await self.client.create_voucher_order(action["voucher_id"], action["kind"], token)
            except DianpingRejected as exc:
                # 重复提示可能对应已有订单，归为未知待查；其余明确业务拒绝记录为 rejected。
                action["state"] = "uncertain" if "重复" in str(exc) else "rejected"
                self._save(action)
                raise ActionError(str(exc), 409) from exc
            except DianpingUnauthorized:
                action["state"] = "prepared"
                self._save(action)
                raise
            except DianpingError as exc:
                action["state"] = "uncertain"
                self._save(action)
                raise ActionError("提交结果未确认；请查询状态，勿再次提交", 503) from exc
            action["order_id"] = order_id
            action["state"] = "submitted"
            self._save(action)
            return self._result(action)

    # 取消也要确认前重读订单；用户可能已在另一页面支付，不能按旧预览直接更新。
    async def _confirm_cancel(self, action: dict[str, Any], token: str) -> dict[str, Any]:
        order = await self.client.get_voucher_order(action["order_id"], token)
        if order["status"] != 1 or order["voucher_id"] != action["voucher_id"]:
            raise ActionError("订单状态已变化，请重新核对", 409)
        if not self._claim_submission(action):
            raise ActionError("操作正在处理，请查询状态", 409)
        try:
            await self.client.cancel_voucher_order(action["order_id"], token)
        except DianpingRejected as exc:
            action["state"] = "rejected"
            self._save(action)
            raise ActionError(str(exc), 409) from exc
        except DianpingError as exc:
            action["state"] = "uncertain"
            self._save(action)
            raise ActionError("取消结果未确认，请查询订单状态", 503) from exc
        action["state"], action["order_status"] = "submitted", 4
        self._save(action)
        return {**self._result(action), "order_status": 4}

    # 只查询并更新动作结果；无订单号时按本人券订单查，秒杀尚未落库不等于失败。
    async def status(self, action_id: str, user_id: int, token: str) -> dict[str, Any]:
        async with self._action_lock(action_id):
            action = await self._owned(action_id, user_id)
            if action["state"] in ("submitted", "uncertain", "submitting"):
                if action["state"] == "submitting":
                    action["state"] = "uncertain"
                    self._save(action)
                try:
                    order = (await self.client.get_voucher_order(action["order_id"], token)
                             if action["order_id"] else
                             await self.client.find_voucher_order(action["voucher_id"], token))
                except DianpingRejected:
                    order = None  # 秒杀订单可能尚未异步落库。
                if order is None and action["order_id"]:
                    # 秒杀提交后被接受的订单号可能还没落库：按本人该券再查一次。
                    # 只有拿到同一个订单号才采纳，历史同券订单不能当成本次结果。
                    try:
                        candidate = await self.client.find_voucher_order(action["voucher_id"], token)
                    except DianpingRejected:
                        candidate = None
                    if candidate and candidate["order_id"] == action["order_id"]:
                        order = candidate
                if order:
                    changed = (action["order_id"] != order["order_id"] or action["state"] != "submitted"
                               or action.get("order_status") != order["status"])
                    action["order_id"] = order["order_id"]
                    action["order_status"] = order["status"]
                    action["state"] = ("submitted" if action["kind"] != "cancel" or order["status"] == 4 else "uncertain")
                    if changed:
                        self._save(action)
                    return {**self._result(action), "order_status": order["status"]}
            return self._result(action)

    async def cancel_order(self, action_id: str, user_id: int, token: str) -> dict[str, Any]:
        """Legacy explicit-confirm button shares the durable cancellation state machine."""
        async with self._action_lock(action_id):
            source = await self._owned(action_id, user_id)
            linked_id = source.get('cancel_action_id')
            if not linked_id:
                if source['state'] != 'submitted' or not source['order_id']:
                    raise ActionError('请先确认订单号和状态', 409)
                if source['kind'] != 'ordinary' or source.get('pay_cents', 0) <= 0:
                    raise ActionError('仅支持取消未支付的普通券订单', 409)
                # Validate current facts before atomically linking one cancellation action.
                order = await self.client.get_voucher_order(source['order_id'], token)
                if (order['status'] != 1 or order['voucher_id'] != source['voucher_id']
                        or order.get('voucher_type') != 0 or order.get('pay_cents', 0) <= 0):
                    raise ActionError('订单状态或券信息已变化，请重新核对', 409)
                linked_id = str(uuid4())
                child = {'id': linked_id, 'user_id': user_id, 'shop_id': None,
                         'voucher_id': source['voucher_id'], 'fingerprint': (), 'kind': 'cancel',
                         'created': time.time(), 'state': 'prepared', 'order_id': source['order_id'],
                         'pay_cents': source['pay_cents']}
                if self.database:
                    with self.database.connect() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        row = conn.execute('SELECT record_json FROM voucher_actions WHERE id=? AND owner_user_id=?',
                                           (action_id, user_id)).fetchone()
                        current = json.loads(row[0])
                        if current.get('cancel_action_id'):
                            linked_id = current['cancel_action_id']
                        else:
                            current['cancel_action_id'] = linked_id
                            now = time.time()
                            conn.execute('INSERT INTO voucher_actions VALUES (?,?,?,?)',
                                         (linked_id,user_id,json.dumps(child,ensure_ascii=False),now))
                            conn.execute('INSERT INTO voucher_action_events(action_id,owner_user_id,state,created_at) VALUES (?,?,?,?)',
                                         (linked_id,user_id,'prepared',now))
                            conn.execute('UPDATE voucher_actions SET record_json=?,updated_at=? WHERE id=?',
                                         (json.dumps(current,ensure_ascii=False),now,action_id))
                else:
                    self._save(child)
                    source['cancel_action_id'] = linked_id
                    self._save(source)
        linked = await self._owned(linked_id, user_id)
        if linked['state'] == 'prepared':
            return await self.confirm(linked_id, user_id, token)
        return await self.status(linked_id, user_id, token)

    @staticmethod
    def _result(action: dict[str, Any]) -> dict[str, Any]:
        return {"action_id": action["id"], "state": action["state"],
                "order_id": action["order_id"], "voucher_id": action["voucher_id"],
                "free": action.get("pay_cents") == 0,
                "kind": action["kind"],
                **({"request_state": action["request_state"]} if action.get("request_state") else {}),
                **({"order_status": action["order_status"]} if action.get("order_status") is not None else {}),
                "cancellable": action.get("kind") == "ordinary" and action.get("pay_cents", 0) > 0}
