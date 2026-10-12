"""Configuration-only composition root. Concrete plugins are imported by name."""

import argparse
import asyncio
import importlib
import sys
import tomllib
from pathlib import Path

from miniagent.kernel import Kernel


async def run(config_path: Path, prompt: str | None) -> int:
    with config_path.open("rb") as source:
        config = tomllib.load(source)
    plugins = []
    for entry in config["plugins"]:
        module, name = entry["factory"].split(":", 1)
        factory = getattr(importlib.import_module(module), name)
        plugins.append(factory(**entry.get("options", {})))
    kernel = Kernel()
    await kernel.start(plugins)
    try:
        return await kernel.require("cli").run(prompt)
    finally:
        await kernel.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal plugin-composed agent")
    parser.add_argument("--config", type=Path, default=Path("miniagent.toml"))
    parser.add_argument("prompt", nargs="?", help="Omit to start an interactive session")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args.config, args.prompt))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(f"miniagent: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
