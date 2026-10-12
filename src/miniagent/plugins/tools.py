"""Tool registry and a minimal local shell/file toolset (not a sandbox)."""

import asyncio
import copy
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from miniagent.kernel import Context


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[dict, Callable, Draft202012Validator]] = {}

    def register(
        self, name: str, description: str, parameters: dict,
        execute: Callable[[dict], Awaitable[dict]],
    ) -> Callable:
        if name in self._tools:
            raise ValueError(f"Duplicate tool: {name}")
        Draft202012Validator.check_schema(parameters)
        definition = {"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters,
        }}
        self._tools[name] = (
            copy.deepcopy(definition), execute, Draft202012Validator(parameters)
        )
        return lambda: self._tools.pop(name)

    def definitions(self) -> list[dict]:
        return copy.deepcopy([tool[0] for tool in self._tools.values()])

    async def execute(self, name: str, arguments: str) -> dict:
        if name not in self._tools:
            return {"status": "error", "error": f"Unknown tool: {name}"}
        _, execute, validator = self._tools[name]
        try:
            args = json.loads(arguments)
            validator.validate(args)
        except (ValueError, ValidationError) as exc:
            return {"status": "error", "error": f"Invalid arguments: {exc}"}
        try:
            return {"status": "ok", "result": await execute(args)}
        except Exception as exc:
            # Tool failures are observations for the model. Cancellation propagates.
            return {"status": "error", "error": str(exc)}


class LocalTools:
    def __init__(self, cwd: str, timeout: float, output_limit: int):
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {self.cwd}")
        if timeout <= 0 or output_limit < 1:
            raise ValueError("timeout and output_limit must be positive")
        self.timeout, self.output_limit = timeout, output_limit

    async def shell(self, args: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", args["command"], cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            raise TimeoutError(f"Shell exceeded {self.timeout:g} seconds") from None
        finally:
            # End the whole command group, including children on timeout/cancel.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
        stdout, stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
        return {
            "exit_code": process.returncode,
            "stdout": stdout[:self.output_limit],
            "stderr": stderr[:self.output_limit],
            "truncated": len(stdout) > self.output_limit or len(stderr) > self.output_limit,
        }

    async def file(self, args: dict) -> dict:
        path = (self.cwd / args["path"]).resolve()
        action = args["action"]
        if action == "read":
            with path.open(encoding="utf-8") as source:
                text = source.read(self.output_limit + 1)
            return {"text": text[:self.output_limit], "truncated": len(text) > self.output_limit}
        if action == "write":
            path.write_text(args["content"], encoding="utf-8")
        elif action == "replace":
            text = path.read_text(encoding="utf-8")
            if text.count(args["old"]) != 1:
                raise ValueError("old must match exactly once; file was not changed")
            path.write_text(text.replace(args["old"], args["new"], 1), encoding="utf-8")
        return {"path": str(path), "action": action}


class ToolsPlugin:
    name = "tools"
    requires = ()

    def __init__(self, cwd: str = ".", timeout: float = 30, output_limit: int = 20000):
        self.local = LocalTools(cwd, timeout, output_limit)

    async def setup(self, ctx: Context) -> None:
        registry = ToolRegistry()
        registry.register(
            "shell",
            f"Run a shell command in {self.local.cwd}. Each call is a fresh shell. "
            "Commands run with the host user's permissions; this is not a sandbox.",
            {
                "type": "object",
                "properties": {"command": {"type": "string", "minLength": 1}},
                "required": ["command"], "additionalProperties": False,
            },
            self.local.shell,
        )
        registry.register(
            "file",
            f"Read, write, or replace UTF-8 text files. Relative paths use {self.local.cwd}. "
            "write overwrites a file; replace requires old to match exactly once.",
            {
                "type": "object",
                "properties": {
                    "action": {"enum": ["read", "write", "replace"]},
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "old": {"type": "string", "minLength": 1},
                    "new": {"type": "string"},
                },
                "required": ["action", "path"], "additionalProperties": False,
                "allOf": [
                    {"if": {"properties": {"action": {"const": "write"}}},
                     "then": {"required": ["content"]}},
                    {"if": {"properties": {"action": {"const": "replace"}}},
                     "then": {"required": ["old", "new"]}},
                ],
            },
            self.local.file,
        )
        ctx.provide("tools", registry)
