"""Agent 运行边界：工具白名单、调用预算、超时与可查看的轨迹。"""

from __future__ import annotations

import asyncio
import json
import re
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError


from .protocol import AgentEvent, ToolSpec
from .run_context import RunContext, HarnessError, BudgetExceeded


def mask_personal_info(value: str) -> str:
    """Remove common contact identifiers before model calls or persistent events."""
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", value)
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[邮箱]", value)
    return value


# 统一执行工具和模型请求：白名单、参数校验、局部及全局预算、取消、超时和脱敏事件。
class AgentHarness:
    def __init__(self, tools: dict[str, Callable[..., Awaitable[Any]] | ToolSpec], max_calls: int = 12,
                 timeout: float = 4.0, max_duration: float = 30.0,
                 validators: dict[str, type[BaseModel]] | None = None,
                 before_tool_call: Callable[[str, dict[str, Any]], Any] | None = None,
                 after_tool_call: Callable[[str, Any], Any] | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 max_result_bytes: int = 131072, context: RunContext | None = None,
                 parent_span_id: str | None = None):
        self.specs = {name: (tool if isinstance(tool, ToolSpec) else ToolSpec(
            name, tool, parameters=(validators or {}).get(name))) for name, tool in tools.items()}
        if any(name != spec.name for name, spec in self.specs.items()):
            raise ValueError("ToolSpec 名称与注册键不一致")
        self._tools = {name: spec.execute for name, spec in self.specs.items()}
        # 嵌套工作流沿用同一 RunContext，避免每个子阶段重新获得一份总预算。
        self.context = context or RunContext(max_calls=max_calls, max_duration=max_duration)
        self.parent_span_id = parent_span_id
        self._max_calls = max_calls
        self._timeout = timeout
        self._max_duration = max_duration
        self._started = time.monotonic()
        self._validators = {name: spec.parameters for name, spec in self.specs.items() if spec.parameters}
        self._before_tool_call = before_tool_call
        self._after_tool_call = after_tool_call
        self._on_event = on_event
        self._max_result_bytes = max_result_bytes
        self.run_id = str(uuid4()) if parent_span_id else self.context.run_id
        self.calls = 0
        self.trace: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def record(self, event: str, **details: Any) -> None:
        safe = self._redact(details)
        # 事件使用根任务的序号与计时；合并不同子阶段时仍能还原执行顺序。
        item = AgentEvent(event, self.run_id, self.context.run_id, self.parent_span_id,
                          self.context.next_sequence(),
                          round((time.monotonic() - self.context.started) * 1000), safe).to_wire()
        self.events.append(item)
        if self._on_event:
            self._on_event(item)

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = str(key)
                if name in {"input_tokens", "output_tokens"} and type(item) is int and item >= 0:
                    result[name] = item
                # 保留合法 Token 用量指标，但令牌、密钥等敏感字段不进入持久化轨迹。
                elif any(word in name.lower() for word in ("token", "authorization", "password", "secret", "api_key")):
                    result[name] = "[redacted]"
                else:
                    result[name] = cls._redact(item)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._redact(item) for item in value[:20]]
        if isinstance(value, str):
            value = mask_personal_info(value)
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[redacted]", value)
            value = re.sub(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", "Bearer [redacted]", value,
                           flags=re.IGNORECASE)
            return value[:400]
        return value if isinstance(value, (int, float, bool)) or value is None else str(type(value).__name__)

    def _remaining(self) -> float:
        # 单次超时不能超过子阶段剩余时长，也不能超过根任务剩余时长。
        remaining = min(self.context.check(), self._max_duration - (time.monotonic() - self._started))
        if remaining <= 0:
            raise BudgetExceeded("任务总时长预算已用完", "deadline_exceeded")
        return remaining

    async def model_call(self, operation: Callable[[], Awaitable[Any]], timeout: float = 12.0) -> Any:
        remaining = self._remaining()
        # 先占用共享模型次数，再发起请求；失败的请求也已经消耗了一次调用机会。
        self.context.reserve_model()
        self.record("model_start")
        try:
            result = await self.context.execute(operation, min(timeout, remaining))
            self.context.add_usage(getattr(result, "usage", {}))
            self.record("model_end", ok=True, budget_usage=getattr(result, "usage", {}))
            return result
        except asyncio.CancelledError:
            self.context.cancelled.set()
            self.record("model_end", ok=False, error="CancelledError")
            raise
        except Exception as exc:
            self.record("model_end", ok=False, error=type(exc).__name__)
            raise

    async def _hook(self, callback, *args):
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    def _check_result(self, result: Any) -> None:
        try:
            result_size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise HarnessError("工具结果无法序列化", "invalid_result") from exc
        if result_size > self._max_result_bytes:
            raise HarnessError("工具结果超出大小限制", "result_limit")

    async def call(self, name: str, *, tool_call_id: str | None = None, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise HarnessError(f"工具未授权: {name}", "not_allowed")
        if self.calls >= self._max_calls:
            raise BudgetExceeded("工具调用预算已用完", "tool_budget")
        self._remaining()
        try:
            # 先校验函数签名，再用参数模型校验类型和范围；拒绝模型拼出的多余或缺失参数。
            inspect.signature(self._tools[name]).bind(**kwargs)
            if name in self._validators:
                kwargs = self._validators[name].model_validate(kwargs).model_dump()
            if self._before_tool_call:
                decision = await self.context.execute(
                    lambda: self._hook(self._before_tool_call, name, kwargs), self._remaining())
                if decision is False:
                    raise HarnessError(f"工具执行被阻止: {name}", "blocked")
        except (TypeError, ValueError, ValidationError) as exc:
            raise HarnessError(f"工具参数不合法: {name}", "invalid_arguments") from exc
        # Async hooks may yield: recheck the local quota before reserving globally.
        if self.calls >= self._max_calls:
            raise BudgetExceeded("工具调用预算已用完", "tool_budget")
        # 异步前置钩子返回后再检查并占用额度，防止并发调用在等待期间耗尽预算。
        self.context.reserve_tool()
        self.calls += 1
        tool_call_id = tool_call_id or str(uuid4())
        start = time.monotonic()
        self.record("tool_start", tool=name, tool_call_id=tool_call_id)
        try:
            result = await self.context.execute(lambda: self._tools[name](**kwargs),
                                                min(self._timeout, self._remaining()))
            # 钩子前后都检查结果大小，避免原始结果或钩子扩展内容突破上限。
            self._check_result(result)
            if self._after_tool_call:
                result = await self.context.execute(lambda: self._hook(self._after_tool_call, name, result),
                                                    self._remaining())
            self._check_result(result)
            self.trace.append({"tool": name, "tool_call_id": tool_call_id, "span_id": self.run_id, "ok": True, "elapsed_ms": round((time.monotonic() - start) * 1000)})
            self.record("tool_end", tool=name, tool_call_id=tool_call_id, ok=True)
            return result
        except asyncio.CancelledError:
            self.context.cancelled.set()
            self.record("tool_end", tool=name, tool_call_id=tool_call_id, ok=False, error="CancelledError")
            raise
        except Exception as exc:
            self.trace.append({"tool": name, "tool_call_id": tool_call_id, "span_id": self.run_id, "ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000), "error": type(exc).__name__})
            self.record("tool_end", tool=name, tool_call_id=tool_call_id, ok=False, error=type(exc).__name__)
            raise
