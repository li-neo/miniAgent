import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from miniagent import Kernel
from miniagent.contracts import ModelReply
from miniagent.plugins.loop import LoopPlugin
from miniagent.plugins.session import JsonlSession, SessionPlugin
from miniagent.plugins.tools import ToolsPlugin


def final(text="done", reason="stop"):
    return ModelReply({"role": "assistant", "content": text}, reason)


def call(name, arguments, call_id="call_1"):
    return ModelReply({
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }, "tool_calls")


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def prepare(self, messages, tools):
        return {"model": "test", "messages": messages, "tools": tools, "stream": True}

    async def complete(self, request, on_text):
        self.requests.append(copy.deepcopy(request))
        reply = self.replies.pop(0)
        if reply.message.get("content"):
            on_text(reply.message["content"])
        return reply


class ModelPlugin:
    name = "test-model"
    requires = ()

    def __init__(self, model):
        self.model = model

    async def setup(self, ctx):
        ctx.provide("model", self.model)


class KernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependency_failure_rolls_back_resources_and_observers(self):
        cleaned, events = [], []

        class First:
            name, requires = "first", ()

            async def setup(self, ctx):
                self.ctx = ctx
                ctx.provide("value", 42)
                ctx.on("event", events.append)
                ctx.defer(lambda: cleaned.append("first"))

        class Missing:
            name, requires = "missing", ("absent",)

        first, kernel = First(), Kernel()
        with self.assertRaisesRegex(ValueError, "Missing service"):
            await kernel.start([first, Missing()])
        self.assertEqual(cleaned, ["first"])
        first.ctx.emit("event", "ignored")
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            kernel.require("value")

    async def test_partial_setup_and_duplicate_services_are_rolled_back(self):
        cleaned = []

        class Bad:
            name, requires = "bad", ()

            async def setup(self, ctx):
                ctx.provide("value", 1)
                ctx.defer(lambda: cleaned.append(True))
                ctx.provide("value", 2)

        kernel = Kernel()
        with self.assertRaisesRegex(ValueError, "already registered"):
            await kernel.start([Bad()])
        self.assertEqual(cleaned, [True])
        with self.assertRaises(ValueError):
            kernel.require("value")

    async def test_cleanup_continues_when_one_cleanup_fails(self):
        cleaned = []

        class Plugin:
            name, requires = "one", ()

            async def setup(self, ctx):
                ctx.provide("value", 1)
                ctx.defer(lambda: cleaned.append(True))

                def fail():
                    raise RuntimeError("cleanup error")

                ctx.defer(fail)

        kernel = Kernel()
        await kernel.start([Plugin()])
        with self.assertRaises(ExceptionGroup):
            await kernel.close()
        self.assertEqual(cleaned, [True])
        with self.assertRaises(ValueError):
            kernel.require("value")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "session.jsonl"
        self.kernel = Kernel()
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.kernel.close)

    async def start(self, model, max_steps=20):
        await self.kernel.start([
            ModelPlugin(model), ToolsPlugin(cwd=str(self.root)),
            SessionPlugin(path=str(self.path), system="test"),
            LoopPlugin(max_steps=max_steps),
        ])
        return self.kernel.require("loop")

    def events(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    async def test_write_verify_trace_and_resume_with_replaced_model(self):
        model = ScriptedModel(
            call("file", {"action": "write", "path": "answer.txt", "content": "42"}),
            call("shell", {"command": "cat answer.txt"}, "call_2"),
            final("verified"),
        )
        agent = await self.start(model)
        result = await agent.run("write and verify")
        self.assertEqual(result.status, "completed")
        self.assertEqual((self.root / "answer.txt").read_text(), "42")
        tool = model.requests[-1]["messages"][-1]
        self.assertEqual(tool["tool_call_id"], "call_2")
        self.assertEqual(json.loads(tool["content"])["result"]["stdout"], "42")
        logged = [event["data"] for event in self.events() if event["kind"] == "request"]
        self.assertEqual(logged, model.requests)

        await self.kernel.close()
        replacement = ScriptedModel(final("continued"))
        agent = await self.start(replacement)
        self.assertEqual((await agent.run("continue")).text, "continued")
        previous = replacement.requests[0]["messages"]
        self.assertTrue(any(m.get("content") == "verified" for m in previous))
        # Restoring history did not execute the old tools again.
        starts = [e for e in self.events() if e["kind"] == "tool_started"]
        self.assertEqual(len(starts), 2)

    async def test_tool_error_is_returned_to_model(self):
        model = ScriptedModel(call("missing_tool", {}), final("recovered"))
        agent = await self.start(model)
        self.assertEqual((await agent.run("try")).status, "completed")
        output = json.loads(model.requests[1]["messages"][-1]["content"])
        self.assertEqual(output["status"], "error")
        self.assertIn("Unknown tool", output["error"])

    async def test_step_limit_stops_new_requests(self):
        model = ScriptedModel(call("shell", {"command": "printf ok"}))
        agent = await self.start(model, max_steps=1)
        self.assertEqual((await agent.run("loop")).status, "limit")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.kernel.require("session").history()[-1]["role"], "tool")

    async def test_truncated_response_is_not_committed_as_an_answer(self):
        agent = await self.start(ScriptedModel(final("partial", "length")))
        result = await agent.run("answer")
        self.assertEqual(result.status, "failed")
        self.assertIn("length", result.error)
        self.assertEqual(self.kernel.require("session").history()[-1]["role"], "user")
        self.assertTrue(any(e["kind"] == "response" for e in self.events()))

    async def test_cancel_model_stops_and_leaves_resumable_history(self):
        entered = asyncio.Event()

        class WaitingModel(ScriptedModel):
            async def complete(self, request, on_text):
                entered.set()
                await asyncio.Event().wait()

        agent = await self.start(WaitingModel())
        task = asyncio.create_task(agent.run("wait"))
        await asyncio.wait_for(entered.wait(), 1)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            await agent.run("overlap")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events()[-1]["data"]["status"], "cancelled")
        self.assertEqual(self.kernel.require("session").history()[-1]["content"], "wait")

    async def test_cancel_tool_retains_unknown_effects_and_refuses_resume(self):
        entered = asyncio.Event()

        async def effect_then_wait(args):
            (self.root / "effect.txt").write_text("already happened")
            entered.set()
            await asyncio.Event().wait()

        agent = await self.start(ScriptedModel(call("wait", {})))
        self.kernel.require("tools").register(
            "wait", "test", {"type": "object"}, effect_then_wait
        )
        task = asyncio.create_task(agent.run("do work"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue((self.root / "effect.txt").exists())
        await self.kernel.close()
        with self.assertRaisesRegex(ValueError, "unresolved tool calls"):
            JsonlSession(str(self.path), "test")

    async def test_session_lock_and_incomplete_tail(self):
        await self.start(ScriptedModel(final()))
        with self.assertRaises(BlockingIOError):
            JsonlSession(str(self.path), "test")
        await self.kernel.close()
        with self.path.open("a") as output:
            output.write('{"seq":')
        with self.assertRaisesRegex(ValueError, "Incomplete session record"):
            JsonlSession(str(self.path), "test")

    async def test_crashed_run_is_not_replayed(self):
        await self.start(ScriptedModel())
        self.kernel.require("session").append("run_started", {})
        await self.kernel.close()
        with self.assertRaisesRegex(ValueError, "interrupted run"):
            JsonlSession(str(self.path), "test")


if __name__ == "__main__":
    unittest.main()
