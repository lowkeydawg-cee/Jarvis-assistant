"""Plugins: add a skill by dropping one Python file into the plugins folder.

A plugin is a .py file with a `register(jarvis)` function:

    def register(jarvis):
        jarvis.tool("coin_flip", "Flip a coin.", lambda args: random.choice(["heads", "tails"]),
                    read_only=True, triggers=r"coin|flip")
        jarvis.route(r"flip a coin", lambda match: "Heads.")
        jarvis.every(3600, check_something)

Each file loads on its own, so a broken one is reported and skipped, and never
stops Jarvis. A plugin's tools ask for confirmation unless it declares them
read_only. Plugins are ordinary Python running on your laptop with your
permissions: only add ones you wrote or trust. Nothing is ever downloaded for you.
Files starting with an underscore are ignored. See docs/PLUGINS.md.
"""

from __future__ import annotations

import importlib.util
import re
import threading
import time
from pathlib import Path
from typing import Callable

from jarvis import toolpick
from jarvis.tools import Tool


class PluginAPI:
    """What a plugin can do. Passed to its register() function."""

    def __init__(self, name: str, bot):
        self.name = name
        self.bot = bot
        self.tools: list[str] = []
        self.routes = 0
        self.jobs = 0

    def tool(self, name: str, description: str, handler: Callable[[dict], str], parameters: dict | None = None,
             read_only: bool = False, triggers: str = "") -> None:
        """Add a tool for the model. It asks the user first unless read_only=True.

        `triggers` is a regular expression of words that make this tool relevant. Without it the
        tool's description goes out with every question, which costs cloud tokens.
        """
        full = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{self.name}__{name}")[:64]
        kwargs = {"parameters": parameters} if parameters else {}
        self.bot.toolbox.register(Tool(full, description, lambda a: str(handler(a)), confirm=not read_only, **kwargs))
        if triggers:
            toolpick.add_tool_group(full, f"plugin:{self.name}", triggers)
        self.tools.append(full)

    def route(self, pattern: str, handler: Callable) -> None:
        """Add an instant answer. `pattern` matches the whole request, in lowercase without punctuation."""
        if self.bot.router:
            self.bot.router.add(f"plugin:{self.name}", pattern, handler)
            self.routes += 1

    def every(self, seconds: float, job: Callable[[], str | None]) -> None:
        """Run `job` in the background. If it returns text, Jarvis says it and sends it to your phone."""
        def loop():
            while True:
                time.sleep(seconds)
                try:
                    text = job()
                    if text:
                        self.notify(str(text))
                except Exception as exc:
                    print(f"  [plugin {self.name}] {exc}")

        threading.Thread(target=loop, daemon=True, name=f"plugin-{self.name}").start()
        self.jobs += 1

    def notify(self, text: str) -> None:
        self.bot.post("say", text)
        if self.bot.notifier is not None:
            self.bot.notifier.send("alerts", text)


def load_plugins(folder: str | Path, bot, log: Callable[[str], None] = print) -> list[dict]:
    """Load every plugin in `folder`. Returns one status line per file."""
    folder = Path(folder).expanduser()
    results: list[dict] = []
    if not folder.is_dir():
        return results
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = re.sub(r"\W", "_", path.stem)
        api = PluginAPI(name, bot)
        try:
            spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "register"):
                raise ValueError("it has no register(jarvis) function")
            module.register(api)
            results.append({"name": name, "ok": True, "tools": api.tools, "routes": api.routes, "jobs": api.jobs})
            log(f"  [plugin] {name}: {len(api.tools)} tools, {api.routes} shortcuts, {api.jobs} jobs")
        except BaseException as exc:  # a plugin calling sys.exit must not take Jarvis down either
            if isinstance(exc, KeyboardInterrupt):
                raise
            results.append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
            log(f"  [plugin] {name} failed to load: {exc}")
    return results
