"""统一的模型消息协议与供应商适配层。业务代码不依赖厂商响应结构。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .protocol import ToolCall


DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}
KEY_VARIABLES = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY", "openai-compatible": "XIANGKOU_LLM_API_KEY",
}


@dataclass(frozen=True)
# 统一模型文本、工具调用与用量，业务层无需识别不同供应商响应结构。
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "anthropic"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout: float = 12.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.getenv("XIANGKOU_LLM_PROVIDER", "anthropic").strip().lower()
        if provider not in DEFAULT_BASE_URLS and provider != "openai-compatible":
            raise ValueError(f"不支持的 XIANGKOU_LLM_PROVIDER: {provider}")
        key = (os.getenv("XIANGKOU_LLM_API_KEY") or os.getenv(KEY_VARIABLES[provider], "")).strip()
        model = (os.getenv("XIANGKOU_LLM_MODEL") or
                 (os.getenv("ANTHROPIC_MODEL", "") if provider == "anthropic" else "")).strip()
        base_url = (os.getenv("XIANGKOU_LLM_BASE_URL") or
                    (os.getenv("ANTHROPIC_BASE_URL", "") if provider == "anthropic" else "") or
                    DEFAULT_BASE_URLS.get(provider, "")).strip().rstrip("/")
        if key and not model:
            raise ValueError("已配置模型 API Key，请同时设置 XIANGKOU_LLM_MODEL")
        if key and not base_url:
            raise ValueError("自定义 OpenAI 兼容供应商需要 XIANGKOU_LLM_BASE_URL")
        timeout = float(os.getenv("XIANGKOU_LLM_TIMEOUT", "12"))
        if not 1 <= timeout <= 30:
            raise ValueError("XIANGKOU_LLM_TIMEOUT 必须在 1 到 30 秒之间")
        return cls(provider=provider, model=model, api_key=key, base_url=base_url, timeout=timeout)


# 只做供应商协议适配和 HTTP 请求；重试、预算和工具授权由上层执行体系控制。
class LLMProvider:
    def __init__(self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        if config.provider not in DEFAULT_BASE_URLS and config.provider != "openai-compatible":
            raise ValueError(f"不支持的模型供应商: {config.provider}")
        self.config = config
        self.transport = transport
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key and self.config.model)

    async def complete(self, system: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None = None, max_tokens: int = 500) -> Completion:
        if not self.enabled:
            raise ValueError("模型未配置")
        # Anthropic 使用原生消息协议，其余供应商走 OpenAI 兼容协议，返回统一 Completion。
        if self.config.provider == "anthropic":
            return await self._anthropic(system, messages, tools or [], max_tokens)
        return await self._chat_completions(system, messages, tools or [], max_tokens)

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("模型 Provider 已关闭")
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("模型 Provider 已关闭")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout, transport=self.transport)
        response = await self._client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("模型响应不是对象")
        return body

    async def _anthropic(self, system: str, messages: list[dict[str, Any]],
                         tools: list[dict[str, Any]], max_tokens: int) -> Completion:
        wire: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                result = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                          "content": message["content"], "is_error": bool(message.get("is_error"))}
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(result)
                else:
                    wire.append({"role": "user", "content": [result]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                blocks.extend({"type": "tool_use", "id": call["id"], "name": call["name"],
                               "input": call["arguments"]} for call in message.get("tool_calls", []))
                wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": "user", "content": message["content"]})
        payload: dict[str, Any] = {"model": self.config.model, "max_tokens": max_tokens,
                                   "system": system, "messages": wire}
        if tools:
            payload["tools"] = [{"name": tool["name"], "description": tool["description"],
                                 "input_schema": tool["parameters"]} for tool in tools]
        body = await self._post(f"{self.config.base_url}/v1/messages",
                                {"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"}, payload)
        blocks = body["content"]
        calls = []
        for item in blocks:
            if item.get("type") != "tool_use":
                continue
            arguments = item.get("input") or {}
            if not isinstance(arguments, dict):
                raise ValueError("模型工具参数不是对象")
            calls.append(ToolCall(str(item["id"]), str(item["name"]), arguments))
        text = "\n".join(item.get("text", "") for item in blocks if item.get("type") == "text")
        usage = body.get("usage") or {}
        return Completion(text=text, tool_calls=calls,
                          usage={"input_tokens": usage.get("input_tokens", 0),
                                 "output_tokens": usage.get("output_tokens", 0)})

    async def _chat_completions(self, system: str, messages: list[dict[str, Any]],
                                tools: list[dict[str, Any]], max_tokens: int) -> Completion:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
                if message.get("tool_calls"):
                    item["tool_calls"] = [{"id": call["id"], "type": "function",
                                           "function": {"name": call["name"],
                                                        "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
                                          for call in message["tool_calls"]]
                wire.append(item)
            elif role == "tool":
                wire.append({"role": "tool", "tool_call_id": message["tool_call_id"],
                             "content": message["content"]})
            else:
                wire.append({"role": "user", "content": message["content"]})
        token_field = "max_completion_tokens" if self.config.provider in {"openai", "kimi"} else "max_tokens"
        payload: dict[str, Any] = {"model": self.config.model, "messages": wire,
                                   token_field: max_tokens}
        if self.config.provider == "deepseek":
            # 巷口 的规划和只读补查需要短结构化输出；关闭默认思考模式，
            # 避免推理内容耗尽输出预算，也无需回传 reasoning_content。
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if self.config.provider == "gemini":
            headers["x-goog-api-client"] = "xiangkou-oai/1.0"
        body = await self._post(f"{self.config.base_url}/chat/completions", headers, payload)
        message = body["choices"][0]["message"]
        calls = []
        for item in message.get("tool_calls") or []:
            function = item["function"]
            arguments = function.get("arguments") or "{}"
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(parsed, dict):
                raise ValueError("模型工具参数不是对象")
            calls.append(ToolCall(str(item["id"]), str(function["name"]), parsed))
        usage = body.get("usage") or {}
        return Completion(text=message.get("content") or "", tool_calls=calls,
                          usage={"input_tokens": usage.get("prompt_tokens", 0),
                                 "output_tokens": usage.get("completion_tokens", 0)})
