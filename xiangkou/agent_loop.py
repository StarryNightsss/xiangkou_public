"""Reusable model/tool loop; domain authorization stays in the phase policy."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Any

from .harness import AgentHarness
from .llm import LLMProvider
from .errors import AgentError, classify_error
from .protocol import AgentMessage, ToolCall, ToolResult, ToolSpec


@dataclass(frozen=True)
# 阶段策略与通用执行循环分开：调用方决定工具范围、写权限和结果校验规则。
class LoopPolicy:
    max_actions: int = 6
    max_context_chars: int = 10000
    max_output_tokens: int = 600
    allow_writes: bool = False
    max_read_retries: int = 1
    authorize: Callable[[ToolCall], str | None] | None = None
    dedup_key: Callable[[ToolCall], Any] | None = None
    observe: Callable[[ToolCall, Any], None] | None = None
    finish_turn: Callable[[list[AgentMessage]], bool] | None = None
    validate_final: Callable[[str], str | None] | None = None

    def __post_init__(self):
        if min(self.max_actions, self.max_context_chars, self.max_output_tokens) <= 0:
            raise ValueError('Agent 循环预算必须为正数')
        if not 0 <= self.max_read_retries <= 2:
            raise ValueError('只读重试次数必须在 0 到 2 之间')


@dataclass
class LoopResult:
    messages: list[AgentMessage]
    actions: int
    failed: bool
    stop_reason: str
    errors: list[AgentError] = field(default_factory=list)


# 模型提出工具调用，循环负责授权、执行、回传观察；领域事实仍由工作流校验。
class AgentLoop:
    def __init__(self, provider: LLMProvider, harness: AgentHarness):
        self.provider, self.harness = provider, harness

    async def run(self, system: str, messages: list[AgentMessage], tools: list[ToolSpec],
                  policy: LoopPolicy, declarations: list[dict] | None = None) -> LoopResult:
        messages = list(messages)
        # 本阶段白名单与 Harness 注册表都必须允许同一个工具，模型声明本身不构成授权。
        allowed = {tool.name: tool for tool in tools}
        used = set()
        actions, failed, reason = 0, False, 'model_finished'
        errors = []
        turn = 0
        # Model/tool continuation has no fixed turn limit. RunContext owns resource limits.
        while True:
            turn += 1
            self.harness.record('agent_turn_start', turn=turn)
            wire = [message.to_wire() for message in messages]
            # Preserve the tool-call/result pairing; trim oldest observations only.
            # 只缩短旧工具结果的内容，不删除调用与结果的配对，避免破坏供应商消息协议。
            for message in wire:
                if len(json.dumps(wire, ensure_ascii=False, default=str)) <= policy.max_context_chars:
                    break
                if message['role'] == 'tool':
                    message['content'] = '{"truncated":true}'
                    self.harness.record('model_context_compacted')
            if len(json.dumps(wire, ensure_ascii=False, default=str)) > policy.max_context_chars:
                reason = 'context_budget'
                break
            try:
                completion = await self.harness.model_call(lambda: self.provider.complete(
                    system, wire, # Exhausting phase actions still allows a model turn to explain the results.
                    tools=(declarations if declarations is not None else [tool.declaration() for tool in tools])
                          if actions < policy.max_actions else [],
                    max_tokens=policy.max_output_tokens), timeout=self.provider.config.timeout)
            except Exception as exc:
                error = replace(classify_error(exc), stage='model')
                if isinstance(exc, (ValueError, KeyError, TypeError)):
                    error = AgentError('model_protocol_error', terminal=True, stage='model')
                errors.append(error)
                failed, reason = True, error.code
                self.harness.record('agent_error', **error.to_wire())
                self.harness.record('agent_turn_end', turn=turn)
                break
            self.harness.record('model_response', provider=self.provider.config.provider,
                                model=self.provider.config.model, usage=completion.usage,
                                tool_calls=len(completion.tool_calls))
            messages.append(AgentMessage('assistant', completion.text, completion.tool_calls))
            # 模型停止调用工具后，先由领域策略检查回复是否有业务证据；不满足则追加纠正要求。
            if not completion.tool_calls:
                correction = policy.validate_final(completion.text) if policy.validate_final else None
                if correction:
                    self.harness.record('agent_final_rejected', reason='business_evidence_required')
                    messages.append(AgentMessage('user', correction))
                    self.harness.record('agent_turn_end', turn=turn)
                    continue
                reason = 'model_finished'
                self.harness.record('agent_turn_end', turn=turn)
                break
            # 调用 ID 用于关联观察结果；重复或空 ID 会让后续消息无法可靠配对，直接终止本轮。
            ids = [call.id for call in completion.tool_calls]
            if len(set(ids)) != len(ids) or any(not call_id for call_id in ids):
                error = AgentError('model_protocol_error', terminal=True, stage='model')
                errors.append(error)
                failed, reason = True, error.code
                self.harness.record('agent_error', **error.to_wire())
                self.harness.record('agent_turn_end', turn=turn)
                break
            terminal_error = None
            for call in completion.tool_calls:
                # 一批调用中发生终止性错误后，仍为剩余调用补上错误结果，但不执行它们。
                if terminal_error:
                    messages.append(ToolResult(call.id, '本轮已终止，未执行此工具', True).message())
                    continue
                denial = '工具未授权' if call.name not in allowed or call.name not in self.harness.specs else None
                if not denial and not (allowed[call.name].read_only and self.harness.specs[call.name].read_only) and not policy.allow_writes:
                    denial = '当前阶段不允许写操作'
                if not denial and policy.authorize:
                    denial = policy.authorize(call)
                # 去重键可由领域策略收窄；例如店铺补查按“工具 + 店铺 ID”防止反复消耗预算。
                key = policy.dedup_key(call) if policy.dedup_key else (
                    call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                denial_code = 'not_allowed'
                if not denial and key in used:
                    denial, denial_code = '重复调用', 'duplicate_call'
                elif not denial and actions >= policy.max_actions:
                    denial, denial_code = '调用预算已用完', 'action_budget'
                if denial:
                    self.harness.record('agent_rejected', tool=call.name, reason=denial_code)
                    messages.append(ToolResult(call.id, denial, True).message())
                    continue
                used.add(key)
                actions += 1
                self.harness.record('agent_decision', mode='model', tool=call.name,
                                    shop_id=call.arguments.get('shop_id'))
                result = None
                succeeded = False
                # 仅对策略和注册表都标记为只读的工具重试；写请求超时可能已生效，不能盲目重发。
                for attempt in range(policy.max_read_retries + 1):
                    try:
                        result = await self.harness.call(call.name, tool_call_id=call.id, **call.arguments)
                        succeeded = True
                        break
                    except Exception as exc:
                        error = classify_error(exc)
                        errors.append(error)
                        self.harness.record('agent_error', tool=call.name, tool_call_id=call.id,
                                            **error.to_wire())
                        if (error.retryable and allowed[call.name].read_only and self.harness.specs[call.name].read_only
                                and attempt < policy.max_read_retries):
                            self.harness.record('tool_retry', tool=call.name, tool_call_id=call.id,
                                                attempt=attempt + 1, code=error.code)
                            continue
                        failed = True
                        messages.append(ToolResult(call.id, error.code, True).message())
                        if error.terminal:
                            terminal_error, reason = error, error.code
                        break
                if not succeeded:
                    continue
                try:
                    spec = allowed[call.name]
                    # 给模型的是裁剪观察；observe 接收原始结果，供工作流使用完整字段复核。
                    content, truncated = spec.project(result) if spec.project else (
                        json.dumps(result, ensure_ascii=False, default=str), False)
                    if policy.observe:
                        policy.observe(call, result)
                    if truncated:
                        self.harness.record('model_observation_limited', tool=call.name,
                                            shop_id=call.arguments.get('shop_id'))
                    messages.append(ToolResult(call.id, content).message())
                except Exception:
                    # Projection/observation bugs are application failures, never retried.
                    error = AgentError('internal_error', terminal=True, stage='observation')
                    errors.append(error)
                    failed, terminal_error, reason = True, error, error.code
                    self.harness.record('agent_error', tool=call.name, tool_call_id=call.id,
                                        **error.to_wire())
                    messages.append(ToolResult(call.id, error.code, True).message())
            self.harness.record('agent_turn_end', turn=turn)
            if terminal_error:
                break
            # 领域阶段可以主动结束循环；否则继续由模型和共享预算决定何时停止。
            if policy.finish_turn and policy.finish_turn(messages):
                reason = 'phase_finished'
                break
        self.harness.record('agent_loop_end', actions=actions, reason=reason, failed=failed,
                            errors=[error.to_wire() for error in errors])
        return LoopResult(messages, actions, failed, reason, errors)
