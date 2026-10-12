"""Service registration, scoped cleanup, and observational notifications."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Plugin(Protocol):
    name: str
    requires: tuple[str, ...]

    async def setup(self, ctx: Context) -> None: ...


class Context:
    def __init__(self, kernel: Kernel):
        self._kernel = kernel
        self._cleanups: list[Callable] = []

    def provide(self, name: str, service: Any) -> None:
        if name in self._kernel._services:
            raise ValueError(f"Service already registered: {name}")
        self._kernel._services[name] = service
        self.defer(lambda: self._kernel._services.pop(name))

    def require(self, name: str) -> Any:
        return self._kernel.require(name)

    def defer(self, cleanup: Callable) -> None:
        self._cleanups.append(cleanup)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        listeners = self._kernel._listeners.setdefault(event, [])
        listeners.append(callback)
        self.defer(lambda: listeners.remove(callback))

    def emit(self, event: str, value: Any) -> None:
        # Observers cannot veto execution; business operations use service calls.
        for callback in tuple(self._kernel._listeners.get(event, ())):
            try:
                callback(value)
            except Exception:
                log.exception("Observer failed: %s", event)

    async def close(self) -> None:
        errors = []
        while self._cleanups:
            try:
                result = self._cleanups.pop()()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Plugin cleanup failed", errors)


class Kernel:
    def __init__(self):
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._contexts: list[Context] = []
        self._started = False

    def require(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise ValueError(f"Missing service: {name}") from None

    async def start(self, plugins: Iterable[Plugin]) -> None:
        if self._started:
            raise RuntimeError("Kernel already started")
        self._started = True
        names = set()
        try:
            for plugin in plugins:
                if plugin.name in names:
                    raise ValueError(f"Duplicate plugin: {plugin.name}")
                names.add(plugin.name)
                for dependency in plugin.requires:
                    self.require(dependency)
                ctx = Context(self)
                self._contexts.append(ctx)
                await plugin.setup(ctx)
        except BaseException as exc:
            try:
                await self.close()
            except Exception as cleanup_error:
                exc.add_note(f"Cleanup also failed: {cleanup_error}")
            raise

    async def close(self) -> None:
        errors = []
        while self._contexts:
            try:
                await self._contexts.pop().close()
            except Exception as exc:
                errors.append(exc)
        self._started = False
        if errors:
            raise ExceptionGroup("Kernel cleanup failed", errors)
