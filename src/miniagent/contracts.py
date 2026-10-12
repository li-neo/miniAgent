"""Small service contracts. Messages retain provider-specific JSON fields."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Message = dict[str, Any]


@dataclass
class ModelReply:
    message: Message
    finish_reason: str
    usage: dict[str, Any] | None = None


class Model(Protocol):
    def prepare(self, messages: list[Message], tools: list[dict]) -> dict: ...

    async def complete(
        self, request: dict, on_text: Callable[[str], None]
    ) -> ModelReply: ...


class Tools(Protocol):
    def definitions(self) -> list[dict]: ...

    async def execute(self, name: str, arguments: str) -> dict: ...


class Session(Protocol):
    def append(self, kind: str, data: dict) -> None: ...

    def history(self) -> list[Message]: ...


@dataclass
class RunResult:
    status: Literal["completed", "limit", "failed", "cancelled"]
    text: str = ""
    error: str = ""


class AgentLoop(Protocol):
    async def run(self, prompt: str) -> RunResult: ...
