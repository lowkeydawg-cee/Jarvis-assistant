"""The conversation loop: listen, think (with tools), speak, and handle the inbox.

The inbox is how everything that isn't you talking reaches Jarvis: watcher
alerts, the morning briefing, and the screen hotkey. Producers post to it from
their own threads and set `attention`, which wakes the main loop out of
listening or waiting for the wake word.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from typing import Callable

from jarvis.persona import system_prompt
from jarvis.streaming import SentenceStream
from jarvis.tools import ToolBox

_YES = re.compile(r"\b(yes|yeah|yep|sure|go ahead|do it|ok|okay|affirmative|please)\b", re.I)
_NO = re.compile(r"\b(no|nope|don't|do not|stop|cancel|never mind|wait)\b", re.I)
_EXIT = re.compile(r"^\s*(goodbye|bye|exit|quit|shut down|that's all)\b", re.I)


def is_yes(text: str) -> bool:
    return bool(_YES.search(text)) and not _NO.search(text)


def is_exit(text: str) -> bool:
    return bool(_EXIT.search(text))


class Assistant:
    MAX_TOOL_ROUNDS = 5

    def __init__(self, llm, speaker, listen: Callable[[], str], cfg: dict):
        self.llm = llm
        self.speaker = speaker
        self.listen = listen
        self.history_turns = int(cfg["llm"].get("history_turns", 12))
        self.history: list[dict] = []
        self.tools_enabled = cfg["tools"].get("enabled", True)
        self.toolbox = ToolBox(cfg["tools"], confirm=self._confirm, announce=self.speaker.say)
        self.inbox: queue.Queue = queue.Queue()
        self.attention = threading.Event()
        self.screen = None  # set by the CLI when vision is enabled
        self.on_error: Callable[[Exception], None] | None = None
        self.router = None  # QuickRouter, set by the CLI unless router.enabled is false
        self.last_source = ""  # "instant" when the router answered, else "model"

    # ---- inbox -----------------------------------------------------------

    def post(self, kind: str, text: str = "") -> None:
        """Thread-safe: queue something for Jarvis to handle between turns."""
        self.inbox.put((kind, text))
        self.attention.set()

    def drain_inbox(self) -> None:
        self.attention.clear()
        while True:
            try:
                kind, text = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "say":
                    self.speaker.say(text)
                elif kind == "ask":
                    self.respond(text)
                elif kind == "screen" and self.screen is not None:
                    self.speaker.say("Taking a look.")
                    seen = self.screen.look(text)
                    self.respond(
                        "I pressed the screen hotkey. Here is what a vision model saw on my screen:\n"
                        f"{seen}\nTell me briefly what matters, and how to fix any error."
                    )
            except Exception as exc:
                print(f"  [error] {exc}")
                if self.on_error:
                    self.on_error(exc)

    # ---- conversation ----------------------------------------------------

    def _confirm(self, what: str) -> bool:
        print(f"  [confirm] {what}")
        self.speaker.say("Should I go ahead?")
        return is_yes(self.listen())

    def _messages(self) -> list[dict]:
        trimmed = self.history[-self.history_turns * 2 :]
        # Never start the window on a dangling tool result.
        while trimmed and trimmed[0]["role"] == "tool":
            trimmed = trimmed[1:]
        return [{"role": "system", "content": system_prompt()}, *trimmed]

    def respond(self, user_text: str) -> str:
        """Run one turn. Sentences are spoken as they stream in. Returns the full reply."""
        self.history.append({"role": "user", "content": user_text})
        quick = self.router.answer(user_text) if self.router else None
        if quick:
            self.last_source = "instant"
            self.history.append({"role": "assistant", "content": quick})
            self.speaker.say(quick)
            self.speaker.wait()
            return quick
        self.last_source = "model"
        tools = self.toolbox.specs() if self.tools_enabled else None
        spoken: list[str] = []

        for _ in range(self.MAX_TOOL_ROUNDS):
            stream = SentenceStream()

            def on_text(delta: str) -> None:
                for sentence in stream.feed(delta):
                    spoken.append(sentence)
                    self.speaker.feed(sentence)

            msg = self.llm.chat_stream(self._messages(), tools=tools, on_text=on_text)
            for sentence in stream.flush():
                spoken.append(sentence)
                self.speaker.feed(sentence)
            calls = msg.get("tool_calls") or []
            self.history.append(
                {"role": "assistant", "content": msg.get("content", ""), **({"tool_calls": calls} if calls else {})}
            )
            interrupted = self.speaker.wait()
            if interrupted:
                self.history.append({"role": "user", "content": "(I interrupted you.)"})
                return " ".join(spoken)
            if not calls:
                return " ".join(spoken)
            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result = self.toolbox.call(fn.get("name", ""), args)
                self.history.append({
                    "role": "tool", "content": result, "tool_name": fn.get("name", ""),
                    **({"tool_call_id": call["id"]} if call.get("id") else {}),
                })

        fallback = "I got a bit tangled up there. Could you ask that another way?"
        self.speaker.say(fallback)
        return fallback

    def run(self, wait_for_wake: Callable[[threading.Event], bool] | None = None) -> None:
        self.speaker.say("Jarvis online. What can I do for you?")
        while True:
            self.drain_inbox()
            if wait_for_wake:
                if not wait_for_wake(self.attention):
                    continue  # woken by the inbox, not the wake word
                self.speaker.say("Yes?")
            try:
                heard = self.listen().strip()
            except (KeyboardInterrupt, EOFError):
                break
            if not heard:
                continue
            if is_exit(heard):
                break
            try:
                self.respond(heard)
            except Exception as exc:  # keep the assistant alive on any single failure
                print(f"  [error] {exc}")
                self.speaker.say("Something went wrong on my end. Check the terminal for details.")
        self.speaker.say("Signing off. Talk soon.")
