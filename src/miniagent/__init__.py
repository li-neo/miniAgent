"""Agent capabilities are supplied by plugins; the kernel only composes them."""

from .kernel import Context, Kernel, Plugin

__all__ = ["Context", "Kernel", "Plugin"]
