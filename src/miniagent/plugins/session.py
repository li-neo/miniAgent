"""One append-only JSONL session with a process-level single-writer lock."""

import copy
import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from miniagent.kernel import Context


class JsonlSession:
    def __init__(self, path: str, system: str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        self._events: list[dict] = []
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._file.seek(0)
            for index, line in enumerate(self._file):
                if not line.endswith("\n"):
                    raise ValueError("Incomplete session record; use a new session path")
                event = json.loads(line)
                if event["seq"] != index:
                    raise ValueError("Invalid session sequence")
                self._events.append(event)
            if not self._events:
                self.append("session", {"version": 1, "system": system})
            header = self._events[0]
            if header["kind"] != "session" or header["data"].get("version") != 1:
                raise ValueError("Unsupported session format")
            if header["data"]["system"] != system:
                raise ValueError("System prompt changed; use a new session path")
            self.history()
        except BaseException:
            self._file.close()
            raise

    def append(self, kind: str, data: dict) -> None:
        event = {
            "seq": len(self._events),
            "time": datetime.now(UTC).isoformat(),
            "kind": kind,
            "data": data,
        }
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        self._file.write(line + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._events.append(json.loads(line))

    def history(self) -> list[dict]:
        messages = [{"role": "system", "content": self._events[0]["data"]["system"]}]
        pending = set()
        running = False
        for event in self._events:
            kind, data = event["kind"], event["data"]
            if kind == "run_started":
                running = True
            elif kind == "run_ended":
                running = False
            elif kind == "message":
                messages.append(data)
                if data["role"] == "assistant":
                    pending.update(call["id"] for call in data.get("tool_calls", []))
                elif data["role"] == "tool":
                    pending.discard(data["tool_call_id"])
        if running or pending:
            raise ValueError(
                "Session has an interrupted run or unresolved tool calls. "
                "Inspect its log and external effects, then use a new session path; "
                "calls will not be replayed automatically."
            )
        return copy.deepcopy(messages)

    def close(self) -> None:
        self._file.close()


class SessionPlugin:
    name = "session"
    requires = ()

    def __init__(
        self,
        path: str = ".miniagent/session.jsonl",
        system: str = "You are a helpful agent. Use the available tools when needed.",
    ):
        self.path = path
        self.system = system

    async def setup(self, ctx: Context) -> None:
        session = JsonlSession(self.path, self.system)
        ctx.defer(session.close)
        ctx.provide("session", session)
