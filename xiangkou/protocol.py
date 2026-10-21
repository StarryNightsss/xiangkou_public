"""Provider-independent messages and a single source of truth for tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False

    def message(self) -> AgentMessage:
        return AgentMessage('tool', self.content, tool_call_id=self.tool_call_id, is_error=self.is_error)


@dataclass(frozen=True)
class AgentMessage:
    role: Literal['user', 'assistant', 'tool']
    content: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False

    def to_wire(self) -> dict[str, Any]:
        item: dict[str, Any] = {'role': self.role, 'content': self.content}
        if self.tool_calls:
            item['tool_calls'] = [dict(id=call.id, name=call.name, arguments=call.arguments) for call in self.tool_calls]
        if self.tool_call_id is not None:
            item.update(tool_call_id=self.tool_call_id, is_error=self.is_error)
        return item


@dataclass(frozen=True)
# 工具的执行函数、参数 Schema、读写属性和观察裁剪在同一处定义，供声明与实际执行共同使用。
class ToolSpec:
    name: str
    execute: Callable[..., Awaitable[Any]]
    description: str = ''
    parameters: type[BaseModel] | None = None
    read_only: bool = True
    project: Callable[[Any], tuple[str, bool]] | None = None

    def declaration(self) -> dict[str, Any]:
        schema = self.parameters.model_json_schema() if self.parameters else {
            'type': 'object', 'properties': {}, 'additionalProperties': False}
        return dict(name=self.name, description=self.description, parameters=schema)


@dataclass(frozen=True)
# 根任务、子阶段和全局序号一起输出，前端及持久化记录可按同一时间线查看。
class AgentEvent:
    event: str
    run_id: str
    root_run_id: str
    parent_span_id: str | None
    sequence: int
    elapsed_ms: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {**self.details, 'event': self.event, 'run_id': self.run_id,
                'root_run_id': self.root_run_id, 'span_id': self.run_id,
                'parent_span_id': self.parent_span_id, 'sequence': self.sequence,
                'elapsed_ms': self.elapsed_ms}
