"""Terminal input/output; it drives the loop service, never the model or tools."""

import asyncio
import signal
import sys

from miniagent.contracts import AgentLoop
from miniagent.kernel import Context


class CLI:
    def __init__(self, agent: AgentLoop):
        self.agent = agent
        self._streamed = False

    def text(self, text: str) -> None:
        self._streamed = True
        print(text, end="", flush=True)

    async def _run_prompt(self, prompt: str) -> int:
        self._streamed = False
        task = asyncio.create_task(self.agent.run(prompt))
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGINT)

        def cancel() -> None:
            if not task.done() and not task.cancelling():
                task.cancel()

        loop.add_signal_handler(signal.SIGINT, cancel)
        try:
            result = await task
            if result.status == "completed":
                if not self._streamed:
                    print(result.text)
                return 0
            print(f"[{result.status}] {result.error}", file=sys.stderr)
            return 1
        except asyncio.CancelledError:
            print("\n[cancelled]", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)

    @staticmethod
    async def _read_prompt() -> str:
        # No blocking input thread left behind when the user interrupts at the prompt.
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        fd = sys.stdin.fileno()

        def ready() -> None:
            loop.remove_reader(fd)
            if not future.done():
                future.set_result(sys.stdin.readline())

        print("you> ", end="", flush=True)
        loop.add_reader(fd, ready)
        try:
            return await future
        finally:
            loop.remove_reader(fd)

    async def run(self, prompt: str | None = None) -> int:
        if prompt is not None:
            return await self._run_prompt(prompt)
        print("miniAgent · /exit to quit · Ctrl+C to stop")
        while True:
            line = await self._read_prompt()
            if not line or line.strip() == "/exit":
                return 0
            if line.strip():
                await self._run_prompt(line.rstrip("\n"))


class CLIPlugin:
    name = "cli"
    requires = ("loop",)

    async def setup(self, ctx: Context) -> None:
        cli = CLI(ctx.require("loop"))
        ctx.on("text", cli.text)
        ctx.on("response_end", lambda _: print() if cli._streamed else None)
        ctx.on("tool_started", lambda event: print(
            f"[tool] {event['name']}", file=sys.stderr
        ))
        ctx.provide("cli", cli)
