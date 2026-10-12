"""OpenAI-compatible Chat Completions over SSE; no implicit request retries."""

import copy
import json
import os
from collections.abc import Callable

import httpx

from miniagent.contracts import ModelReply
from miniagent.kernel import Context


class OpenAIModel:
    def __init__(self, client: httpx.AsyncClient, model: str):
        self.client, self.model = client, model

    def prepare(self, messages: list[dict], tools: list[dict]) -> dict:
        request = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            request["tools"] = tools
        return request

    async def complete(self, request: dict, on_text: Callable[[str], None]) -> ModelReply:
        message: dict = {"role": "assistant", "content": None}
        calls: dict[int, dict] = {}
        finish_reason, usage = None, None
        async with self.client.stream("POST", "chat/completions", json=request) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue
                chunk = json.loads(data)
                if "error" in chunk:
                    raise ValueError(f"Model API error: {chunk['error']}")
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    for key, value in delta.items():
                        if key == "tool_calls":
                            for part in value or []:
                                index = part["index"]
                                part = {k: v for k, v in part.items() if k != "index"}
                                self._merge(calls.setdefault(index, {}), part)
                        elif value is not None:
                            if key == "role":
                                message[key] = value
                            else:
                                self._merge(message, {key: value})
                            if key == "content" and isinstance(value, str):
                                on_text(value)
                    finish_reason = choice.get("finish_reason") or finish_reason
        if finish_reason is None:
            raise ValueError("Model stream ended without a finish reason")
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return ModelReply(message, finish_reason, usage)

    @classmethod
    def _merge(cls, target: dict, delta: dict) -> None:
        # Preserve extension fields such as reasoning_content, not just visible text.
        for key, value in delta.items():
            if isinstance(value, dict):
                cls._merge(target.setdefault(key, {}), value)
            elif isinstance(value, str):
                target[key] = (target.get(key) or "") + value
            elif isinstance(value, list):
                target.setdefault(key, []).extend(copy.deepcopy(value))
            elif value is not None:
                target[key] = value


class OpenAIModelPlugin:
    name = "model"
    requires = ()

    def __init__(
        self, model: str | None = None, base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY", timeout: float = 60,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.timeout = timeout

    async def setup(self, ctx: Context) -> None:
        model = self.model or os.environ.get("MINIAGENT_MODEL")
        key = os.environ.get(self.api_key_env)
        if not model or not key:
            raise ValueError(f"Set MINIAGENT_MODEL and {self.api_key_env}, or configure model")
        base_url = self.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.timeout,
        )
        ctx.defer(client.aclose)
        ctx.provide("model", OpenAIModel(client, model))
