"""A task-wide budget and cancellation signal shared by workflow spans."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4


class HarnessError(RuntimeError):
    def __init__(self, message: str, code: str = 'harness_error'):
        super().__init__(message)
        self.code = code


class BudgetExceeded(HarnessError):
    pass


@dataclass
# 同一用户任务的共享额度与取消信号；对话、规划和补查可创建不同 span，但共享此对象。
class RunContext:
    max_calls: int = 20
    max_duration: float = 30.0
    max_model_calls: int = 12
    max_tokens: int = 16000
    run_id: str = field(default_factory=lambda: str(uuid4()))
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    sequence: int = 0
    outcomes: list[dict] = field(default_factory=list)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    # 使用单调时钟计算剩余时间，避免系统时间调整影响超时判断。
    def check(self) -> float:
        if self.cancelled.is_set():
            raise asyncio.CancelledError()
        remaining = self.max_duration - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded('任务总时长预算已用完', 'deadline_exceeded')
        return remaining

    def reserve_tool(self) -> None:
        self.check()
        if self.calls >= self.max_calls:
            raise BudgetExceeded('工具调用预算已用完', 'tool_budget')
        # No await between check and reservation: safe across asyncio tasks.
        self.calls += 1

    def reserve_model(self) -> None:
        self.check()
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded('模型调用预算已用完', 'model_budget')
        if self.input_tokens + self.output_tokens >= self.max_tokens:
            raise BudgetExceeded('模型 Token 预算已用完', 'token_budget')
        self.model_calls += 1

    # 按供应商返回的合法用量累计；调用前检查预算，已返回的实际用量可能超过阈值。
    def add_usage(self, usage: dict) -> None:
        for name in ('input_tokens', 'output_tokens'):
            value = usage.get(name, 0)
            if type(value) is int and value >= 0:
                setattr(self, name, getattr(self, name) + value)

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    # 同时等待业务请求和取消信号；任一超时或取消都要清理后台任务。
    async def execute(self, operation, timeout: float):
        remaining = self.check()
        task = asyncio.create_task(operation())
        cancelled = asyncio.create_task(self.cancelled.wait())
        try:
            done, _ = await asyncio.wait({task, cancelled}, timeout=min(timeout, remaining),
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                raise asyncio.CancelledError()
            if task not in done:
                raise TimeoutError('执行超时')
            return await task
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            for pending in (task, cancelled):
                if not pending.done():
                    pending.cancel()
            # 等待子任务退出并收集异常，避免取消后遗留未回收的协程。
            await asyncio.gather(task, cancelled, return_exceptions=True)
