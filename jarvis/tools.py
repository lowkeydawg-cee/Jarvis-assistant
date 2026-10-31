"""The tool registry Jarvis's brain can call, with a safety layer around actions.

Every capability (shell commands, notes search, screen vision, MCP servers,
briefings) registers itself here as a Tool. The LLM sees one flat list of
function specs, and every call comes back through ToolBox.call, which is the
single place where announcing and confirming happen.

Shell command safety model:
1. Only commands whose program is on the allowlist can run.
2. Shell metacharacters (pipes, redirects, chaining, substitution) are refused,
   and commands run without a shell, so nothing can be smuggled in.
3. Known destructive arguments are refused even for allowed programs.
4. The user confirms every command out loud (or by typing) before it runs.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

_FORBIDDEN_CHARS = set(";&|><`$\n\r")
_DANGEROUS_ARGS = {
    "git": {"push", "reset", "clean", "rebase", "filter-branch", "gc", "rm"},
    "pip": {"uninstall"},
    "npm": {"uninstall", "publish", "unpublish"},
}
_DANGEROUS_FLAGS = {"-rf", "-fr", "--force", "-f", "--hard", "--delete"}
_SCRIPT_FLAGS = {"python": {"-c"}, "python3": {"-c"}, "node": {"-e", "--eval", "-p"}}
_NO_ARGS = {"type": "object", "properties": {}}


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[[dict], str]
    parameters: dict = field(default_factory=lambda: dict(_NO_ARGS))
    confirm: bool = False
    # Returns the sentence Jarvis says before running the tool. None means stay quiet.
    announce: Callable[[dict], str | None] | None = None

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    argv: list[str] | None = None


def check_command(command: str, allowed: list[str]) -> Verdict:
    """Decide whether a command may run. Pure function, easy to test."""
    command = command.strip()
    if not command:
        return Verdict(False, "The command was empty.")
    if any(ch in _FORBIDDEN_CHARS for ch in command):
        return Verdict(False, "Chained commands, pipes, and redirects aren't allowed.")
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return Verdict(False, "I couldn't parse that command.")
    if not argv:
        return Verdict(False, "The command was empty.")

    program = os.path.basename(argv[0]).lower()
    if program.endswith(".exe"):
        program = program[:-4]
    if program not in {a.lower() for a in allowed}:
        return Verdict(False, f"{program} isn't on my list of allowed commands.")

    args = [a.lower() for a in argv[1:]]
    if set(args) & _DANGEROUS_ARGS.get(program, set()):
        return Verdict(False, "That looks destructive, so I won't run it.")
    if set(args) & _DANGEROUS_FLAGS:
        return Verdict(False, "Force and delete flags aren't allowed.")
    if set(args) & _SCRIPT_FLAGS.get(program, set()):
        return Verdict(False, "Inline scripts aren't allowed.")
    return Verdict(True, argv=argv)


def _resolve(argv: list[str]) -> list[str]:
    # Windows built-ins like dir, type and echo only exist inside cmd.exe.
    if os.name == "nt" and argv[0].lower() in {"dir", "type", "echo"}:
        return ["cmd", "/c", *argv]
    exe = shutil.which(argv[0])
    return [exe, *argv[1:]] if exe else argv


def run_command(argv: list[str], timeout: int = 20, max_chars: int = 4000) -> str:
    try:
        proc = subprocess.run(
            _resolve(argv), capture_output=True, text=True, timeout=timeout, shell=False
        )
    except FileNotFoundError:
        return f"Error: {argv[0]} is not installed."
    except subprocess.TimeoutExpired:
        return f"Error: the command timed out after {timeout} seconds."
    output = (proc.stdout or "") + (proc.stderr or "")
    output = output.strip() or "(no output)"
    if len(output) > max_chars:
        output = output[:max_chars] + "\n...(truncated)"
    return f"exit code {proc.returncode}\n{output}"


def system_info() -> str:
    total, used, free = shutil.disk_usage(os.path.expanduser("~"))
    gb = 1024 ** 3
    return (
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"CPU cores: {os.cpu_count()}\n"
        f"Disk: {free / gb:.0f} GB free of {total / gb:.0f} GB\n"
        f"Python: {platform.python_version()}\n"
        f"Time: {datetime.now():%Y-%m-%d %H:%M}"
    )


class ToolBox:
    """Holds every tool and dispatches the LLM's calls, asking first where needed."""

    def __init__(
        self,
        cfg: dict,
        confirm: Callable[[str], bool],
        announce: Callable[[str], None],
    ):
        self.allowed = cfg.get("allowed_commands", [])
        self.confirm_commands = cfg.get("confirm_commands", True)
        self.timeout = int(cfg.get("command_timeout", 20))
        self.confirm = confirm
        self.announce = announce
        self.tools: dict[str, Tool] = {}
        self.register(Tool(
            name="run_command",
            description=(
                "Run a single safe shell command on the user's computer and return its output. "
                "Use for checking system status, git status, versions, files in a folder, and similar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The exact command to run."},
                    "reason": {
                        "type": "string",
                        "description": "One short spoken sentence telling the user what you are about to do.",
                    },
                },
                "required": ["command", "reason"],
            },
            handler=self._run_command,
        ))
        self.register(Tool(
            name="system_info",
            description="Get basic facts about this computer: OS, CPU count, disk space, current time.",
            handler=lambda args: system_info(),
        ))

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def specs(self, context: str | None = None) -> list[dict]:
        """All tool specs, or with `context` only those the conversation could need (see toolpick)."""
        if context is None:
            return [tool.spec() for tool in self.tools.values()]
        from jarvis.toolpick import pick

        return [self.tools[n].spec() for n in pick(list(self.tools), context)]

    def call(self, name: str, arguments: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        if tool.announce:
            line = tool.announce(arguments)
            if line:
                self.announce(line)
        if tool.confirm and not self.confirm(f"{name} {arguments}"):
            return "The user declined, so the tool was not used."
        try:
            return tool.handler(arguments)
        except Exception as exc:  # a broken tool should never take Jarvis down
            return f"Error from {name}: {exc}"

    def _run_command(self, arguments: dict) -> str:
        command = str(arguments.get("command", ""))
        reason = str(arguments.get("reason", "")).strip() or f"I'm going to run {command}."
        verdict = check_command(command, self.allowed)
        if not verdict.allowed:
            return f"Refused: {verdict.reason}"
        self.announce(reason)
        if self.confirm_commands and not self.confirm(command):
            return "The user declined, so the command was not run."
        return run_command(verdict.argv, timeout=self.timeout)
