"""The default model/tool loop, implemented as an ordinary plugin."""

import asyncio
import copy
import json

from miniagent.contracts import Model, RunResult, Session, Tools
from miniagent.kernel import Context


class ToolLoop:
    def __init__(
        self, model: Model, tools: Tools, session: Session, ctx: Context, max_steps: int
    ):
        self.model, self.tools, self.session = model, tools, session
        self.ctx = ctx
        self.max_steps = max_steps
        self._lock = asyncio.Lock()

    async def run(self, prompt: str) -> RunResult:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        if self._lock.locked():
            raise RuntimeError("A run is already active")
        async with self._lock:
            messages = self.session.history()
            result = RunResult("failed", error="Run interrupted")
            self.session.append("run_started", {"max_steps": self.max_steps})

            def add(message: dict) -> None:
                self.session.append("message", message)
                messages.append(copy.deepcopy(message))

            try:
                add({"role": "user", "content": prompt})
                for step in range(1, self.max_steps + 1):
                    # Snapshot after adaptation: this is the actual JSON request.
                    request = self.model.prepare(
                        copy.deepcopy(messages), self.tools.definitions()
                    )
                    request = json.loads(json.dumps(request, allow_nan=False))
                    self.session.append("request", request)
                    reply = await self.model.complete(
                        request, lambda text: self.ctx.emit("text", text)
                    )
                    self.session.append("response", {
                        "message": reply.message,
                        "finish_reason": reply.finish_reason,
                        "usage": reply.usage,
                    })
                    if reply.message.get("role") != "assistant":
                        raise ValueError("Model response must have the assistant role")
                    calls = reply.message.get("tool_calls") or []
                    if reply.finish_reason not in ("stop", "tool_calls"):
                        raise ValueError(f"Model did not finish: {reply.finish_reason}")
                    if bool(calls) != (reply.finish_reason == "tool_calls"):
                        raise ValueError("Model finish reason and tool calls disagree")
                    self._validate_calls(calls)
                    add(reply.message)
                    self.ctx.emit("response_end", {"step": step})
                    if not calls:
                        result = RunResult("completed", text=reply.message.get("content") or "")
                        break
                    for call in calls:
                        function = call["function"]
                        # Committed before execution; a crash leaves an unresolved call.
                        self.session.append("tool_started", {"id": call["id"]})
                        self.ctx.emit("tool_started", {
                            "id": call["id"], "name": function["name"]
                        })
                        output = await self.tools.execute(
                            function["name"], function["arguments"]
                        )
                        add({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(output, ensure_ascii=False, allow_nan=False),
                        })
                        self.ctx.emit("tool_finished", {
                            "id": call["id"], "name": function["name"], "result": output
                        })
                else:
                    result = RunResult("limit", error=f"Reached {self.max_steps} model steps")
            except asyncio.CancelledError:
                result = RunResult("cancelled", error="Cancelled by caller")
                raise
            except Exception as exc:
                result = RunResult("failed", error=str(exc))
            finally:
                self.session.append("run_ended", {
                    "status": result.status, "error": result.error
                })
                self.ctx.emit("run_ended", result)
            return result

    @staticmethod
    def _validate_calls(calls: list[dict]) -> None:
        ids = set()
        for call in calls:
            if call.get("type") != "function":
                raise ValueError("Unsupported tool call type")
            call_id, function = call.get("id"), call.get("function", {})
            if not isinstance(call_id, str) or not call_id or call_id in ids:
                raise ValueError("Missing or duplicate tool call ID")
            if not isinstance(function.get("name"), str):
                raise ValueError("Tool call has no function name")
            if not isinstance(function.get("arguments"), str):
                raise ValueError("Tool arguments must be a JSON string")
            ids.add(call_id)


class LoopPlugin:
    name = "loop"
    requires = ("model", "tools", "session")

    def __init__(self, max_steps: int = 20):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.max_steps = max_steps

    async def setup(self, ctx: Context) -> None:
        ctx.provide("loop", ToolLoop(
            ctx.require("model"), ctx.require("tools"), ctx.require("session"),
            ctx, self.max_steps,
        ))
